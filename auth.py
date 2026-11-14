"""Sign-in (Telegram, phone number through the Telegram bot, or email and password),
signed session cookies and a small account store.

Three ways in:
  * Telegram login on the website (below);
  * phone number through the bot: the user shares their own number with the bot, which
    replies with a one-time sign-in link (telegram_bot.py, login_link / use_login_link);
  * email + password: no email is ever sent and the address is not verified; the password is
    stored only as a scrypt hash. Email accounts have negative user IDs, so they never collide
    with Telegram IDs (always positive).
Short checks (up to ANON_MAX_WORDS words, default 1000) need no sign-in at all (app.check).

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
  LOGIN_REQUIRED=1       longer checks need a signed-in user (default when sign-in is on; 0 = optional).
  EMAIL_LOGIN=0          turn off email + password accounts (on by default when sign-in is on).
  ANON_MAX_WORDS=1000    plagiarism checks up to this many words need no sign-in (0 = always sign in).
  SESSION_SECRET         optional, 32+ characters; otherwise derived from the bot token.
  USERS_DB               SQLite file for accounts (default /data/plagiq-users.sqlite3 when a
                         Railway volume is mounted at /data, else ./plagiq-users.sqlite3).

Stored per user: Telegram ID (or email account number), display name, username, phone number
(only if shared through the bot), email and password hash (email accounts), first/last sign-in and a log of
checks (date, mode, word count, result, AI estimate, report ID). Never manuscript text
or file names.
"""
import base64
import re
import secrets
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


def telegram_enabled():
    token = bot_token()
    return ":" in token and token.split(":", 1)[0].isdigit()


def email_enabled():
    """Email + password accounts: on by default, but only when sessions can be signed."""
    has_key = telegram_enabled() or len(os.environ.get("SESSION_SECRET", "")) >= 32
    return has_key and os.environ.get("EMAIL_LOGIN", "1") != "0"


def enabled():
    return telegram_enabled() or email_enabled()


def required():
    return enabled() and os.environ.get("LOGIN_REQUIRED", "1") != "0"


def anon_max_words():
    """Plagiarism checks up to this many words need no sign-in (0 = always sign in)."""
    try:
        return max(0, int(os.environ.get("ANON_MAX_WORDS", "1000")))
    except ValueError:
        return 1000


def bot_id():
    return bot_token().split(":", 1)[0] if telegram_enabled() else ""


def public_config():
    import telegram_bot
    import mailer
    return {"enabled": enabled(), "required": required(), "bot_id": bot_id(), "telegram": telegram_enabled(),
            "email": email_enabled(), "email_recovery": email_enabled() and mailer.configured(),
            "email_verify_required": verification_required(),
            "phone_bot": telegram_bot.enabled(), "anon_max_words": anon_max_words(),
            "bot_username": os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")[:64]}


# ------------------------------------------------------------- Telegram data

def check_telegram(data, now=None):
    """The verified user dict, or None. `data` is the JSON Telegram returned."""
    if not telegram_enabled() or not isinstance(data, dict):
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
                               "iat": int((now or time.time()) * 1000), "exp": int((now or time.time()) + SESSION_DAYS * 86400)},
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
    if int(data["id"]) < 0 and _password_changed(-int(data["id"])) > int(data.get("iat", 0)):
        return None                                   # signed in before the password was reset
    return {"id": int(data["id"]), "name": str(data.get("name", "")), "username": str(data.get("username", ""))}


