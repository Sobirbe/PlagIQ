"""Outgoing e-mail (sign-in codes, test e-mails, confirmation and recovery links of older accounts).

Two ways to send, set by the owner on the website service:
  * Resend's web API (HTTPS; works where the hosting blocks outgoing mail ports, as on Railway's smaller plans):
      RESEND_API_KEY (secret; a sending-only key is enough) and SMTP_FROM = a sender on a domain verified in
      Resend, e.g. "PlagIQ <codes@mail.example.uz>". Used whenever RESEND_API_KEY is set. Each message carries an
      idempotency key, and network errors, 429 and 5xx answers are retried with backoff (Retry-After respected);
      other refusals are not retried.
  * Brevo's web API (HTTPS; works where the hosting blocks outgoing mail ports, as on Railway's smaller plans):
      BREVO_API_KEY (secret) and SMTP_FROM = the sender verified in Brevo, e.g. "PlagIQ <no-reply@example.org>".
      Used whenever BREVO_API_KEY is set. Request: POST https://api.brevo.com/v3/smtp/email with header "api-key".
  * An SMTP server: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD (secret), SMTP_FROM,
      SMTP_SECURITY = starttls (default) | ssl | none
MAIL_BACKEND=dev is for development and tests only: nothing is e-mailed; messages are kept in memory (OUTBOX) and
written to the server log, so the sign-in code can be read there. Never set it on the public site.

Nothing is sent while these are missing; the site then says that recovery by e-mail is not available and points to
support. Messages contain only the link and plain instructions, never the user's documents.
"""
import json
import logging
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import parseaddr


OUTBOX = []                     # development backend: the last messages, newest last
log = logging.getLogger("plagiq.mail")


def dev():
    return os.environ.get("MAIL_BACKEND", "").strip().lower() == "dev"


RESEND_URL = "https://api.resend.com/emails"
RESEND_TRIES = 3


def resend():
    return bool(os.environ.get("RESEND_API_KEY", "").strip())


BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def brevo():
    return bool(os.environ.get("BREVO_API_KEY", "").strip())


def configured():
    sender = parseaddr(os.environ.get("SMTP_FROM", ""))[1]
    return bool(sender and (resend() or brevo() or os.environ.get("SMTP_HOST")))


def backend():
    """"resend", "brevo", "smtp", "dev" or "" (for the owner dashboard). Resend is used first, then Brevo, then SMTP."""
    if dev():
        return "dev"
    if not configured():
        return ""
    return "resend" if resend() else "brevo" if brevo() else "smtp"


class MailError(Exception):
    pass


def send(to, subject, text):
    if dev():
        OUTBOX.append({"to": to, "subject": subject, "text": text})
        del OUTBOX[:-50]
        log.warning("development e-mail backend (MAIL_BACKEND=dev), not sent: to=%s subject=%s\n%s", to, subject, text)
        return
    if not configured():
        raise MailError("mail_not_configured")
    if resend():
        return _send_resend(to, subject, text)
    if brevo():
        return _send_brevo(to, subject, text)
    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    host, security = os.environ["SMTP_HOST"], os.environ.get("SMTP_SECURITY", "starttls").lower()
    port = int(os.environ.get("SMTP_PORT") or (465 if security == "ssl" else 587))
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            if security == "starttls":
                server.starttls(context=context)
        with server:
            if os.environ.get("SMTP_USER"):
                server.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        # The reason (e.g. "SMTPAuthenticationError: 535 Username and Password not accepted" or "TimeoutError")
        # goes to the server log and to the owner's test on /admin; never the password, never to visitors.
        detail = f"{type(exc).__name__}: {str(exc)[:200]}".rstrip(": ")
        log.warning("e-mail not sent via %s:%s (%s): %s", host, port, security, detail)
        raise MailError(detail) from None


def _send_resend(to, subject, text, sleep=time.sleep):
    """POST /emails (resend.com/docs/api-reference/emails/send-email). One idempotency key for all retries of this
    message, so a retry after a lost answer never sends it twice."""
    body = json.dumps({"from": os.environ["SMTP_FROM"].strip(), "to": [to], "subject": subject, "text": text}).encode()
    headers = {"Authorization": "Bearer " + os.environ["RESEND_API_KEY"].strip(), "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": "PlagIQ", "Idempotency-Key": "plagiq-" + uuid.uuid4().hex}
    detail = ""
    for attempt in range(RESEND_TRIES):
        wait = 2 ** attempt
        try:
            with urllib.request.urlopen(urllib.request.Request(RESEND_URL, body, headers, method="POST"), timeout=20) as reply:
                answer = json.loads(reply.read() or b"{}")
            log.info("e-mail sent via Resend (id %s)", answer.get("id", "?"))
            return answer.get("id")
        except urllib.error.HTTPError as exc:
            try:                                 # Resend explains refusals: {"statusCode", "name", "message"}
                info = json.loads(exc.read() or b"{}")
                reason = f"{info.get('name', '')}: {info.get('message', '')}".strip(": ")[:200]
            except (ValueError, OSError):
                reason = ""
            detail = f"Resend HTTP {exc.code}" + (f" ({reason})" if reason else "")
            if exc.code != 429 and exc.code < 500:
                break                            # wrong key, unverified sender ...: retrying cannot help
            try:
                wait = max(wait, min(int(exc.headers.get("retry-after", "0")), 30))
            except (TypeError, ValueError):
                pass
        except (OSError, ValueError) as exc:
            detail = f"Resend unreachable: {type(exc).__name__}: {str(exc)[:200]}"
        if attempt < RESEND_TRIES - 1:
            sleep(wait)
    log.warning("e-mail not sent via Resend: %s", detail)
    raise MailError(detail)


def _send_brevo(to, subject, text):
    name, address = parseaddr(os.environ["SMTP_FROM"])
    body = json.dumps({"sender": {"name": name or "PlagIQ", "email": address}, "to": [{"email": to}],
                       "subject": subject, "textContent": text}).encode()
    request = urllib.request.Request(BREVO_URL, body, method="POST", headers={
        "api-key": os.environ["BREVO_API_KEY"].strip(), "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            if reply.status not in (200, 201, 202):
                raise MailError(f"Brevo HTTP {reply.status}")
    except urllib.error.HTTPError as exc:
        try:                                    # Brevo explains refusals, e.g. {"code": "unauthorized", "message": "..."}
            info = json.loads(exc.read() or b"{}")
            reason = f"{info.get('code', '')}: {info.get('message', '')}".strip(": ")[:200]
        except (ValueError, OSError):
            reason = ""
        detail = f"Brevo HTTP {exc.code}" + (f" ({reason})" if reason else "")
        log.warning("e-mail not sent via Brevo: %s", detail)
        raise MailError(detail) from None
    except (OSError, ValueError) as exc:
        detail = f"Brevo unreachable: {type(exc).__name__}: {str(exc)[:200]}"
        log.warning("e-mail not sent via Brevo: %s", detail)
        raise MailError(detail) from None
