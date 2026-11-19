"""Checks and their reports, kept for REPORT_HOURS (default 24) and then deleted; a signed-in account's reports for
ACCOUNT_REPORT_DAYS (default 7; 0 = REPORT_HOURS as well).

A check is recorded when it starts ("running") and updated when it ends: "done" with its report, or "failed"
with an error code. So a user can close the browser during a long check, come back, and find it under
"My checks", from the same browser or, when signed in, from any device.

What is stored: the report as shown in the browser and the PDFs, which includes the document's text (for the
highlighted appendix), compressed, in a database file next to the accounts database (the private /data volume).
It is never shared, never indexed and never used to train anything. After REPORT_HOURS the row is deleted and
the space is given back to the disk (incremental vacuum). A user can delete a report earlier.

Never stored: the AI detector's flagged passages and the integrity-signal examples (shown once only).
"""
import json
import os
import sqlite3
import threading
import time
import zlib
from contextlib import closing
from pathlib import Path

_lock = threading.Lock()
LIST_LIMIT = 50


def hours():
    try:
        return min(max(int(os.environ.get("REPORT_HOURS", "24")), 1), 72)
    except ValueError:
        return 24


def account_days():
    """Days a signed-in account's reports are kept (ACCOUNT_REPORT_DAYS, 0-30; 0 = the same as REPORT_HOURS)."""
    try:
        return min(max(int(os.environ.get("ACCOUNT_REPORT_DAYS", "7")), 0), 30)
    except ValueError:
        return 7


def ttl(signed_in=False):
    """Seconds a report is kept: longer for a signed-in account (it can be reopened from any device)."""
    if signed_in and account_days():
        return max(account_days() * 86400, hours() * 3600)
    return hours() * 3600


def _ru(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def period(lang, signed_in=False):
    """"24 hours" / "7 days" in the language (uz, ru, en), with the Russian number forms."""
    days = account_days() if signed_in else 0
    if days:
        return {"uz": f"{days} kun", "ru": _ru(days, "день", "дня", "дней"),
                "en": f"{days} day" + ("" if days == 1 else "s")}[lang]
    h = hours()
    return {"uz": f"{h} soat", "ru": _ru(h, "час", "часа", "часов"), "en": f"{h} hour" + ("" if h == 1 else "s")}[lang]


def fill(page):
    """Put the retention periods into the page texts (index.html, privacy.html), so they always match the settings:
    __KEEP_xx__ both periods, __KEEPACC_xx__ the account's, __HOURS_xx__ without an account, __KEEPAFTER_xx__ "after"."""
    longer = bool(account_days())
    for lang in ("uz", "ru", "en"):
        acc, h = period(lang, True), period(lang)
        both = {"en": f"{acc} in your account ({h} without signing in)",
                "uz": f"hisobingizda {acc} (tizimga kirmasdan — {h})",
                "ru": f"в аккаунте {acc} (без входа — {h})"}[lang] if longer else h
        after = {"en": f"after {acc} ({h} if you are not signed in)",
                 "uz": f"{acc}dan keyin (tizimga kirmagan bo‘lsangiz, {h}dan keyin)",
                 "ru": f"через {acc} (без входа — через {h})"}[lang] if longer else \
            {"en": f"after {h}", "uz": f"{h}dan keyin", "ru": f"через {h}"}[lang]
        key = lang.upper()
        page = (page.replace(f"__KEEPACC_{key}__", acc).replace(f"__KEEPAFTER_{key}__", after)
                .replace(f"__KEEP_{key}__", both).replace(f"__HOURS_{key}__", h))
    return page


def _path():
    configured = os.environ.get("REPORTS_DB")
    if configured:
        return configured
    import auth
    return str(Path(auth._db_path()).with_name("plagiq-reports.sqlite3"))


def _connect():
    path = _path()
    fresh = not Path(path).exists()
    db = sqlite3.connect(path, timeout=10)
    if fresh:
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")         # deleted reports give their space back
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, owner TEXT NOT NULL, user INTEGER, "
               "created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL, filename TEXT, mode TEXT, "
               "words INTEGER, status TEXT, percent REAL, error TEXT, charge TEXT, price INTEGER, payload BLOB)")
    if "kind" not in {row[1] for row in db.execute("PRAGMA table_info(reports)")}:
        db.execute("ALTER TABLE reports ADD COLUMN kind TEXT")    # "trial": on-screen only (no PDF or CSV)
    db.execute("CREATE INDEX IF NOT EXISTS reports_owner ON reports (owner)")
    db.execute("CREATE INDEX IF NOT EXISTS reports_user ON reports (user)")
    db.execute("CREATE INDEX IF NOT EXISTS reports_expires ON reports (expires)")
    return db


