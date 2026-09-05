"""
Bomond booking bot — webhook-based Telegram bot (Flask + raw Bot API).

Flow: /start -> choose master -> choose service -> date/time -> name -> phone -> confirm -> forwarded to admin.

Also:
  * POST /api/review          — CORS-enabled review form endpoint for bomond.site.
  * GET  /cabinet             — client "личный кабинет" (Telegram Mini App page).
  * POST /api/cabinet/session — validates Telegram initData, returns the client's card
                                (data imported from the DIKIDI client export), photos, referral link.
  * GET  /api/cabinet/photo/<id> — streams a before/after photo stored as a Telegram file_id.
  * POST /api/admin/import    — upload a DIKIDI clients CSV (admin secret) — same as sending the
                                file to the bot as an admin.

Env vars:
  TELEGRAM_BOT_TOKEN  - bot token from @BotFather
  WEBHOOK_SECRET      - random path segment, keeps the webhook URL unguessable
  ADMIN_CHAT_ID       - chat id (or @channelusername) that receives booking requests and reviews
  DATABASE_URL        - Postgres connection string (cabinet features are disabled without it)
  ADMIN_SECRET        - password for `/admin <secret>` (registers a Telegram user as staff)
  CABINET_URL         - public URL of the cabinet page (default: this service's /cabinet)
"""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl

import requests
from flask import Flask, Response, jsonify, request, send_file

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # keeps the bot bootable even if the driver is missing
    psycopg = None

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hook")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
CABINET_URL = os.environ.get("CABINET_URL", "https://bomond-booking-bot.onrender.com/cabinet")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Bomond_Hostess_bot")
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"

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
# per IP per 60s. Best-effort only (in-memory, resets on redeploy).
REVIEW_RATE_LIMIT = {}
REVIEW_MIN_INTERVAL = 60

MASTERS = [
    ("tatiana", "Татьяна Буева — главный колорист"),
    ("alena", "Алёна Сухарева — колорист, лешмейкер"),
    ("lina", "Лина Овчарова — колорист, бровист"),
    ("polina", "Полина Андреева — мастер по реконструкции волос"),
    ("margarita", "Маргарита Манина — мастер-брейдер"),
    ("any", "Не важно, подберите сами"),
]

SERVICES = [
    ("hair", "Парикмахерский зал (стрижка, окрашивание, укладка)"),
    ("brows", "Зона бровей и ресниц"),
    ("lash", "Наращивание ресниц"),
    ("pm", "Перманентный макияж"),
    ("care", "Уход для волос"),
    ("braid", "Плетение кос, брейдов, дредов"),
    ("makeup", "Макияж, причёски"),
]

MASTER_NAMES = dict(MASTERS)
SERVICE_NAMES = dict(SERVICES)


# ---------------------------------------------------------------- telegram helpers

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


def cabinet_button_row():
    return [{"text": "👤 Личный кабинет", "web_app": {"url": CABINET_URL}}]


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


# ---------------------------------------------------------------- database

