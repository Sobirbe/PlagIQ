"""Older e-mail + password accounts keep working; the phone-number links from the Telegram bot still work; no check
runs without an account."""
import json

import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import telegram_bot
from test_auth_ai import EN, TOKEN


@pytest.fixture()
def site(monkeypatch, tmp_path):
    for name, value in {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_BOT_USERNAME": "plagiq_bot",
                        "USERS_DB": str(tmp_path / "users.sqlite3"), "PUBLIC_ORIGIN": "https://plagiq.example",
                        "TELEGRAM_BOT_REPLIES": "1"}.items():
        monkeypatch.setenv(name, value)
    for name in ("LOGIN_REQUIRED", "SESSION_SECRET", "EMAIL_LOGIN", "ANON_MAX_WORDS", "PAYMENTS_ENABLED",
                 "PRIVATE_PILOT_USER", "PRIVATE_PILOT_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    web.RATE.clear(); web.results.clear(); auth._FAILS.clear()
    return TestClient(web.app, base_url="https://plagiq.example")


def check(client, text, **extra):
    return client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": EN, **extra},
                       files={"submission": ("m.txt", text.encode())})


# ------------------------------------------------------------- no check without an account

def test_no_check_without_account(site, monkeypatch):
    assert check(site, EN).json()["code"] == "login_required"               # 168 words: still needs sign-in
    monkeypatch.setenv("ANON_MAX_WORDS", "2000")                           # older settings are no longer read
    monkeypatch.setenv("LOGIN_REQUIRED", "0")
    assert check(site, EN).json()["code"] == "login_required"


# ------------------------------------------------------------- older e-mail + password accounts

def test_register_closed_but_older_password_accounts_sign_in(site):
    r = site.post("/auth/register", json={"email": " Dilnoza@Example.UZ ", "password": "correct horse 1", "name": "Dilnoza"})
    assert r.status_code == 410 and r.json()["code"] == "password_signup_closed"
    with pytest.raises(auth.AccountError):
        auth.register_email("dilnoza@example.uz", "short")
    auth.register_email(" Dilnoza@Example.UZ ", "correct horse 1", "Dilnoza")       # an account made earlier
    with pytest.raises(auth.AccountError):
        auth.register_email("dilnoza@example.uz", "another pass")
    assert site.post("/auth/login", json={"email": "dilnoza@example.uz", "password": "wrong pass"}).json()["code"] == "login_failed_email"
    assert site.post("/auth/login", json={"email": "nobody@example.uz", "password": "wrong pass"}).json()["code"] == "login_failed_email"
    ok = site.post("/auth/login", json={"email": "DILNOZA@example.uz", "password": "correct horse 1"})
    assert ok.status_code == 200 and ok.json()["user"]["name"] == "Dilnoza"


def test_password_stored_only_as_hash(site):
    auth.register_email("a@b.uz", "secret password 9")
    import sqlite3
    with sqlite3.connect(auth.db_file()) as db:
        stored = db.execute("SELECT password FROM email_accounts").fetchone()[0]
    assert stored.startswith("scrypt$") and "secret password 9" not in stored


def test_too_many_failed_logins(site):
    auth.register_email("a@b.uz", "secret password 9")
    for _ in range(auth.FAIL_LIMIT):
        site.post("/auth/login", json={"email": "a@b.uz", "password": "nope nope"})
    r = site.post("/auth/login", json={"email": "a@b.uz", "password": "secret password 9"})
    assert r.status_code == 429 and r.json()["code"] == "too_many_attempts"


def test_email_account_deleted_completely(site):
    auth.register_email("a@b.uz", "secret password 9")
    assert site.post("/auth/login", json={"email": "a@b.uz", "password": "secret password 9"}).status_code == 200
    assert site.delete("/api/me").status_code == 200
    assert site.post("/auth/login", json={"email": "a@b.uz", "password": "secret password 9"}).status_code == 401


def test_email_only_site_with_session_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.sqlite3"))
    assert auth.enabled() and auth.email_enabled() and not auth.telegram_enabled()
    monkeypatch.setenv("EMAIL_LOGIN", "0")
    assert not auth.enabled()


def test_email_user_is_never_owner(site, monkeypatch):
    auth.register_email("a@b.uz", "secret password 9")
    user = site.post("/auth/login", json={"email": "a@b.uz", "password": "secret password 9"}).json()["user"]
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", str(-user["id"]) + "," + str(user["id"]))
    assert not auth.is_owner(user)
    assert site.get("/admin").status_code in (401, 403, 404)


# ------------------------------------------------------------- phone number through the bot

def contact_update(sender=777, owner=777, phone="998901234567"):
    return {"update_id": 2, "message": {"message_id": 6, "chat": {"id": sender, "type": "private"},
                                        "from": {"id": sender, "first_name": "Aziz", "language_code": "uz"},
                                        "contact": {"phone_number": phone, "user_id": owner, "first_name": "Aziz"}}}


@pytest.fixture()
def sent(monkeypatch):
    out = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b'{"ok": true}'

    monkeypatch.setattr(telegram_bot.urllib.request, "urlopen",
                        lambda request, timeout=10: out.append(json.loads(request.data)) or Reply())
    return out


def hook(client, update):
    return client.post("/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": telegram_bot.secret()})


def test_phone_share_gives_one_time_link(site, sent):
    assert hook(site, contact_update()).status_code == 200
    reply = sent[-1]
    assert "+998901234567" in reply["text"]
    url = reply["reply_markup"]["inline_keyboard"][0][0]["url"]
    assert url.startswith("https://plagiq.example/auth/bot?t=")
    nonce = url.split("t=", 1)[1]
    r = site.get("/auth/bot?t=" + nonce, follow_redirects=False)
    assert r.status_code == 200 and auth.COOKIE in r.headers["set-cookie"]
    assert site.get("/api/me").json()["user"] == {"id": 777, "name": "Aziz", "username": ""}
    site.cookies.clear()
    assert site.get("/auth/bot?t=" + nonce).status_code == 410                  # single use
    import sqlite3
    with sqlite3.connect(auth.db_file()) as db:
        assert db.execute("SELECT phone FROM users WHERE id=777").fetchone()[0] == "+998901234567"


def test_someone_elses_number_is_refused(site, sent):
    hook(site, contact_update(owner=999))
    assert "o‘zingizning raqamingizni" in sent[-1]["text"] and "inline_keyboard" not in sent[-1]["reply_markup"]


def test_expired_link(site, sent, monkeypatch):
    hook(site, contact_update())
    nonce = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["url"].split("t=", 1)[1]
    later = __import__("time").time() + 11 * 60
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert site.get("/auth/bot?t=" + nonce).status_code == 410
    assert site.get("/auth/bot?t=short").status_code == 410