def start(identifier, owner, user_id, filename, mode):
    now = time.time()
    with _lock, closing(_connect()) as db, db:
        db.execute("INSERT INTO reports (id, owner, user, created, expires, state, filename, mode) "
                   "VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
                   (identifier, owner, user_id, now, now + ttl(user_id is not None), (filename or "")[:180], mode))


def charged(identifier, charge_id, price):
    """Remember a charge, so a check interrupted by a restart can be refunded."""
    with _lock, closing(_connect()) as db, db:
        db.execute("UPDATE reports SET charge=?, price=? WHERE id=?", (str(charge_id), int(price), identifier))


def finish(identifier, result):
    payload = zlib.compress(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(), 6)
    with _lock, closing(_connect()) as db, db:
        db.execute("UPDATE reports SET state='done', status=?, percent=?, words=?, mode=?, payload=?, charge=NULL, kind=? "
                   "WHERE id=?", (result.get("status"), result.get("percent"), int(result.get("total_tokens") or 0),
                                  result.get("mode"), payload, (result.get("entitlement") or {}).get("kind"), identifier))


def fail(identifier, code):
    with _lock, closing(_connect()) as db, db:
        db.execute("UPDATE reports SET state='failed', error=?, charge=NULL WHERE id=? AND state='running'",
                   (str(code)[:60], identifier))


def get(identifier):
    """The row as a dict (payload decoded into 'result'), or None when missing or expired."""
    with _lock, closing(_connect()) as db:
        row = db.execute("SELECT id, owner, user, created, expires, state, filename, mode, words, status, percent, error, "
                         "payload FROM reports WHERE id=? AND expires>?", (identifier, time.time())).fetchone()
    if not row:
        return None
    entry = dict(zip(("id", "owner", "user", "created", "expires", "state", "filename", "mode", "words", "status",
                      "percent", "error"), row[:-1]))
    entry["result"] = json.loads(zlib.decompress(row[-1])) if row[-1] else None
    return entry


def listing(owner, user_id):
    """Checks of this browser session and (signed in) this account, newest first, without payloads."""
    with _lock, closing(_connect()) as db:
        rows = db.execute("SELECT id, created, expires, state, filename, mode, words, status, percent, error, kind FROM reports "
                          "WHERE expires>? AND (owner=? OR (user IS NOT NULL AND user=?)) ORDER BY created DESC LIMIT ?",
                          (time.time(), owner or "-", user_id if user_id is not None else -10**18, LIST_LIMIT)).fetchall()
    return [dict(zip(("id", "created", "expires", "state", "filename", "mode", "words", "status", "percent", "error", "kind"), r),
                 exports=r[10] != "trial") for r in rows]


def delete(identifier):
    with _lock, closing(_connect()) as db, db:
        return db.execute("DELETE FROM reports WHERE id=?", (identifier,)).rowcount > 0


def forget_user(user_id):
    """Account deletion: the user's reports go too."""
    with _lock, closing(_connect()) as db, db:
        db.execute("DELETE FROM reports WHERE user=?", (user_id,))


def purge(now=None):
    """Delete expired reports and give the space back. Returns how many were deleted."""
    with _lock, closing(_connect()) as db:
        with db:
            removed = db.execute("DELETE FROM reports WHERE expires<=?", (now or time.time(),)).rowcount
        if removed:
            db.execute("PRAGMA incremental_vacuum")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return removed


def interrupted():
    """At start-up: checks still 'running' were cut off by a restart. Mark them failed and return the charged
    ones as (user_id, charge_id, price) so they can be refunded."""
    with _lock, closing(_connect()) as db, db:
        rows = db.execute("SELECT user, charge, price FROM reports WHERE state='running' AND charge IS NOT NULL").fetchall()
        db.execute("UPDATE reports SET state='failed', error='interrupted', charge=NULL WHERE state='running'")
    return [(u, c, p) for u, c, p in rows if u is not None and c and p]


def stats():
    with _lock, closing(_connect()) as db:
        count, size = db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM reports").fetchone()
    return {"reports": count, "payload_bytes": size, "hours": hours()}