def _password_changed(account):
    try:
        with _lock, _connect() as db:
            row = db.execute("SELECT password_changed FROM email_accounts WHERE id=?", (account,)).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


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
    db.execute("CREATE TABLE IF NOT EXISTS email_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, "
               "password TEXT NOT NULL, created TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS login_links (nonce TEXT PRIMARY KEY, user INTEGER NOT NULL, expires INTEGER NOT NULL)")
    columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "phone" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    email_columns = {row[1] for row in db.execute("PRAGMA table_info(email_accounts)")}
    if "verified" not in email_columns:
        db.execute("ALTER TABLE email_accounts ADD COLUMN verified TEXT")             # when the address was confirmed
    if "password_changed" not in email_columns:
        db.execute("ALTER TABLE email_accounts ADD COLUMN password_changed INTEGER")  # ms; sessions issued before end
    # Single-use links for e-mail confirmation and password recovery: only a hash of the token is stored.
    db.execute("CREATE TABLE IF NOT EXISTS email_tokens (hash TEXT PRIMARY KEY, account INTEGER NOT NULL, kind TEXT NOT NULL, "
               "expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0)")
    return db


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def remember(user, phone=None):
    with _lock, _connect() as db:
        db.execute("INSERT INTO users (id, name, username, first_seen, last_seen, phone) VALUES (?, ?, ?, ?, ?, ?) "
                   "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, "
                   "last_seen=excluded.last_seen, phone=COALESCE(excluded.phone, users.phone)",
                   (user["id"], user["name"], user.get("username", ""), _now(), _now(), phone))


def log_check(user, result, report_id):
    ai = (result.get("ai") or {}).get("fraction_ai")
    remember(user)                     # the account row may be missing (e.g. signed in before the volume existed)
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
        db.execute("DELETE FROM login_links WHERE user=?", (user["id"],))
        if user["id"] < 0:
            db.execute("DELETE FROM email_accounts WHERE id=?", (-user["id"],))


# ------------------------------------------------------------- email + password

EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,63}")
MIN_PASSWORD = 8
_FAILS = {}                                  # key -> [timestamps of failed sign-ins]
FAIL_LIMIT, FAIL_WINDOW = 10, 15 * 60


class AccountError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + _b64(salt) + "$" + _b64(digest)


