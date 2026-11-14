"""Outgoing e-mail (account verification and password recovery) through the operator's SMTP server.

Off until the owner sets, on the website service:
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD (secret), SMTP_FROM (e.g. "PlagIQ <no-reply@example.org>"),
  SMTP_SECURITY = starttls (default) | ssl | none
Nothing is sent while these are missing; the site then says that recovery by e-mail is not available and points to
support. Messages contain only the link and plain instructions, never the user's documents.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr


def configured():
    return bool(os.environ.get("SMTP_HOST") and parseaddr(os.environ.get("SMTP_FROM", ""))[1])


class MailError(Exception):
    pass


def send(to, subject, text):
    if not configured():
        raise MailError("mail_not_configured")
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
        raise MailError(type(exc).__name__) from None
