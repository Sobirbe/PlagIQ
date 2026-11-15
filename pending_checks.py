"""Checks paid one at a time ("One document. One payment."): the check waits here until the payment provider has
confirmed the payment, then the server runs it. TEST (sandbox) MODE ONLY, like payments.py.

Flow
  1. The user chooses "Pay 25,000 so'm with Payme" (or Click / Uzum Bank) for the document on the page. The server
     works out the price itself (entitlements.decide), refuses if it differs from the price the page showed, creates
     an order for exactly that amount (payments.create_order, purpose "check") and stores the check here as
     "awaiting". The same request_id from the same account returns the same order (a double click or a retried
     request never creates a second order).
  2. The provider calls PlagIQ's merchant endpoint (server to server, signed); that marks the order paid and credits
     the amount to the account's balance, exactly as for a top-up. Coming back from the provider's page in the
     browser changes nothing.
  3. The server notices the paid order (every 30 seconds, and whenever the page asks for the state), claims the check
     ("running"; only one claim can succeed) and runs it. The check takes its price from the balance and gives it
     back if it fails, so a failed check leaves the payment on the balance, where it pays for running it again.
     A check cut off by a restart goes back to "awaiting" and runs again.

What is kept: the document's extracted text (compressed), its file name and the check's options, in the reports
database on the private volume, only until the check has run, or at most PENDING_HOURS (6) if the payment does not
arrive; then the text is deleted. If a payment arrives after that, the amount stays on the balance. The rows
themselves (without text) are deleted after 7 days, and with the account.
"""
import json
import os
import secrets
import sqlite3
import threading
import time
import zlib
from contextlib import closing

_lock = threading.Lock()
KEEP_DAYS = 7


def hours():
    try:
        return min(max(int(os.environ.get("PENDING_HOURS", "6")), 1), 24)
    except ValueError:
        return 6


def _connect():
    import reports_store
    db = sqlite3.connect(reports_store._path(), timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS pending_checks (id TEXT PRIMARY KEY, user INTEGER NOT NULL, session TEXT NOT NULL, "
               "request_key TEXT NOT NULL, order_id TEXT NOT NULL, price INTEGER NOT NULL, created REAL NOT NULL, "
               "expires REAL NOT NULL, state TEXT NOT NULL, job TEXT, error TEXT, filename TEXT, params TEXT, text BLOB, "
               "UNIQUE (user, request_key))")
    db.execute("CREATE INDEX IF NOT EXISTS pending_state ON pending_checks (state)")
    return db


FIELDS = ("id", "user", "order_id", "price", "created", "expires", "state", "job", "error", "filename")


def create(user_id, session, request_key, order_id, price, filename, params, text):
    ident = secrets.token_hex(16)
    now = time.time()
    with _lock, closing(_connect()) as db, db:
        db.execute("INSERT INTO pending_checks (id, user, session, request_key, order_id, price, created, expires, state, "
                   "filename, params, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting', ?, ?, ?)",
                   (ident, user_id, session, request_key, order_id, int(price), now, now + hours() * 3600,
                    (filename or "")[:180], json.dumps(params, ensure_ascii=False), zlib.compress(text.encode(), 6)))
    return ident


def by_request(user_id, request_key):
    with _lock, closing(_connect()) as db:
        row = db.execute(f"SELECT {', '.join(FIELDS)} FROM pending_checks WHERE user=? AND request_key=?",
                         (user_id, request_key)).fetchone()
    return dict(zip(FIELDS, row)) if row else None


def listing(user_id, limit=20):
    with _lock, closing(_connect()) as db:
        rows = db.execute(f"SELECT {', '.join(FIELDS)} FROM pending_checks WHERE user=? ORDER BY created DESC LIMIT ?",
                          (user_id, limit)).fetchall()
    return [dict(zip(FIELDS, r)) for r in rows]


def awaiting():
    """Checks still waiting for their payment (not expired): [{"id", "user", "order_id", ...}]."""
    with _lock, closing(_connect()) as db:
        rows = db.execute(f"SELECT {', '.join(FIELDS)} FROM pending_checks WHERE state='awaiting' AND expires>?",
                          (time.time(),)).fetchall()
    return [dict(zip(FIELDS, r)) for r in rows]


def claim(ident):
    """Take an awaiting check to run it. Returns its full row (with session, params and text) or None when another
    worker already took it."""
    with _lock, closing(_connect()) as db, db:
        if db.execute("UPDATE pending_checks SET state='running' WHERE id=? AND state='awaiting'", (ident,)).rowcount != 1:
            return None
        row = db.execute("SELECT user, session, price, filename, params, text, order_id FROM pending_checks WHERE id=?",
                         (ident,)).fetchone()
    return {"id": ident, "user": row[0], "session": row[1], "price": row[2], "filename": row[3],
            "params": json.loads(row[4] or "{}"), "text": zlib.decompress(row[5]).decode() if row[5] else None,
            "order_id": row[6]}


def release(ident):
    """Put a claimed check back (the server was busy); it runs at the next round."""
    with _lock, closing(_connect()) as db, db:
        db.execute("UPDATE pending_checks SET state='awaiting' WHERE id=? AND state='running'", (ident,))


def finish(ident, job=None, error=None):
    """The check ran (done, with its report id) or failed (error code). The document text is deleted either way."""
    with _lock, closing(_connect()) as db, db:
        db.execute("UPDATE pending_checks SET state=?, job=?, error=?, text=NULL, params=NULL WHERE id=?",
                   ("failed" if error else "done", job, error, ident))


def interrupted():
    """At start-up: checks that were running were cut off by the restart; they run again."""
    with _lock, closing(_connect()) as db, db:
        return db.execute("UPDATE pending_checks SET state='awaiting' WHERE state='running' AND text IS NOT NULL").rowcount


def purge(now=None):
    """Unpaid checks past PENDING_HOURS lose their text ("expired"); rows older than KEEP_DAYS are deleted."""
    now = now or time.time()
    with _lock, closing(_connect()) as db, db:
        expired = db.execute("UPDATE pending_checks SET state='expired', text=NULL, params=NULL "
                             "WHERE state='awaiting' AND expires<=?", (now,)).rowcount
        db.execute("DELETE FROM pending_checks WHERE created<?", (now - KEEP_DAYS * 86400,))
    return expired


def forget_user(user_id):
    with _lock, closing(_connect()) as db, db:
        db.execute("DELETE FROM pending_checks WHERE user=?", (user_id,))
