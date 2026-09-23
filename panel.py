"""Панель управления сайтом bomond.site.

Читает и правит секции сайта на Mottor: тексты, фотографии, ссылки.
Аутентификация — один пароль владельца (PANEL_PASSWORD), сессия в подписанной куке.
"""

import base64
import hashlib
import hmac
import itertools
import json
import os
import time
import html as html_mod
from html.parser import HTMLParser

import requests
from flask import Response, jsonify, request, send_file

MOTTOR_URL = "https://api.lpmotor.ru/v3/mcp"
MOTTOR_AUTH = os.environ.get("MOTTOR_AUTH", "")
MOTTOR_USER_ID = os.environ.get("MOTTOR_USER_ID", "")
SITE_ID = int(os.environ.get("MOTTOR_SITE_ID", "2961379"))
PAGE_ID = int(os.environ.get("MOTTOR_PAGE_ID", "2806917"))
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
PANEL_SECRET = os.environ.get("PANEL_SECRET") or (PANEL_PASSWORD + "|bomond-panel")
COOKIE = "bomond_panel"
MAX_IMAGE_BYTES = 70000  # предел приёма картинок у Mottor, проверено опытным путём

SECTION_TITLES = {
    "bnav": "Шапка",
    "bhro": "Первый экран",
    "babt": "О салоне",
    "btim": "Мастера",
    "bsvc": "Услуги",
    "bpro": "Акции",
    "bprc": "Прайс",
    "brev": "Отзывы",
    "bcnt": "Контакты",
    "bftr": "Подвал",
}
TEXT_TAGS = {"h1", "h2", "h3", "h4", "p", "span", "li", "td", "th", "figcaption", "button", "a", "strong", "b", "em", "small"}
SKIP_TAGS = {"script", "style", "canvas"}


# ---------- клиент Mottor ----------

