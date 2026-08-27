"""
Bomond booking bot — webhook-based Telegram bot (Flask + raw Bot API).

Flow: /start -> choose master -> choose service -> date/time -> name -> phone -> confirm -> forwarded to admin.

Env vars required:
  TELEGRAM_BOT_TOKEN  - bot token from @BotFather
  WEBHOOK_SECRET      - random path segment, keeps the webhook URL unguessable
  ADMIN_CHAT_ID       - optional; telegram chat id that receives finished booking requests
"""
import os
import re
import requests
from flask import Flask, request, jsonify

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hook")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
API = f"https://api.telegram.org/bot{TOKEN}"

app = Flask(__name__)

# in-memory per-chat conversation state (fine for a single small salon bot)
STATE = {}

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

    if text == "/start":
        start_flow(chat_id, msg["from"].get("first_name", ""))
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


@app.route("/")
def health():
    return "Bomond booking bot is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