def db():
    if not (DATABASE_URL and psycopg):
        raise RuntimeError("database not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


SCHEMA = """
create table if not exists clients (
  phone text primary key,
  first_name text, last_name text, email text,
  discount numeric, card_number text, spent numeric, avg_check numeric,
  account_balance numeric, bonus_balance numeric, visits_count integer,
  last_visit date, birthday date, gender text, blacklisted boolean default false,
  comment text, source text, extra jsonb default '{}'::jsonb,
  imported_at timestamptz default now()
);
create table if not exists tg_links (
  tg_user_id bigint primary key,
  phone text not null,
  first_name text, username text,
  referral_code text unique,
  referred_by text,
  linked_at timestamptz default now()
);
create index if not exists tg_links_phone_idx on tg_links(phone);
create table if not exists photos (
  id serial primary key,
  phone text not null,
  kind text not null check (kind in ('before','after')),
  file_id text not null,
  file_unique_id text,
  taken_on date not null default current_date,
  comment text,
  uploaded_by bigint,
  created_at timestamptz default now()
);
create index if not exists photos_phone_idx on photos(phone, taken_on);
create table if not exists admins (
  tg_user_id bigint primary key,
  name text,
  added_at timestamptz default now()
);
create table if not exists referrals (
  id serial primary key,
  referrer_code text not null,
  referred_tg_user_id bigint unique,
  referred_phone text,
  created_at timestamptz default now()
);
create table if not exists imports (
  id serial primary key,
  filename text, rows_total integer, rows_upserted integer,
  uploaded_by bigint, created_at timestamptz default now()
);
"""


def init_db():
    if not (DATABASE_URL and psycopg):
        print("DATABASE_URL not set — cabinet features disabled")
        return
    try:
        with db() as conn:
            conn.execute(SCHEMA)
        print("database schema ready")
    except Exception as e:  # noqa: BLE001
        print("database init error:", e)


# ---------------------------------------------------------------- phone / parsing helpers

def normalize_phone(raw):
    """'+7 (906) 718-17-68' / '89067181768' / '9067181768' -> '79067181768' (digits only)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else ""


def display_phone(digits):
    if len(digits) == 11 and digits[0] == "7":
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return "+" + digits if digits else ""


def parse_number(s):
    s = str(s or "").replace("\xa0", "").replace(" ", "").replace("%", "").replace(",", ".").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(s):
    s = str(s or "").strip()
    if not s:
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_bool(s):
    return str(s or "").strip().lower() in ("да", "yes", "1", "true", "+")


# DIKIDI "Список клиентов" export columns -> our fields
CSV_MAP = {
    "Имя клиента": "first_name",
    "Фамилия клиента": "last_name",
    "Мобильный номер": "phone",
    "Электронная почта": "email",
    "Скидка, %": "discount",
    "Номер карты": "card_number",
    "Потрачено": "spent",
    "Средний чек": "avg_check",
    "Лицевой счет": "account_balance",
    "Бонусный счет": "bonus_balance",
    "Количество записей": "visits_count",
    "Последний визит": "last_visit",
    "День рождения": "birthday",
    "Пол": "gender",
    "В черном списке": "blacklisted",
    "Комментарий": "comment",
    "Источник": "source",
}


def import_clients_csv(data: bytes, filename="clients.csv", uploaded_by=None):
    """Upsert DIKIDI clients export into `clients`. Returns (rows_total, rows_upserted)."""
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("unknown encoding")
    sample = text[:2000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames or "Мобильный номер" not in [h.strip() for h in reader.fieldnames]:
        raise ValueError("Это не похоже на экспорт клиентов DIKIDI (нет колонки «Мобильный номер»)")

    total = upserted = 0
    with db() as conn:
        for row in reader:
            total += 1
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            phone = normalize_phone(row.get("Мобильный номер"))
            if not phone:
                continue
            known = {CSV_MAP[k]: v for k, v in row.items() if k in CSV_MAP}
            extra = {k: v for k, v in row.items() if k not in CSV_MAP and v}
            conn.execute(
                """
                insert into clients (phone, first_name, last_name, email, discount, card_number, spent, avg_check,
                                     account_balance, bonus_balance, visits_count, last_visit, birthday, gender,
                                     blacklisted, comment, source, extra, imported_at)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                on conflict (phone) do update set
                  first_name=excluded.first_name, last_name=excluded.last_name, email=excluded.email,
                  discount=excluded.discount, card_number=excluded.card_number, spent=excluded.spent,
                  avg_check=excluded.avg_check, account_balance=excluded.account_balance,
                  bonus_balance=excluded.bonus_balance, visits_count=excluded.visits_count,
                  last_visit=excluded.last_visit, birthday=excluded.birthday, gender=excluded.gender,
                  blacklisted=excluded.blacklisted, comment=excluded.comment, source=excluded.source,
                  extra=excluded.extra, imported_at=now()
                """,
                (
                    phone,
                    known.get("first_name") or None,
                    known.get("last_name") or None,
                    known.get("email") or None,
                    parse_number(known.get("discount")),
                    known.get("card_number") or None,
                    parse_number(known.get("spent")),
                    parse_number(known.get("avg_check")),
                    parse_number(known.get("account_balance")),
                    parse_number(known.get("bonus_balance")),
                    int(parse_number(known.get("visits_count")) or 0),
                    parse_date(known.get("last_visit")),
                    parse_date(known.get("birthday")),
                    known.get("gender") or None,
                    parse_bool(known.get("blacklisted")),
                    known.get("comment") or None,
                    known.get("source") or None,
                    json.dumps(extra, ensure_ascii=False),
                ),
            )
            upserted += 1
        conn.execute(
            "insert into imports (filename, rows_total, rows_upserted, uploaded_by) values (%s,%s,%s,%s)",
            (filename, total, upserted, uploaded_by),
        )
    return total, upserted


# ---------------------------------------------------------------- admins / links / referrals

def is_admin(user_id):
    try:
        with db() as conn:
            return conn.execute("select 1 from admins where tg_user_id=%s", (user_id,)).fetchone() is not None
    except Exception:  # noqa: BLE001
        return False


def new_referral_code():
    return secrets.token_urlsafe(5).replace("-", "x").replace("_", "y")[:7].upper()


def link_phone(user, phone):
    """Store verified (Telegram-provided) phone for this Telegram user. Returns the link row."""
    with db() as conn:
        row = conn.execute("select * from tg_links where tg_user_id=%s", (user["id"],)).fetchone()
        if row:
            conn.execute(
                "update tg_links set phone=%s, first_name=%s, username=%s where tg_user_id=%s",
                (phone, user.get("first_name"), user.get("username"), user["id"]),
            )
            row["phone"] = phone
            return row
        code = new_referral_code()
        conn.execute(
            "insert into tg_links (tg_user_id, phone, first_name, username, referral_code) values (%s,%s,%s,%s,%s)",
            (user["id"], phone, user.get("first_name"), user.get("username"), code),
        )
        # attach the phone to a pending referral, if any
        conn.execute("update referrals set referred_phone=%s where referred_tg_user_id=%s", (phone, user["id"]))
        return conn.execute("select * from tg_links where tg_user_id=%s", (user["id"],)).fetchone()


def register_referral(user, code):
    """Remember that `user` came via referral `code`. Returns referrer link row or None."""
    with db() as conn:
        ref = conn.execute("select * from tg_links where referral_code=%s", (code,)).fetchone()
        if not ref or ref["tg_user_id"] == user["id"]:
            return None
        already = conn.execute("select 1 from tg_links where tg_user_id=%s", (user["id"],)).fetchone()
        if already:
            return None  # existing clients can't be "invited"
        conn.execute(
            "insert into referrals (referrer_code, referred_tg_user_id) values (%s,%s) on conflict (referred_tg_user_id) do nothing",
            (code, user["id"]),
        )
        return ref


# ---------------------------------------------------------------- bot flows

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


def send_cabinet(chat_id, intro=None):
    text = intro or (
        "👤 <b>Личный кабинет Bomond</b>\n\n"
        "Ваши визиты, скидка и бонусы, фото «до/после» и реферальная ссылка для подруг — всё в одном месте."
    )
    send(chat_id, text, reply_markup=inline([cabinet_button_row()]))


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


def handle_start(msg, payload):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    if payload == "cabinet":
        send_cabinet(chat_id)
        return
    if payload.startswith("ref_"):
        try:
            ref = register_referral(user, payload[4:].upper())
        except Exception as e:  # noqa: BLE001
            print("referral error:", e)
            ref = None
        if ref:
            who = ref.get("first_name") or "подруга"
            send(
                chat_id,
                f"🎀 Вы пришли по приглашению — {who} рекомендует Bomond!\n\n"
                "Вам — <b>скидка 15% на первый визит</b>, а подруге — бонусы на счёт. "
                "Скидку применит администратор при записи, просто скажите, что вы по приглашению.",
            )
            if ADMIN_CHAT_ID:
                uname = f"@{user['username']}" if user.get("username") else f"id{user['id']}"
                send(ADMIN_CHAT_ID, f"🎀 Новый гость по реферальной ссылке {ref['referral_code']} ({who}): {uname}. Скидка 15% на первый визит.")
    start_flow(chat_id, user.get("first_name", ""))


PROMO_TEXT = (
    "🎁 <b>Супер акция сентября</b>\n\n"
    "1️⃣ Запишитесь в сентябре онлайн\n"
    "2️⃣ Приходите в студию красоты Bomond\n"
    "3️⃣ После процедуры — беспроигрышная лотерея с подарками\n\n"
    "🎀 <b>Приведи подругу</b> (постоянно): подруге — скидка 15% на первый визит, "
    "вам — 10% бонусами. Ваша ссылка для приглашения — в личном кабинете.\n\n"
    "Подробности — в канале <a href=\"https://t.me/bomondblh\">@bomondblh</a>.\n\n"
    "Записаться: /start · Кабинет: /cabinet"
)


def handle_text(msg):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    text = (msg.get("text") or "").strip()

    if text.startswith("/start"):
        handle_start(msg, text[6:].strip())
        return

    if text.startswith("/cabinet"):
        send_cabinet(chat_id)
        return

    if text.startswith("/admin"):
        secret = text[6:].strip()
        if ADMIN_SECRET and secret and hmac.compare_digest(secret, ADMIN_SECRET):
            try:
                with db() as conn:
                    conn.execute(
                        "insert into admins (tg_user_id, name) values (%s,%s) on conflict (tg_user_id) do update set name=excluded.name",
                        (user["id"], (user.get("first_name") or "") + " " + (user.get("last_name") or "")),
                    )
                send(
                    chat_id,
                    "✅ Вы зарегистрированы как сотрудник Bomond.\n\n"
                    "📎 Отправьте боту файл экспорта клиентов из DIKIDI (CSV) — база обновится.\n"
                    "📷 Отправьте фото с подписью <code>+7 9xx xxx-xx-xx до</code> или <code>… после</code> "
                    "(можно добавить комментарий: <code>+79067181768 после окрашивание, Татьяна</code>) — фото появится в кабинете клиента.",
                )
            except Exception as e:  # noqa: BLE001
                send(chat_id, f"Не удалось сохранить: {e}")
        else:
            send(chat_id, "Неверный код.")
        return

    if text == "/price":
        send(
            chat_id,
            "💰 <b>Ориентировочные цены</b>\n\n"
            "✂️ Стрижка — от 400 ₽ (чёлка) / женская от 1500 ₽\n"
            "🎨 Окрашивание — от 2600 ₽\n"
            "💆‍♀️ Уход для волос — от 1000 ₽\n"
            "👁 Брови и ресницы — от 700 ₽\n"
            "👀 Наращивание ресниц — от 2500 ₽\n"
            "💋 Перманентный макияж — от 3000 ₽\n"
            "🪢 Плетение, брейды — от 1500 ₽\n"
            "💄 Макияж — от 4000 ₽\n\n"
            "Полный прайс с ценами «Мастер / ТОП-мастер» — на сайте "
            "<a href=\"https://bomond.site/#price\">bomond.site</a>.\n\n"
            "Готовы записаться? Жмите /start",
        )
        return

    if text == "/promo":
        send(chat_id, PROMO_TEXT)
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
            "/cabinet — личный кабинет: визиты, бонусы, фото до/после, ссылка для подруг\n"
            "/price — цены на популярные процедуры\n"
            "/promo — актуальные акции\n"
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
        forward_general(chat_id, user, text)
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

    if step == "confirm":
        send(chat_id, summary_text(s), reply_markup=confirm_keyboard())
        return

    start_flow(chat_id, user.get("first_name", ""))


def handle_contact(msg):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    contact = msg["contact"]
    phone_raw = contact.get("phone_number", "")
    phone = normalize_phone(phone_raw)
    s = STATE.get(chat_id)

    # A contact that belongs to the sender is verified by Telegram -> link it to the account
    # (this is also how the cabinet's "Подтвердить номер" button arrives).
    linked = False
    if phone and contact.get("user_id") == user["id"]:
        try:
            link_phone(user, phone)
            linked = True
        except Exception as e:  # noqa: BLE001
            print("link error:", e)

    if s and s.get("step") == "phone":
        s["phone"] = display_phone(phone) if phone else phone_raw
        s["step"] = "confirm"
        send(chat_id, "Спасибо!", remove_keyboard=True)
        send(chat_id, summary_text(s), reply_markup=confirm_keyboard())
        return

    if linked:
        send_cabinet(chat_id, "✅ Номер подтверждён. Ваш кабинет открыт — нажмите кнопку ниже.")


PHOTO_CAPTION_RE = re.compile(r"(?P<phone>\+?[\d\s\-\(\)]{10,20})\s*(?P<kind>до|после|before|after)\b\s*(?P<rest>.*)", re.I | re.S)


def handle_photo(msg):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    if not is_admin(user["id"]):
        # a client sending a photo — treat as a general message
        forward_general(chat_id, user, "[фото]")
        send(chat_id, "Спасибо! Мы получили ваше сообщение и скоро ответим.")
        return
    caption = (msg.get("caption") or "").strip()
    m = PHOTO_CAPTION_RE.match(caption)
    if not m:
        send(chat_id, "Подпишите фото так: <code>+7 9xx xxx-xx-xx до</code> или <code>+7 9xx xxx-xx-xx после</code> (дальше можно комментарий).")
        return
    phone = normalize_phone(m.group("phone"))
    kind = "before" if m.group("kind").lower() in ("до", "before") else "after"
    comment = m.group("rest").strip() or None
    best = max(msg["photo"], key=lambda p: p.get("file_size") or 0)  # largest size
    with db() as conn:
        conn.execute(
            "insert into photos (phone, kind, file_id, file_unique_id, comment, uploaded_by) values (%s,%s,%s,%s,%s,%s)",
            (phone, kind, best["file_id"], best.get("file_unique_id"), comment, user["id"]),
        )
        client = conn.execute("select first_name, last_name from clients where phone=%s", (phone,)).fetchone()
    who = " ".join(filter(None, [client and client["first_name"], client and client["last_name"]])) if client else "клиент пока не найден в базе"
    send(chat_id, f"📷 Сохранено: фото <b>{'ДО' if kind == 'before' else 'ПОСЛЕ'}</b> для {display_phone(phone)} ({who}).")


def handle_document(msg):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    doc = msg["document"]
    name = (doc.get("file_name") or "").lower()
    if not is_admin(user["id"]):
        forward_general(chat_id, user, f"[файл {name}]")
        send(chat_id, "Спасибо! Мы получили ваше сообщение и скоро ответим.")
        return
    if not name.endswith(".csv"):
        send(chat_id, "Пришлите экспорт клиентов из DIKIDI в формате CSV (Клиенты → Экспорт → CSV).")
        return
    try:
        info = api("getFile", file_id=doc["file_id"])
        path = info["result"]["file_path"]
        data = requests.get(f"{FILE_API}/{path}", timeout=60).content
        total, upserted = import_clients_csv(data, filename=name, uploaded_by=user["id"])
        send(chat_id, f"✅ База обновлена: строк в файле {total}, загружено клиентов {upserted}.")
    except Exception as e:  # noqa: BLE001
        send(chat_id, f"Не удалось импортировать: {e}")


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
            if msg.get("chat", {}).get("type") != "private":
                return jsonify(ok=True)  # ignore group/channel chatter
            if "contact" in msg:
                handle_contact(msg)
            elif "photo" in msg:
                handle_photo(msg)
            elif "document" in msg:
                handle_document(msg)
            elif "text" in msg:
                handle_text(msg)
    except Exception as e:  # noqa: BLE001
        # never let a bad update crash the webhook response
        print("webhook error:", e)
    return jsonify(ok=True)


# ---------------------------------------------------------------- reviews (site form)

MASTER_NAMES_REVIEW = {
    "tatiana": "Татьяна Буева",
    "alena": "Алёна Сухарева",
    "lina": "Лина Овчарова",
    "polina": "Полина Андреева",
    "margarita": "Маргарита Манина",
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
    # plain text (no parse_mode) — user-submitted content is never trusted with HTML
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


# ---------------------------------------------------------------- cabinet (Telegram Mini App)

INIT_DATA_MAX_AGE = 24 * 3600


def verify_init_data(init_data: str):
    """Validate Telegram WebApp initData (HMAC-SHA256 per Telegram docs). Returns user dict or None."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
        return json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError):
        return None


