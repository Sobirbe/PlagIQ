"""Replies from the PlagIQ Telegram bot.

Sign-in happens on the website ("Sign in with Telegram"), so the bot itself is not needed for it.
People who open the bot and press /start would otherwise get no answer; with this switched on,
the bot replies (in Uzbek, Russian or English, from the user's Telegram language) with how to
sign in and a button that opens the site.

Settings (website service):
  TELEGRAM_BOT_REPLIES=1   turn replies on. At start-up the site registers its webhook
                           (PUBLIC_ORIGIN + /telegram/webhook) with Telegram's setWebhook,
                           using a secret token derived from the bot token. Needs PUBLIC_ORIGIN (https).
The bot only answers messages; it stores nothing and logs no message text.
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
    "uz": ("Assalomu alaykum! PlagIQ — ilmiy ishlarni plagiatga tekshirish xizmati.\n\n"
           "Kirish uchun botga yozish shart emas: saytni oching, «Telegram orqali kirish» tugmasini bosing "
           "va ochilgan oynada Telegram orqali tasdiqlang. Shundan keyin hujjatni yuklab tekshirishingiz mumkin.",
           "PlagIQ saytini ochish"),
    "ru": ("Здравствуйте! PlagIQ — сервис проверки научных работ на плагиат.\n\n"
           "Писать боту для входа не нужно: откройте сайт, нажмите «Войти через Telegram» и подтвердите вход "
           "в открывшемся окне Telegram. После этого можно загрузить документ и проверить его.",
           "Открыть сайт PlagIQ"),
    "en": ("Hello! PlagIQ checks academic writing for plagiarism.\n\n"
           "You do not need to message this bot to sign in: open the website, press “Sign in with Telegram” "
           "and confirm in the Telegram window that opens. Then upload your document and run the check.",
           "Open PlagIQ"),
}


def origin():
    return os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")


def enabled():
    return auth.enabled() and os.environ.get("TELEGRAM_BOT_REPLIES", "") == "1" and origin().startswith("https://")


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
    """The sendMessage payload for an incoming update, or None (only private text messages get an answer)."""
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return None
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or not isinstance(chat.get("id"), int):
        return None
    code = str((message.get("from") or {}).get("language_code") or "")[:2]
    text, button = TEXTS.get(code, TEXTS["uz"])
    return {"chat_id": chat["id"], "text": text,
            "reply_markup": {"inline_keyboard": [[{"text": button, "url": origin() + "/"}]]}}


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
