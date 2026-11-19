""""Your check is ready": a short message when a check ends while its page is no longer open.

The page polls the check's progress every 1.2 s; when it has not asked for 15 s by the time the check ends (the tab
was closed, the phone screen went off, the browser was quit), the signed-in owner gets one message with a link to
"My checks". It goes by Telegram when the account signs in with Telegram (the sign-in asks Telegram for permission
to receive messages from the bot), otherwise, or if Telegram refuses, by e-mail to the account's address.

The message names the file and says whether the check finished; it contains no percentage, no text from the
document and no link to the report itself (the report opens only when signed in). CHECK_NOTIFY=0 turns it off.
"""
import logging
import os
import threading

import auth
import mailer
import progress
import reports_store
import uzcyr

log = logging.getLogger("plagiq.notify")

TEXTS = {
    "uz": {"subject_ok": "PlagIQ: «{f}» tekshiruvi tayyor", "subject_fail": "PlagIQ: «{f}» tekshiruvi yakunlanmadi",
           "ok": "«{f}» hujjatining tekshiruvi yakunlandi. Natijani «Mening tekshiruvlarim» bo‘limida ko‘ring:",
           "fail": "«{f}» hujjatining tekshiruvi yakunlanmadi. Sababi «Mening tekshiruvlarim» bo‘limida ko‘rsatilgan; "
                   "qayta urinib ko‘rishingiz mumkin:",
           "keep": "Hisobot hisobingizda {h} saqlanadi.", "button": "Natijani ochish",
           "why": "Bu xat tekshiruv tugaganda sahifa yopiq bo‘lgani uchun yuborildi."},
    "ru": {"subject_ok": "PlagIQ: проверка «{f}» готова", "subject_fail": "PlagIQ: проверка «{f}» не завершилась",
           "ok": "Проверка документа «{f}» завершена. Результат — в разделе «Мои проверки»:",
           "fail": "Проверка документа «{f}» не завершилась. Причина указана в разделе «Мои проверки»; "
                   "можно попробовать ещё раз:",
           "keep": "Отчёт хранится в аккаунте {h}.", "button": "Открыть результат",
           "why": "Это сообщение отправлено, потому что страница была закрыта, когда проверка закончилась."},
    "en": {"subject_ok": "PlagIQ: your check of “{f}” is ready", "subject_fail": "PlagIQ: the check of “{f}” did not finish",
           "ok": "The check of “{f}” has finished. See the result under “My checks”:",
           "fail": "The check of “{f}” did not finish. The reason is shown under “My checks”; you can try again:",
           "keep": "The report is kept in your account {h}.", "button": "Open the result",
           "why": "You received this because the page was closed when the check ended."},
}


def enabled():
    return os.environ.get("CHECK_NOTIFY", "1").strip() != "0"


def texts(lang):
    if lang == "uzc":
        return uzcyr.deep(TEXTS["uz"])
    return TEXTS.get(lang) or TEXTS["uz"]


def message(filename, ok, lang, origin):
    """(subject, body, link, button) in the page's language."""
    t, name = texts(lang), (filename or "Document")[:80]
    link = origin.rstrip("/") + "/#history"
    keep = reports_store.period(lang if lang in ("ru", "en") else "uz", True)     # signed in: the account's period
    if lang == "uzc":
        keep = uzcyr.to_cyrillic(keep)
    body = "\n\n".join([t["ok" if ok else "fail"].format(f=name) + "\n" + link,
                        t["keep"].format(h=keep), t["why"], "PlagIQ"])
    return t["subject_ok" if ok else "subject_fail"].format(f=name), body, link, t["button"]


def _telegram(chat_id, body, link, button, opener=None):
    import telegram_bot
    data = telegram_bot._call("sendMessage", {"chat_id": chat_id, "text": body, "disable_web_page_preview": True,
                                              "reply_markup": {"inline_keyboard": [[{"text": button, "url": link}]]}}
                              if link.startswith("https://") else
                              {"chat_id": chat_id, "text": body, "disable_web_page_preview": True}, opener)
    return bool(data.get("ok"))


def deliver(user, filename, ok, lang, origin, opener=None):
    """Send the message; returns "telegram", "email" or None. Never raises."""
    try:
        subject, body, link, button = message(filename, ok, lang, origin)
        found = auth.identities(user)
        if auth.telegram_enabled():
            for ident in found:
                if ident.startswith("telegram:"):
                    try:
                        if _telegram(int(ident[9:]), body, link, button, opener):
                            return "telegram"
                    except Exception as exc:          # blocked the bot, never started it, network
                        log.info("check notice not sent by Telegram: %s", type(exc).__name__)
        email = next((i[6:] for i in found if i.startswith("email:")), None)
        if email and (mailer.configured() or mailer.dev()):
            mailer.send(email, subject, body)
            return "email"
    except Exception as exc:
        log.warning("check notice not sent: %s", type(exc).__name__)
    return None


def after_check(user, token, disconnected, filename, ok, lang, origin):
    """Called when a check ends: sends the message in the background if the owner's page was not watching.
    Returns True when a message is being sent."""
    if not (enabled() and user):
        return False
    watching = progress.watching(token)
    if watching is None:
        watching = not disconnected
    if watching:
        return False
    _start(deliver, user, filename, ok, lang, origin)
    return True


def _start(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()
