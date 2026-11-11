"""Sign-in with Telegram, signed session cookies and a small account store.

Flow (no Telegram script is loaded into PlagIQ's pages, so the CSP stays strict):
  1. "Sign in with Telegram" sends the browser to oauth.telegram.org with the bot ID
     and this site as origin/return address.
  2. Telegram returns to /login with #tgAuthResult=<base64 JSON> in the URL fragment.
  3. The page posts that JSON to /auth/telegram; the server checks Telegram's signature
     (HMAC-SHA256 keyed with SHA-256 of the bot token, see core.telegram.org/widgets/login)
     and the auth_date, then sets an HttpOnly, SameSite=Strict session cookie.

Settings (website service):
  TELEGRAM_BOT_TOKEN     token from @BotFather (secret). Turns sign-in on.
  TELEGRAM_BOT_USERNAME  the bot's username, without @ (shown on the button; not secret).
  LOGIN_REQUIRED=1       checks need a signed-in user (default when the token is set; 0 = optional).
  SESSION_SECRET         optional, 32+ characters; otherwise derived from the bot token.
  USERS_DB               SQLite file for accounts (default /data/plagiq-users.sqlite3 when a
                         Railway volume is mounted at /data, else ./plagiq-users.sqlite3).

Stored per user: Telegram ID, display name, username, first/last sign-in and a log of
checks (date, mode, word count, result, AI estimate, report ID). Never manuscript text
or file names.
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

COOKIE = "plagiq_user"
SESSION_DAYS = 30
AUTH_MAX_AGE = 24 * 3600          # Telegram auth data older than a day is refused
_lock = threading.Lock()


def bot_token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def enabled():
    token = bot_token()
    return ":" in token and token.split(":", 1)[0].isdigit()


def required():
    return enabled() and os.environ.get("LOGIN_REQUIRED", "1") != "0"


def bot_id():
    return bot_token().split(":", 1)[0] if enabled() else ""


def public_config():
    return {"enabled": enabled(), "required": required(), "bot_id": bot_id(),
            "bot_username": os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")[:64]}


# ------------------------------------------------------------- Telegram data

def check_telegram(data, now=None):
    """The verified user dict, or None. `data` is the JSON Telegram returned."""
    if not enabled() or not isinstance(data, dict):
        return None
    fields = {k: str(v) for k, v in data.items() if k != "hash" and v is not None and not isinstance(v, (dict, list))}
    received = str(data.get("hash", ""))
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token().encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        return None
    try:
        age = (now or time.time()) - int(fields.get("auth_date", "0"))
        user_id = int(fields.get("id", ""))
    except ValueError:
        return None
    if not 0 <= age <= AUTH_MAX_AGE + 60:
        return None
    name = " ".join(x for x in (fields.get("first_name", ""), fields.get("last_name", "")) if x).strip()
    return {"id": user_id, "name": name[:120] or fields.get("username", "")[:64] or str(user_id),
            "username": fields.get("username", "")[:64]}


# ------------------------------------------------------------- session cookie

def _key():
    explicit = os.environ.get("SESSION_SECRET", "")
    if len(explicit) >= 32:
        return explicit.encode()
    return hashlib.sha256(b"plagiq-session|" + bot_token().encode()).digest()


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def issue(user, now=None):
    payload = _b64(json.dumps({"id": user["id"], "name": user["name"], "username": user.get("username", ""),
                               "exp": int((now or time.time()) + SESSION_DAYS * 86400)},
                              separators=(",", ":"), ensure_ascii=False).encode())
    return payload + "." + _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())


def read(cookie, now=None):
    """The signed-in user from the cookie value, or None."""
    if not enabled() or not cookie or cookie.count(".") != 1:
        return None
    payload, signature = cookie.split(".")
    if not hmac.compare_digest(_b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest()), signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < (now or time.time()):
        return None
    return {"id": int(data["id"]), "name": str(data.get("name", "")), "username": str(data.get("username", ""))}


def is_owner(user):
    """Site owners (OWNER_TELEGRAM_IDS, comma-separated Telegram user IDs) see /admin."""
    ids = {x.strip() for x in os.environ.get("OWNER_TELEGRAM_IDS", "").split(",") if x.strip().isdigit()}
    return bool(user) and str(user["id"]) in ids


def stats():
    """Counts for the owner dashboard (no personal data)."""
    try:
        with _lock, _connect() as db:
            users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            day = time.strftime("%Y-%m-%d", time.gmtime())
            week = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))
            checks = db.execute("SELECT COUNT(*), SUM(date >= ?), SUM(date >= ?) FROM checks", (day, week)).fetchone()
            modes = dict(db.execute("SELECT mode, COUNT(*) FROM checks WHERE date >= ? GROUP BY mode", (week,)).fetchall())
    except sqlite3.Error:
        return None
    return {"users": users, "checks_total": checks[0] or 0, "checks_today": checks[1] or 0,
            "checks_week": checks[2] or 0, "modes_week": modes, "db_path": _db_path(),
            "on_volume": _db_path().startswith("/data/") and Path("/data").is_dir()}


def db_file():
    return _db_path()


def owner_id(user):
    """Stable, non-reversible owner label for a user's archive deposits ("" when signed out)."""
    if not user:
        return ""
    return "user:" + hashlib.sha256(b"plagiq-owner|" + str(user["id"]).encode()).hexdigest()[:24]


# ------------------------------------------------------------- account store

def _db_path():
    configured = os.environ.get("USERS_DB")
    if configured:
        return configured
    return "/data/plagiq-users.sqlite3" if Path("/data").is_dir() else str(Path(__file__).with_name("plagiq-users.sqlite3"))


def _connect():
    db = sqlite3.connect(_db_path(), timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT, "
               "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS checks (user INTEGER NOT NULL, date TEXT NOT NULL, mode TEXT NOT NULL, "
               "words INTEGER NOT NULL, percent REAL, ai REAL, report TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS checks_user ON checks (user, date)")
    return db


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def remember(user):
    with _lock, _connect() as db:
        db.execute("INSERT INTO users (id, name, username, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, "
                   "last_seen=excluded.last_seen", (user["id"], user["name"], user.get("username", ""), _now(), _now()))


def log_check(user, result, report_id):
    ai = (result.get("ai") or {}).get("fraction_ai")
    with _lock, _connect() as db:
        db.execute("INSERT INTO checks (user, date, mode, words, percent, ai, report) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (user["id"], _now(), result.get("mode", ""), int(result.get("total_tokens") or 0),
                    result.get("percent"), ai, report_id[:12]))


def history(user, limit=20):
    with _lock, _connect() as db:
        rows = db.execute("SELECT date, mode, words, percent, ai, report FROM checks WHERE user=? "
                          "ORDER BY date DESC LIMIT ?", (user["id"], limit)).fetchall()
    return [dict(zip(("date", "mode", "words", "percent", "ai", "report"), row)) for row in rows]


def forget(user):
    """Delete the account and its check log (the user's right to erasure). Archive deposits
    are removed separately through the engine (see app.delete_me)."""
    with _lock, _connect() as db:
        db.execute("DELETE FROM checks WHERE user=?", (user["id"],))
        db.execute("DELETE FROM users WHERE id=?", (user["id"],))
