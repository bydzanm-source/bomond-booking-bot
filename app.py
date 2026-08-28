"""
Bomond booking bot — webhook-based Telegram bot (Flask + raw Bot API).

Flow: /start -> choose master -> choose service -> date/time -> name -> phone -> confirm -> forwarded to admin.

Also exposes POST /api/review (CORS-enabled for bomond.site) so the website
can let visitors leave a review that gets posted straight to ADMIN_CHAT_ID.

Env vars required:
  TELEGRAM_BOT_TOKEN  - bot token from @BotFather
  WEBHOOK_SECRET      - random path segment, keeps the webhook URL unguessable
  ADMIN_CHAT_ID       - chat id (or @channelusername) that receives booking
                        requests and site reviews. For a channel, the bot must
                        first be added there as admin with "Post Messages".
"""
import os
import time
import requests
from flask import Flask, request, jsonify

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hook")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
API = f"https://api.telegram.org/bot{TOKEN}"

# only these origins may call the public review API
ALLOWED_ORIGINS = {
    "https://bomond.site",
    "https://www.bomond.site",
    "https://bomond-salon.onrender.com",
}

app = Flask(__name__)

# in-memory per-chat conversation state (fine for a single small salon bot)
STATE = {}

# very small anti-spam guard for the public review endpoint: one submission
# per IP per 60s. Best-effort only (in-memory, resets on redeploy) — not a
# substitute for real abuse protection, but enough for a small salon site.
REVIEW_RATE_LIMIT = {}
REVIEW_MIN_INTERVAL = 60

MASTERS = [
    ("tatiana", "Татьяна Буева — главный колорист"),
    ("alena", "Алёна Сухарева — колорист, лешмейкер"),
    ("lina", "Лина Овчарова — колорист, бровист"),
    ("polina", "Полина Андреева — стилист-парикмахер"),
    ("any", "Не важно, подберите сами"),
]

SERVICES = [
    ("color", "Окрашивание и колористика"),
    ("cut", "Стрижка"),
    ("care", "Ботокс / кератин / холодное восстановление"),
    ("lam", "Ламинирование бровей и ресниц"),
    ("lash", "Наращивание ресниц"),
    ("pm", "Перманентный макияж"),
    ("braid", "Плетение кос и брейды"),
]

MASTER_NAMES = dict(MASTERS)
SERVICE_NAMES = dict(SERVICES)


def api(method, **payload):
    r = requests.post(f"{API}/{method}", json=payload, timeout=15)
    return r.json()


def send(chat_id, text, reply_markup=None, remove_keyboard=False):
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    elif remove_keyboard:
        kwargs["reply_markup"] = {"remove_keyboard": True}
    return api("sendMessage", **kwargs)


def inline(rows):
    return {"inline_keyboard": rows}


def master_keyboard():
    return inline([[{"text": name, "callback_data": f"m:{key}"}] for key, name in MASTERS])


def service_keyboard():
    return inline([[{"text": name, "callback_data": f"s:{key}"}] for key, name in SERVICES])


def confirm_keyboard():
    return inline([
        [{"text": "✅ Отправить заявку", "callback_data": "confirm"}],
        [{"text": "✏️ Начать заново", "callback_data": "restart"}],
    ])