def photo_token(tg_user_id, ttl=12 * 3600):
    exp = int(time.time()) + ttl
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{tg_user_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{tg_user_id}.{exp}.{sig}"


def verify_photo_token(token):
    try:
        uid, exp, sig = token.split(".")
        if int(exp) < time.time():
            return None
        good = hmac.new(WEBHOOK_SECRET.encode(), f"{uid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return int(uid) if hmac.compare_digest(good, sig) else None
    except (ValueError, AttributeError):
        return None


def client_payload(conn, link):
    phone = link["phone"]
    client = conn.execute("select * from clients where phone=%s", (phone,)).fetchone()
    photos = conn.execute(
        "select id, kind, taken_on, comment from photos where phone=%s order by taken_on desc, id desc", (phone,)
    ).fetchall()
    # group photos into before/after pairs by day
    days = {}
    for p in photos:
        d = days.setdefault(p["taken_on"], {"date": p["taken_on"].isoformat(), "before": None, "after": None, "comment": None})
        if not d[p["kind"]]:
            d[p["kind"]] = {"id": p["id"]}
        d["comment"] = d["comment"] or p["comment"]
    pairs = [days[k] for k in sorted(days.keys(), reverse=True)]
    refs = conn.execute("select count(*) as n from referrals where referrer_code=%s", (link["referral_code"],)).fetchone()["n"]

    card = None
    if client:
        name = " ".join(filter(None, [client["first_name"], client["last_name"]])) or None
        card = {
            "name": name,
            "visits_count": client["visits_count"],
            "total_spent": float(client["spent"]) if client["spent"] is not None else None,
            "discount": float(client["discount"]) if client["discount"] is not None else None,
            "bonus": float(client["bonus_balance"]) if client["bonus_balance"] is not None else None,
            "last_visit": client["last_visit"].isoformat() if client["last_visit"] else None,
            "birthday": client["birthday"].isoformat() if client["birthday"] else None,
            "category": client["source"],
        }
    return {
        "ok": True,
        "linked": True,
        "phone": phone,
        "phone_display": display_phone(phone),
        "client": card,
        "visits": [],
        "photo_pairs": pairs,
        "photo_token": photo_token(link["tg_user_id"]),
        "referral_code": link["referral_code"],
        "referral_link": f"https://t.me/{BOT_USERNAME}?start=ref_{link['referral_code']}",
        "referrals_count": refs,
    }


@app.route("/cabinet")
def cabinet_page():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cabinet.html"))


@app.route("/api/cabinet/session", methods=["POST"])
def cabinet_session():
    data = request.get_json(force=True, silent=True) or {}
    user = verify_init_data(str(data.get("initData", "")))
    if not user or not user.get("id"):
        return jsonify(ok=False, error="bad_signature"), 401
    if not DATABASE_URL:
        return jsonify(ok=False, error="db_unavailable"), 503
    try:
        with db() as conn:
            link = conn.execute("select * from tg_links where tg_user_id=%s", (user["id"],)).fetchone()
            if not link:
                return jsonify(ok=True, linked=False, telegram={"id": user["id"], "first_name": user.get("first_name")})
            if not link.get("referral_code"):
                code = new_referral_code()
                conn.execute("update tg_links set referral_code=%s where tg_user_id=%s", (code, user["id"]))
                link["referral_code"] = code
            payload = client_payload(conn, link)
    except Exception as e:  # noqa: BLE001
        print("cabinet error:", e)
        return jsonify(ok=False, error="server_error"), 500
    payload["telegram"] = {"id": user["id"], "first_name": user.get("first_name")}
    return jsonify(payload)


FILE_PATH_CACHE = {}


@app.route("/api/cabinet/photo/<int:photo_id>")
def cabinet_photo(photo_id):
    uid = verify_photo_token(request.args.get("t", ""))
    if not uid:
        return Response("forbidden", status=403)
    try:
        with db() as conn:
            row = conn.execute(
                "select p.file_id from photos p join tg_links l on l.phone = p.phone where p.id=%s and l.tg_user_id=%s",
                (photo_id, uid),
            ).fetchone()
    except Exception as e:  # noqa: BLE001
        print("photo error:", e)
        return Response("error", status=500)
    if not row:
        return Response("not found", status=404)
    cached = FILE_PATH_CACHE.get(row["file_id"])
    if not cached or cached[1] < time.time():
        info = api("getFile", file_id=row["file_id"])
        if not info.get("ok"):
            return Response("unavailable", status=502)
        cached = (info["result"]["file_path"], time.time() + 3000)
        FILE_PATH_CACHE[row["file_id"]] = cached
    upstream = requests.get(f"{FILE_API}/{cached[0]}", timeout=30)
    resp = Response(upstream.content, status=upstream.status_code, content_type=upstream.headers.get("Content-Type", "image/jpeg"))
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.route("/api/admin/import", methods=["POST"])
def admin_import():
    token = request.headers.get("X-Admin-Secret") or request.args.get("token", "")
    if not (ADMIN_SECRET and token and hmac.compare_digest(token, ADMIN_SECRET)):
        return jsonify(ok=False, error="forbidden"), 403
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False, error="no_file"), 400
    try:
        total, upserted = import_clients_csv(f.read(), filename=f.filename or "upload.csv")
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, rows_total=total, rows_upserted=upserted)


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS and request.path == "/api/review":
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/")
def health():
    return "Bomond booking bot is running."


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