class Mottor:
    def __init__(self):
        self.session_id = None
        self.ids = itertools.count(1)

    def _post(self, body, timeout=120):
        headers = {
            "Authorization": MOTTOR_AUTH,
            "X-Api-User-Id": MOTTOR_USER_ID,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        r = requests.post(MOTTOR_URL, json=body, headers=headers, timeout=timeout)
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        text = r.text
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            for line in text.splitlines():
                if line.startswith("data:"):
                    payload = json.loads(line[5:].strip())
                    if "result" in payload or "error" in payload:
                        return payload
            return {}
        return json.loads(text)

    def _init(self):
        if self.session_id:
            return
        self._post({"jsonrpc": "2.0", "id": next(self.ids), "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "bomond-panel", "version": "1"}}})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def call(self, tool, args, attempts=4):
        last = None
        for _ in range(attempts):
            try:
                self._init()
                res = self._post({"jsonrpc": "2.0", "id": next(self.ids), "method": "tools/call",
                                  "params": {"name": tool, "arguments": args}})
                if "error" in res:
                    raise RuntimeError(res["error"].get("message", "mottor error"))
                chunks = [c["text"] for c in res.get("result", {}).get("content", []) if c.get("type") == "text"]
                return json.loads("\n".join(chunks)) if chunks else {}
            except Exception as e:  # noqa: BLE001
                last = e
                self.session_id = None
                time.sleep(1.5)
        raise RuntimeError(str(last))


mottor = Mottor()


# ---------- разбор HTML секции ----------

class Extractor(HTMLParser):
    """Собирает правимые куски: текстовые узлы, src и alt картинок, href ссылок."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.html = html
        offset = 0
        self.line_starts = [0]
        for line in html.split("\n")[:-1]:
            offset += len(line) + 1
            self.line_starts.append(offset)
        self.items = []
        self.stack = []

    def _offset(self):
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
        if tag not in ("img", "a"):
            return
        raw = self.get_starttag_text() or ""
        base = self._offset()
        for name in ("src", "alt", "href"):
            if tag == "a" and name != "href":
                continue
            if tag == "img" and name == "href":
                continue
            value = dict(attrs).get(name)
            if value is None:
                continue
            needle = name + '="' + value + '"'
            pos = raw.find(needle)
            if pos < 0:
                continue
            start = base + pos + len(name) + 2
            self.items.append({"kind": name if name != "src" else "image", "tag": tag,
                               "start": start, "end": start + len(value), "value": value})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.stack.pop()

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_data(self, data):
        parent = self.stack[-1] if self.stack else ""
        if parent in SKIP_TAGS or parent not in TEXT_TAGS:
            return
        if not data.strip():
            return
        start = self._offset()
        stop = self.html.find("<", start)
        if stop < 0:
            stop = len(self.html)
        raw = self.html[start:stop]
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw) - len(raw.rstrip())
        self.items.append({"kind": "text", "tag": parent, "start": start + lead,
                           "end": stop - trail, "value": html_mod.unescape(raw.strip())})


def extract(html):
    parser = Extractor(html)
    parser.feed(html)
    items = sorted(parser.items, key=lambda x: x["start"])
    for i, item in enumerate(items):
        item["i"] = i
    return items


def patch(html, items, edits):
    """edits: {index: new_value}. Возвращает новый HTML."""
    out = html
    for index in sorted((int(k) for k in edits), reverse=True):
        item = items[index]
        value = edits[str(index)] if str(index) in edits else edits[index]
        value = value.replace("\r", "")
        if item["kind"] == "text":
            value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\xa0", "&nbsp;")
        else:
            value = value.replace('"', "&quot;")
        out = out[: item["start"]] + value + out[item["end"] :]
    return out


def unescape_for_form(value):
    return html_mod.unescape(value)


# ---------- содержимое страницы ----------

def load_sections():
    data = mottor.call("page_content_get", {"site_id": SITE_ID, "page_id": PAGE_ID})
    result = []
    for section in data.get("sections", []):
        html = section.get("data", {}).get("ms_html", "")
        prefix = ""
        marker = html.find('class="')
        if marker >= 0:
            prefix = html[marker + 7 : marker + 11]
        items = [it for it in extract(html) if it["kind"] != "href" or it["value"].startswith("http") or it["value"].startswith("tel:")]
        fields = [{"i": it["i"], "kind": it["kind"], "tag": it["tag"],
                   "value": unescape_for_form(it["value"]) if it["kind"] == "text" else it["value"]}
                  for it in items]
        result.append({"id": section["id"], "position": section.get("position"),
                       "title": SECTION_TITLES.get(prefix, prefix or "Секция"), "fields": fields})
    return result


def save_section(section_id, edits, originals):
    data = mottor.call("page_content_get", {"site_id": SITE_ID, "page_id": PAGE_ID})
    section = next((s for s in data.get("sections", []) if s["id"] == section_id), None)
    if not section:
        raise RuntimeError("секция не найдена")
    html = section["data"]["ms_html"]
    items = extract(html)
    for key, was in (originals or {}).items():
        index = int(key)
        if index >= len(items):
            raise RuntimeError("stale")
        current = unescape_for_form(items[index]["value"]) if items[index]["kind"] == "text" else items[index]["value"]
        if current.strip() != str(was).strip():
            raise RuntimeError("stale")
    new_html = patch(html, items, edits)
    if new_html == html:
        return False
    mottor.call("section_content_update", {
        "site_id": SITE_ID, "page_id": PAGE_ID, "section_id": section_id, "fonts": [],
        "data": {"ms_html": new_html, "ms_css": section["data"]["ms_css"], "ms_js": section["data"]["ms_js"]},
    })
    mottor.call("page_deploy", {"site_id": SITE_ID, "page_id": PAGE_ID})
    return True


def upload_image(raw_bytes):
    if len(raw_bytes) > MAX_IMAGE_BYTES:
        raise RuntimeError("too_big")
    res = mottor.call("page_image_upload", {"site_id": SITE_ID, "page_id": PAGE_ID,
                                            "content": base64.b64encode(raw_bytes).decode()})
    url = res.get("url") or (res.get("data") or {}).get("url")
    if not url:
        raise RuntimeError("upload_failed")
    return url


# ---------- сессия ----------

def make_token():
    issued = str(int(time.time()))
    sig = hmac.new(PANEL_SECRET.encode(), issued.encode(), hashlib.sha256).hexdigest()[:32]
    return issued + "." + sig


def valid_token(token):
    if not token or "." not in token:
        return False
    issued, sig = token.rsplit(".", 1)
    expect = hmac.new(PANEL_SECRET.encode(), issued.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return False
    return time.time() - int(issued) < 30 * 24 * 3600


def authorized():
    return valid_token(request.cookies.get(COOKIE, ""))


def init_app(app):
    here = os.path.dirname(os.path.abspath(__file__))

    @app.route("/panel")
    def panel_page():
        return send_file(os.path.join(here, "panel.html"))

    @app.route("/panel/api/login", methods=["POST"])
    def panel_login():
        body = request.get_json(force=True, silent=True) or {}
        given = str(body.get("password", ""))
        if not PANEL_PASSWORD or not hmac.compare_digest(given.encode(), PANEL_PASSWORD.encode()):
            time.sleep(1.0)
            return jsonify(ok=False), 401
        resp = jsonify(ok=True)
        resp.set_cookie(COOKIE, make_token(), max_age=30 * 24 * 3600, httponly=True, secure=True, samesite="Lax")
        return resp

    @app.route("/panel/api/logout", methods=["POST"])
    def panel_logout():
        resp = jsonify(ok=True)
        resp.delete_cookie(COOKIE)
        return resp

    @app.route("/panel/api/content")
    def panel_content():
        if not authorized():
            return jsonify(ok=False, error="auth"), 401
        try:
            return jsonify(ok=True, sections=load_sections())
        except Exception as e:  # noqa: BLE001
            return jsonify(ok=False, error=str(e)), 502

    @app.route("/panel/api/save", methods=["POST"])
    def panel_save():
        if not authorized():
            return jsonify(ok=False, error="auth"), 401
        body = request.get_json(force=True, silent=True) or {}
        try:
            changed = save_section(str(body.get("section_id")), body.get("edits") or {}, body.get("originals") or {})
        except RuntimeError as e:  # noqa: BLE001
            if str(e) == "stale":
                return jsonify(ok=False, error="stale"), 409
            return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=True, changed=changed)

    @app.route("/panel/api/image", methods=["POST"])
    def panel_image():
        if not authorized():
            return jsonify(ok=False, error="auth"), 401
        f = request.files.get("file")
        if not f:
            return jsonify(ok=False, error="no_file"), 400
        raw = f.read()
        try:
            url = upload_image(raw)
        except RuntimeError as e:  # noqa: BLE001
            if str(e) == "too_big":
                return jsonify(ok=False, error="too_big", size=len(raw)), 413
            return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=True, url=url)

    return app