def contact_keyboard():
    return {
        "keyboard": [[{"text": "📱 Отправить мой номер", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def start_flow(chat_id, first_name=""):
    STATE[chat_id] = {"step": "master"}
    greeting = f"Здравствуйте{', ' + first_name if first_name else ''}! ✨\n\n"
    send(
        chat_id,
        greeting
        + "Это запись в студию красоты <b>Bomond</b>.\n"
        + "К какому мастеру хотите записаться?",
        reply_markup=master_keyboard(),
    )


def summary_text(s):
    return (
        "Проверьте заявку:\n\n"
        f"👤 Мастер: <b>{MASTER_NAMES.get(s.get('master'), '—')}</b>\n"
        f"💅 Услуга: <b>{SERVICE_NAMES.get(s.get('service'), '—')}</b>\n"
        f"🗓 Желаемое время: <b>{s.get('datetime', '—')}</b>\n"
        f"🙋 Имя: <b>{s.get('name', '—')}</b>\n"
        f"📞 Телефон: <b>{s.get('phone', '—')}</b>"
    )


def handle_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    data = cq["data"]
    api("answerCallbackQuery", callback_query_id=cq["id"])
    s = STATE.setdefault(chat_id, {"step": "master"})

    if data == "restart":
        start_flow(chat_id, cq["from"].get("first_name", ""))
        return

    if data == "confirm":
        if s.get("step") != "confirm":
            return
        notify_admin(chat_id, cq["from"], s)
        send(
            chat_id,
            "Готово! 🎉 Заявка отправлена — администратор Bomond свяжется с вами в ближайшее"
            " время, чтобы подтвердить запись.\n\nЕсли что-то срочное — звоните: +7 906 718-17-68.",
            remove_keyboard=True,
        )
        STATE.pop(chat_id, None)
        return

    if data.startswith("m:") and s.get("step") == "master":
        s["master"] = data[2:]
        s["step"] = "service"
        send(chat_id, "Отлично! Какая услуга вас интересует?", reply_markup=service_keyboard())
        return

    if data.startswith("s:") and s.get("step") == "service":
        s["service"] = data[2:]
        s["step"] = "datetime"
        send(chat_id, "На какую дату и время вам удобно? Напишите в свободной форме — например «в субботу днём» или «12 сентября к 18:00».")
        return


def handle_text(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if text.startswith("/start"):
        start_flow(chat_id, msg["from"].get("first_name", ""))
        return

    if text == "/price":
        send(
            chat_id,
            "💰 <b>Ориентировочные цены</b>\n\n"
            "✂️ Стрижка — от 1000 ₽\n"
            "🎨 Окрашивание — от 2000 ₽\n"
            "💆‍♀️ Ботокс / кератин — от 2500 ₽\n"
            "👁 Ламинирование / коррекция бровей — от 900 ₽\n"
            "💋 Перманентный макияж — от 6000 ₽\n"
            "🪢 Плетение, брейды — от 1500 ₽\n\n"
            "Точная стоимость зависит от длины волос и квалификации мастера — "
            "полный прайс на сайте <a href=\"https://bomond.site/#price\">bomond.site</a>.\n\n"
            "Готовы записаться? Жмите /start",
        )
        return

    if text == "/promo":
        send(
            chat_id,
            "🎁 <b>Акции месяца</b>\n\n"
            "1️⃣ Окрашивание у Алёны и Лины — скидка 10% весь месяц\n"
            "2️⃣ Плетение и брейды — скидка 20% в начале месяца\n"
            "3️⃣ Криореконструкция волос — 500 ₽ вместо 1500 ₽\n"
            "4️⃣ Ламинирование ресниц — скидка 10% в конце месяца\n\n"
            "Точные даты и свободные окошки — в нашем канале "
            "<a href=\"https://t.me/bomondblh\">@bomondblh</a>.\n\n"
            "Записаться: /start",
        )
        return

    if text == "/contacts":
        send(
            chat_id,
            "📍 <b>Bomond — студия красоты</b>\n\n"
            "Московская область, Балашиха,\nшоссе Энтузиастов, 7/1\n\n"
            "🕙 Ежедневно 10:00–21:00, без выходных\n"
            "📞 <a href=\"tel:+79067181768\">+7 906 718-17-68</a>\n"
            "🌐 <a href=\"https://bomond.site\">bomond.site</a>\n\n"
            "Записаться: /start",
        )
        return

    if text == "/help":
        send(
            chat_id,
            "ℹ️ <b>Как пользоваться ботом</b>\n\n"
            "/start — записаться: выбрать мастера, услугу и удобное время\n"
            "/price — цены на популярные процедуры\n"
            "/promo — актуальные акции месяца\n"
            "/contacts — адрес, часы работы, телефон\n\n"
            "Можно просто написать вопрос текстом — мы его увидим и ответим лично.",
        )
        return

    if text == "/myid":
        send(chat_id, f"Ваш chat_id: <code>{chat_id}</code>")
        return

    s = STATE.get(chat_id)
    if not s:
        # no active flow — treat as a general question, forward to admin
        forward_general(chat_id, msg["from"], text)
        send(chat_id, "Спасибо! Мы получили ваше сообщение и скоро ответим.\n\nЧтобы записаться на процедуру, нажмите /start.")
        return

    step = s.get("step")

    if step == "datetime":
        s["datetime"] = text
        s["step"] = "name"
        send(chat_id, "Как вас зовут?")
        return

    if step == "name":
        s["name"] = text
        s["step"] = "phone"
        send(chat_id, "И контактный телефон — можно нажать кнопку ниже или написать вручную.", reply_markup=contact_keyboard())
        return

    if step == "phone":
        s["phone"] = text
        s["step"] = "confirm"
        send(chat_id, "Спасибо!", remove_keyboard=True)
        send(chat_id, summary_text(s), reply_markup=confirm_keyboard())
        return

    # already at confirm step and user typed instead of tapping a button
    if step == "confirm":
        send(chat_id, summary_text(s), reply_markup=confirm_keyboard())
        return

    start_flow(chat_id, msg["from"].get("first_name", ""))


def handle_contact(msg):
    chat_id = msg["chat"]["id"]
    s = STATE.get(chat_id)
    phone = msg["contact"].get("phone_number", "")
    if not phone.startswith("+"):
        phone = "+" + phone
    if s and s.get("step") == "phone":
        s["phone"] = phone
        s["step"] = "confirm"
        send(chat_id, "Спасибо!", remove_keyboard=True)
        send(chat_id, summary_text(s), reply_markup=confirm_keyboard())


def notify_admin(chat_id, user, s):
    if not ADMIN_CHAT_ID:
        return
    username = f"@{user['username']}" if user.get("username") else f"id{user['id']}"
    text = "🆕 Новая заявка на запись Bomond\n\n" + summary_text(s) + f"\n\nTelegram: {username}"
    send(ADMIN_CHAT_ID, text)


def forward_general(chat_id, user, text):
    if not ADMIN_CHAT_ID:
        return
    username = f"@{user['username']}" if user.get("username") else f"id{user['id']}"
    send(ADMIN_CHAT_ID, f"💬 Сообщение от {username} (chat {chat_id}):\n\n{text}")


@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            msg = update["message"]
            if "contact" in msg:
                handle_contact(msg)
            elif "text" in msg:
                handle_text(msg)
    except Exception as e:
        # never let a bad update crash the webhook response
        print("webhook error:", e)
    return jsonify(ok=True)


MASTER_NAMES_REVIEW = {
    "tatiana": "Татьяна Буева",
    "alena": "Алёна Сухарева",
    "lina": "Лина Овчарова",
    "polina": "Полина Андреева",
    "varvara": "Варвара Лунина (администратор)",
    "": "не указан",
}


@app.route("/api/review", methods=["POST", "OPTIONS"])
def submit_review():
    if request.method == "OPTIONS":
        return jsonify(ok=True)

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()
    last = REVIEW_RATE_LIMIT.get(ip)
    if last and now - last < REVIEW_MIN_INTERVAL:
        return jsonify(ok=False, error="too_many_requests"), 429

    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", "")).strip()[:100]
    text = str(data.get("text", "")).strip()[:2000]
    master_key = str(data.get("master", "")).strip()
    try:
        rating = int(data.get("rating", 0))
    except (TypeError, ValueError):
        rating = 0
    rating = max(1, min(5, rating)) if rating else 0

    if not name or not text or not rating:
        return jsonify(ok=False, error="missing_fields"), 400

    REVIEW_RATE_LIMIT[ip] = now

    master_name = MASTER_NAMES_REVIEW.get(master_key, "не указан")
    stars = "★" * rating + "☆" * (5 - rating)
    # plain text (no parse_mode) — user-submitted content is never trusted
    # with Telegram's HTML formatting to avoid any markup-injection surprises
    message = (
        "🌟 Новый отзыв с сайта Bomond\n\n"
        f"{stars}\n"
        f"Имя: {name}\n"
        f"Мастер: {master_name}\n\n"
        f"{text}"
    )
    if ADMIN_CHAT_ID:
        api("sendMessage", chat_id=ADMIN_CHAT_ID, text=message)

    return jsonify(ok=True)


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/")
def health():
    return "Bomond booking bot is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
