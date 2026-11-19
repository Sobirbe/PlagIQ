"""Operator and support details, set by the site owner (never invented by PlagIQ).

Website service variables:
  OPERATOR_NAME        legal name of the person or organisation operating PlagIQ
  OPERATOR_ADDRESS     postal address
  SUPPORT_EMAIL        e-mail address for support and privacy requests
  SUPPORT_TELEGRAM     Telegram username for support, e.g. @plagiq_support
  SUPPORT_HOURS        e.g. "Mon–Fri 9:00–18:00 (Tashkent)"
  PRIVACY_UPDATED      date the privacy notice was last reviewed, e.g. 2026-12-01
  PRIVACY_DATA_LOCATION_UZ / _RU / _EN   where personal data is stored, as approved by legal counsel
  PRIVACY_REVIEWED=1   legal counsel has reviewed the notice (removes the "to be confirmed" remarks and the draft notice)
Anything not set is shown as "to be completed by the operator", never as a guess. See docs/LEGAL-CHECKLIST.md.
"""
import html
import os
import re
from pathlib import Path

PENDING = {"uz": "operator tomonidan to‘ldiriladi", "ru": "будет заполнено оператором", "en": "to be completed by the operator"}
COUNSEL = {"uz": "(Huquqshunos tomonidan tasdiqlanishi kutilmoqda.)", "ru": "(Ожидает подтверждения юристом.)",
           "en": "(Awaiting confirmation by legal counsel.)"}
DRAFT = ("<p class=\"draft\"><b>Loyiha / Проект / Draft.</b> Ushbu bildirishnoma yakunlanmoqda: ayrim ma’lumotlar operator va "
         "huquqshunos ko‘rib chiqishini kutmoqda. · Уведомление дорабатывается: часть сведений ожидает оператора и юриста. · "
         "This notice is being finalised: some details are awaiting the operator and legal review.</p>")
FIELDS = ("OPERATOR_NAME", "OPERATOR_ADDRESS", "SUPPORT_EMAIL", "PRIVACY_UPDATED", "PRIVACY_DATA_LOCATION_UZ",
          "PRIVACY_DATA_LOCATION_RU", "PRIVACY_DATA_LOCATION_EN")


def value(name):
    return " ".join(os.environ.get(name, "").split())[:500]


def support():
    """Public support details (only what the owner has set)."""
    telegram = value("SUPPORT_TELEGRAM").lstrip("@")
    return {"email": value("SUPPORT_EMAIL"), "telegram": telegram if re.fullmatch(r"[A-Za-z0-9_]{4,32}", telegram) else "",
            "hours": value("SUPPORT_HOURS"), "operator": value("OPERATOR_NAME")}


def missing():
    """Fields the privacy notice still needs (for the owner dashboard and the draft notice)."""
    out = [f for f in FIELDS if not value(f)]
    if os.environ.get("PRIVACY_REVIEWED") != "1":
        out.append("PRIVACY_REVIEWED")
    if not (value("SUPPORT_EMAIL") or support()["telegram"]):
        out.append("SUPPORT_EMAIL or SUPPORT_TELEGRAM")
    return out


def privacy_html():
    page = Path(__file__).with_name("privacy.html").read_text("utf-8")
    sections = re.split(r'(<section id="(?:uz|ru|en)" lang="(?:uz|ru|en)">)', page)
    out, lang = [], "en"
    for part in sections:
        m = re.match(r'<section id="(uz|ru|en)"', part)
        if m:
            lang = m.group(1)
            out.append(part)
            continue
        out.append(_fill(part, lang))
    import reports_store
    return reports_store.fill("".join(out).replace("{{DRAFT_NOTICE}}", "" if not missing() else DRAFT))


def _fill(text, lang):
    pending = f'<span class="pending">[{PENDING[lang]}]</span>'
    esc = lambda name: html.escape(value(name)) if value(name) else pending
    s = support()
    contact = " · ".join(x for x in (html.escape(s["email"]) if s["email"] else "",
                                     f'<a href="https://t.me/{s["telegram"]}">@{s["telegram"]}</a>' if s["telegram"] else "") if x) or pending
    return (text.replace("{{OPERATOR}}", esc("OPERATOR_NAME")).replace("{{ADDRESS}}", esc("OPERATOR_ADDRESS"))
            .replace("{{EMAIL}}", esc("SUPPORT_EMAIL")).replace("{{CONTACT}}", contact)
            .replace("{{UPDATED}}", esc("PRIVACY_UPDATED"))
            .replace("{{COUNSEL}}", "" if os.environ.get("PRIVACY_REVIEWED") == "1" else COUNSEL[lang])
            .replace("{{DATA_LOCATION_UZ}}", esc("PRIVACY_DATA_LOCATION_UZ")).replace("{{DATA_LOCATION_RU}}", esc("PRIVACY_DATA_LOCATION_RU"))
            .replace("{{DATA_LOCATION_EN}}", esc("PRIVACY_DATA_LOCATION_EN")))
