"""Replies from the PlagIQ Telegram bot.

With this switched on, the bot answers private messages (in Uzbek, Russian or English, from the
user's Telegram language): it explains how to sign in and offers a "share my number" button.
When the user shares their OWN number (Telegram's contact button; contact.user_id must equal the
sender), PlagIQ stores the number with the account and replies with a one-time sign-in link
(/auth/bot?t=..., valid 10 minutes).

Settings (website service):
  TELEGRAM_BOT_REPLIES=1   turn replies on. At start-up the site registers its webhook
                           (PUBLIC_ORIGIN + /telegram/webhook) with Telegram's setWebhook,
                           using a secret token derived from the bot token. Needs PUBLIC_ORIGIN (https).
The bot logs no message text; it stores only the phone number a user chooses to share.
"""
import hashlib
import hmac
import json
import logging
import os
import urllib.request

import auth

API = "https://api.telegram.org"
log = logging.getLogger("plagiq.telegram")
STATUS = {"ok": None, "detail": "not registered yet"}        # last setWebhook result, for /admin

TEXTS = {
    "uz": {"hello": "Assalomu alaykum! PlagIQ — ilmiy ishlarni plagiatga tekshirish xizmati.\n\n"
                    "Telefon raqamingiz orqali kirish uchun pastdagi «📱 Raqamni yuborish» tugmasini bosing — "
                    "men sizga saytga kirish havolasini yuboraman.\n\nYoki saytda «Telegram orqali kirish» tugmasini "
                    "bosing: {site}\n1 000 so‘zgacha bo‘lgan matnni ro‘yxatdan o‘tmasdan ham tekshirish mumkin.",
           "share": "📱 Raqamni yuborish",
           "link": "Raqamingiz qabul qilindi: {phone}. Saytga kirish uchun tugmani bosing. Havola bir marta ishlaydi va "
                   "{m} daqiqa amal qiladi.",
           "open": "PlagIQ’ga kirish",
           "own": "Iltimos, o‘zingizning raqamingizni «📱 Raqamni yuborish» tugmasi orqali yuboring."},
    "ru": {"hello": "Здравствуйте! PlagIQ — сервис проверки научных работ на плагиат.\n\n"
                    "Чтобы войти по номеру телефона, нажмите кнопку «📱 Отправить номер» внизу — я пришлю ссылку для "
                    "входа на сайт.\n\nИли нажмите «Войти через Telegram» на сайте: {site}\n"
                    "Текст до 1 000 слов можно проверить и без регистрации.",
           "share": "📱 Отправить номер",
           "link": "Номер получен: {phone}. Нажмите кнопку, чтобы войти на сайт. Ссылка одноразовая и действует {m} минут.",
           "open": "Войти в PlagIQ",
           "own": "Пожалуйста, отправьте свой собственный номер кнопкой «📱 Отправить номер»."},
    "en": {"hello": "Hello! PlagIQ checks academic writing for plagiarism.\n\n"
                    "To sign in with your phone number, press “📱 Share my number” below and I will send you a sign-in "
                    "link for the website.\n\nOr press “Sign in with Telegram” on the website: {site}\n"
                    "Texts up to 1,000 words can be checked without signing in.",
           "share": "📱 Share my number",
           "link": "Number received: {phone}. Press the button to sign in on the website. The link works once and is "
                   "valid for {m} minutes.",
           "open": "Sign in to PlagIQ",
           "own": "Please send your own number with the “📱 Share my number” button."},
}


def origin():
    return os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")


def enabled():
    return auth.telegram_enabled() and os.environ.get("TELEGRAM_BOT_REPLIES", "") == "1" and origin().startswith("https://")


def secret():
    """The secret_token Telegram sends back in X-Telegram-Bot-Api-Secret-Token (1-256 chars of A-Z a-z 0-9 _ -)."""
    return hmac.new(auth.bot_token().encode(), b"plagiq-telegram-webhook", hashlib.sha256).hexdigest()


def authorised(header):
    return enabled() and bool(header) and hmac.compare_digest(header.encode(), secret().encode())


def _call(method, payload, opener=None):
    request = urllib.request.Request(f"{API}/bot{auth.bot_token()}/{method}", json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
    with (opener or urllib.request.urlopen)(request, timeout=10) as response:
        return json.loads(response.read(65536))


def reply_for(update):
    """The sendMessage payload for an incoming update, or None (only private messages get an answer).
    A shared contact that is the sender's own number signs them in: the reply carries a one-time link."""
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return None
    chat, sender = message.get("chat") or {}, message.get("from") or {}
    if chat.get("type") != "private" or not isinstance(chat.get("id"), int) or not isinstance(sender.get("id"), int):
        return None
    t = TEXTS.get(str(sender.get("language_code") or "")[:2], TEXTS["uz"])
    contact = message.get("contact")
    if isinstance(contact, dict):
        if contact.get("user_id") != sender["id"]:          # someone else's number proves nothing
            return {"chat_id": chat["id"], "text": t["own"], "reply_markup": _share_keyboard(t)}
        phone = "+" + str(contact.get("phone_number", "")).lstrip("+")[:20]
        name = " ".join(x for x in (sender.get("first_name", ""), sender.get("last_name", "")) if x).strip()
        user = {"id": sender["id"], "name": name[:120] or str(sender.get("username", ""))[:64] or str(sender["id"]),
                "username": str(sender.get("username", ""))[:64]}
        auth.remember(user, phone=phone)
        nonce = auth.login_link(user["id"])
        return {"chat_id": chat["id"], "text": t["link"].format(phone=phone, m=auth.LINK_MINUTES),
                "reply_markup": {"inline_keyboard": [[{"text": t["open"], "url": f"{origin()}/auth/bot?t={nonce}"}]]}}
    if not isinstance(message.get("text"), str):
        return None
    return {"chat_id": chat["id"], "text": t["hello"].format(site=origin() + "/"), "reply_markup": _share_keyboard(t)}


def _share_keyboard(t):
    return {"keyboard": [[{"text": t["share"], "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True}


def handle(update, opener=None):
    payload = reply_for(update)
    if not payload:
        return False
    try:
        _call("sendMessage", payload, opener)
        return True
    except (OSError, ValueError) as exc:
        log.warning("Telegram reply failed: %s", type(exc).__name__)
        return False


def register(opener=None):
    """Point the bot's webhook at this site. Returns (ok, description)."""
    if not enabled():
        return False, "off"
    try:
        result = _call("setWebhook", {"url": origin() + "/telegram/webhook", "secret_token": secret(),
                                      "allowed_updates": ["message"], "drop_pending_updates": True}, opener)
        outcome = bool(result.get("ok")), str(result.get("description", ""))[:200]
    except (OSError, ValueError) as exc:
        outcome = False, f"Telegram could not be reached ({type(exc).__name__})"
    STATUS.update(ok=outcome[0], detail=outcome[1])
    return outcome
