"""Times shown to people are Tashkent time (UTC+5, no daylight saving since 1992); stored times stay UTC.

The QR verification link signs the UTC check time, so certificates issued before this change remain
valid; only the display is converted. REPORT_UTC_OFFSET_HOURS (default 5) changes the offset, for example
for a deployment outside Uzbekistan.
"""
import os
from datetime import datetime, timedelta, timezone

LABEL = {"uz": "Toshkent vaqti", "ru": "ташкентское время", "en": "Tashkent time"}


def offset_hours():
    try:
        return max(-12, min(14, int(os.environ.get("REPORT_UTC_OFFSET_HOURS", "5"))))
    except ValueError:
        return 5


def zone():
    return timezone(timedelta(hours=offset_hours()))


def label(lang):
    hours = offset_hours()
    name = LABEL.get(lang, LABEL["en"]) if hours == 5 else "UTC"
    return f"{name}, UTC{hours:+d}" if hours else "UTC"


def parse(value):
    """A datetime from an ISO string, a compact 'YYYYMMDDTHHMM' string, epoch milliseconds or a datetime;
    naive values are taken as UTC. None when it cannot be read."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(value / 1000, timezone.utc)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if len(text) == 13 and text[8] == "T" and text.replace("T", "").isdigit():
                moment = datetime.strptime(text, "%Y%m%dT%H%M")
            else:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def show(value, lang="en", seconds=False, with_label=True):
    """'2026-11-12 22:34 (Toshkent vaqti, UTC+5)'; the input unchanged when it is not a time
    (and a bare date stays a date)."""
    text = str(value or "")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    moment = parse(value)
    if moment is None:
        return text
    local = moment.astimezone(zone())
    shown = local.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")
    return f"{shown} ({label(lang)})" if with_label else shown