def _matches(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        salt = base64.urlsafe_b64decode(salt + "=" * (-len(salt) % 4))
        expected = base64.urlsafe_b64decode(digest + "=" * (-len(digest) % 4))
        got = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def _clean_email(email):
    email = str(email or "").strip().lower()
    if len(email) > 254 or not EMAIL.fullmatch(email):
        raise AccountError("invalid_email", "Enter a valid email address.")
    return email


def _throttle(keys, now=None):
    now = now or time.time()
    for key in keys:
        recent = [t for t in _FAILS.get(key, []) if now - t < FAIL_WINDOW]
        _FAILS[key] = recent
        if len(recent) >= FAIL_LIMIT:
            raise AccountError("too_many_attempts", "Too many failed sign-ins. Try again in 15 minutes.", 429)


def _failed(keys, now=None):
    for key in keys:
        _FAILS.setdefault(key, []).append(now or time.time())


def register_email(email, password, name=""):
    """Create an email account and return its user dict. Nothing is sent to the address."""
    if not email_enabled():
        raise AccountError("login_not_configured", "Email sign-in is not set up on this server.", 503)
    email = _clean_email(email)
    if not isinstance(password, str) or len(password) < MIN_PASSWORD or len(password) > 200:
        raise AccountError("weak_password", f"The password needs at least {MIN_PASSWORD} characters.")
    display = " ".join(str(name or "").split())[:120] or email.split("@")[0][:60]
    hashed = _hash(password)
    with _lock, _connect() as db:
        try:
            cursor = db.execute("INSERT INTO email_accounts (email, password, created) VALUES (?, ?, ?)", (email, hashed, _now()))
        except sqlite3.IntegrityError:
            raise AccountError("email_taken", "An account with this email already exists. Sign in instead.", 409)
        user = {"id": -cursor.lastrowid, "name": display, "username": ""}
    remember(user)
    return user


def login_email(email, password, client=""):
    if not email_enabled():
        raise AccountError("login_not_configured", "Email sign-in is not set up on this server.", 503)
    email = _clean_email(email)
    keys = ["email:" + email] + (["ip:" + client] if client else [])
    _throttle(keys)
    with _lock, _connect() as db:
        row = db.execute("SELECT id, password FROM email_accounts WHERE email=?", (email,)).fetchone()
        name = db.execute("SELECT name FROM users WHERE id=?", (-row[0],)).fetchone() if row else None
    if not row:
        _matches(str(password), _hash("timing-equaliser"))       # same work whether or not the address exists
    if not row or not _matches(str(password), row[1]):
        _failed(keys)
        raise AccountError("login_failed_email", "Wrong email or password.", 401)
    if verification_required() and not _verified(row[0]):
        raise AccountError("email_unverified", "Confirm your email address first: open the link we sent you, or send it again.", 403)
    user = {"id": -row[0], "name": (name[0] if name else "") or email.split("@")[0], "username": ""}
    remember(user)
    return user


# ------------------------------------------------------- e-mail confirmation and password recovery
TOKEN_HOURS = {"verify": 48, "reset": 1}
_SENDS = {}                                    # key -> [times]: confirmation / recovery e-mails sent
SEND_LIMIT, SEND_WINDOW = 3, 3600


def verification_required():
    """Sign-in waits for a confirmed address only when the owner asks for it (EMAIL_VERIFY_REQUIRED=1) and e-mail
    can actually be sent; by default accounts work at once and confirmation is offered."""
    import mailer
    return os.environ.get("EMAIL_VERIFY_REQUIRED") == "1" and mailer.configured()


def _verified(account):
    with _lock, _connect() as db:
        row = db.execute("SELECT verified FROM email_accounts WHERE id=?", (account,)).fetchone()
    return bool(row and row[0])


def email_status(user):
    """{"email", "verified"} for an e-mail account, None for Telegram accounts."""
    if not user or user["id"] >= 0:
        return None
    with _lock, _connect() as db:
        row = db.execute("SELECT email, verified FROM email_accounts WHERE id=?", (-user["id"],)).fetchone()
    return {"email": row[0], "verified": bool(row[1])} if row else None


def _token(account, kind):
    raw = secrets.token_urlsafe(32)
    with _lock, _connect() as db:
        db.execute("DELETE FROM email_tokens WHERE expires < ?", (int(time.time()),))
        db.execute("INSERT INTO email_tokens (hash, account, kind, expires) VALUES (?, ?, ?, ?)",
                   (hashlib.sha256(raw.encode()).hexdigest(), account, kind, int(time.time()) + TOKEN_HOURS[kind] * 3600))
    return raw


def _take(raw, kind):
    """The account a valid, unused token belongs to (and mark it used), or None."""
    if not isinstance(raw, str) or not 20 <= len(raw) <= 100:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with _lock, _connect() as db:
        row = db.execute("SELECT account FROM email_tokens WHERE hash=? AND kind=? AND used=0 AND expires>=?",
                         (digest, kind, int(time.time()))).fetchone()
        if row:
            db.execute("UPDATE email_tokens SET used=1 WHERE hash=?", (digest,))
    return row[0] if row else None


IP_SEND_LIMIT = 20                             # one university network can hold many students


def _may_send(keys, now=None):
    """At most SEND_LIMIT e-mails per address and IP_SEND_LIMIT per network address in SEND_WINDOW."""
    now = now or time.time()
    for key in keys:
        recent = [t for t in _SENDS.get(key, []) if now - t < SEND_WINDOW]
        _SENDS[key] = recent
        if len(recent) >= (IP_SEND_LIMIT if "-ip:" in key else SEND_LIMIT):
            return False
    for key in keys:
        _SENDS[key].append(now)
    return True


MAIL_T = {
    "verify": ("Confirm your PlagIQ email address",
               "Hello,\n\nplease confirm this address for your PlagIQ account by opening the link below (valid for 48 hours):\n\n{link}\n\n"
               "If you did not create a PlagIQ account, ignore this message.\n\n"
               "Assalomu alaykum! PlagIQ hisobingiz uchun manzilni tasdiqlang: {link}\nЗдравствуйте! Подтвердите адрес для аккаунта PlagIQ: {link}\n"),
    "reset": ("Reset your PlagIQ password",
              "Hello,\n\nsomeone asked to reset the password of the PlagIQ account for this address. To choose a new password, "
              "open the link below (valid for 1 hour, once):\n\n{link}\n\nIf it was not you, ignore this message; your password "
              "stays the same.\n\nParolni tiklash havolasi (1 soat): {link}\nСсылка для смены пароля (1 час): {link}\n"),
}


def send_link(kind, account, email, origin):
    """E-mail a confirmation or recovery link. Returns True when sent."""
    import mailer
    link = origin.rstrip("/") + ("/auth/verify-email?t=" if kind == "verify" else "/auth/reset?t=") + _token(account, kind)
    subject, body = MAIL_T[kind]
    try:
        mailer.send(email, subject, body.format(link=link))
        return True
    except mailer.MailError:
        return False


def request_verification(user, origin, client=""):
    status = email_status(user)
    if not status or status["verified"]:
        return False
    if not _may_send(["verify:" + status["email"]] + (["verify-ip:" + client] if client else [])):
        raise AccountError("too_many_emails", "Too many emails requested. Try again in an hour.", 429)
    return send_link("verify", -user["id"], status["email"], origin)


def confirm_email(raw):
    account = _take(raw, "verify")
    if account is None:
        return False
    with _lock, _connect() as db:
        db.execute("UPDATE email_accounts SET verified=? WHERE id=?", (_now(), account))
    return True


def request_reset(email, origin, client=""):
    """Send a recovery link if the address has an account. The answer never says whether it has one."""
    try:
        email = _clean_email(email)
    except AccountError:
        return
    if not _may_send(["reset:" + email] + (["reset-ip:" + client] if client else [])):
        raise AccountError("too_many_emails", "Too many emails requested. Try again in an hour.", 429)
    with _lock, _connect() as db:
        row = db.execute("SELECT id FROM email_accounts WHERE email=?", (email,)).fetchone()
    if row:
        send_link("reset", row[0], email, origin)


def reset_password(raw, password):
    """Set a new password from a recovery link. Ends all earlier sessions and confirms the address."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD or len(password) > 200:
        raise AccountError("weak_password", f"The password needs at least {MIN_PASSWORD} characters.")
    account = _take(raw, "reset")
    if account is None:
        raise AccountError("reset_link_invalid", "This link has expired or was already used. Ask for a new one.", 400)
    with _lock, _connect() as db:
        db.execute("UPDATE email_accounts SET password=?, password_changed=?, verified=COALESCE(verified, ?) WHERE id=?",
                   (_hash(password), int(time.time() * 1000), _now(), account))
        db.execute("UPDATE email_tokens SET used=1 WHERE account=? AND kind='reset'", (account,))
    return True


# ------------------------------------------------------------- one-time links from the bot

LINK_MINUTES = 10


def login_link(user_id):
    """A single-use sign-in code for a Telegram user who shared their phone number with the bot."""
    nonce = secrets.token_urlsafe(24)
    with _lock, _connect() as db:
        db.execute("DELETE FROM login_links WHERE expires < ?", (int(time.time()),))
        db.execute("INSERT INTO login_links (nonce, user, expires) VALUES (?, ?, ?)",
                   (nonce, int(user_id), int(time.time()) + LINK_MINUTES * 60))
    return nonce


def use_login_link(nonce):
    """The user for a valid, unused, unexpired code (the code is consumed), else None."""
    if not isinstance(nonce, str) or not 20 <= len(nonce) <= 64:
        return None
    with _lock, _connect() as db:
        row = db.execute("SELECT user, expires FROM login_links WHERE nonce=?", (nonce,)).fetchone()
        db.execute("DELETE FROM login_links WHERE nonce=?", (nonce,))
        if not row or row[1] < time.time():
            return None
        account = db.execute("SELECT id, name, username FROM users WHERE id=?", (row[0],)).fetchone()
    return {"id": account[0], "name": account[1], "username": account[2] or ""} if account else None
