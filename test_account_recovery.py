"""Older e-mail + password accounts: e-mail confirmation and password recovery (single-use, expiring, hashed links;
no account enumeration; rate limits; a reset ends earlier sessions). New accounts are passwordless (see
test_trial_pricing.py). Mail is captured, never sent. Off until SMTP is configured."""
import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import mailer
from test_auth_ai import login, sign_in      # noqa: F401  (fixture)


@pytest.fixture
def mail(login, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_FROM", "PlagIQ <no-reply@example.org>")
    monkeypatch.delenv("EMAIL_VERIFY_REQUIRED", raising=False)
    monkeypatch.delenv("PUBLIC_ORIGIN", raising=False)
    auth._SENDS.clear(); auth._FAILS.clear()
    box = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, text: box.append((to, subject, text)))
    return login, box


def link(box, kind):
    text = box[-1][2]
    return re.search(r"http://testserver(/auth/(?:verify-email|reset)\?t=[\w-]+)", text).group(1)


def register(client, email="ali@example.org", password="first-password"):
    """An account created earlier with a password (no longer possible on the site), then signed in."""
    auth.register_email(email, password, "Ali")
    return client.post("/auth/login", json={"email": email, "password": password})


def test_older_account_gets_a_confirmation_link_that_works_once(mail):
    client, box = mail
    register(client)
    assert client.get("/api/me").json()["email"] == {"email": "ali@example.org", "verified": False}
    assert client.post("/auth/verify/resend").json() == {"sent": True} and box[-1][0] == "ali@example.org"
    url = link(box, "verify")
    assert "confirmed" in client.get(url + "&ui=en").text
    assert client.get("/api/me").json()["email"]["verified"] is True
    assert "expired or was already used" in client.get(url + "&ui=en").text            # single use
    assert client.post("/auth/verify/resend").json() == {"sent": False}                 # nothing to confirm


def test_password_reset_flow_ends_old_sessions(mail):
    client, box = mail
    register(client)
    old_cookie = client.cookies.get(auth.COOKIE)
    stranger = TestClient(web.app)
    assert stranger.post("/auth/forgot", json={"email": "nobody@example.org"}).json() == {"sent": True}
    count = len(box)
    assert stranger.post("/auth/forgot", json={"email": "ali@example.org"}).json() == {"sent": True}   # same answer
    assert len(box) == count + 1 and "Reset your PlagIQ password" == box[-1][1]
    url = link(box, "reset")
    token = url.split("t=")[1]
    page = stranger.get(url + "&ui=en").text
    assert 'name="password"' in page and 'type="hidden" name="t"' in page
    weak = stranger.post("/auth/reset?ui=en", data={"t": token, "password": "short"}).text
    assert "at least 8 characters" in weak                                                # token still usable
    time.sleep(0.01)
    done = stranger.post("/auth/reset?ui=en", data={"t": token, "password": "a-new-password"}).text
    assert "Your password was changed" in done
    assert "expired or was already used" in stranger.post("/auth/reset", data={"t": token, "password": "another-one"}).text
    assert auth.read(old_cookie) is None                                                  # earlier session ended
    assert client.get("/api/me").json()["user"] is None
    assert stranger.post("/auth/login", json={"email": "ali@example.org", "password": "first-password"}).status_code == 401
    assert stranger.post("/auth/login", json={"email": "ali@example.org", "password": "a-new-password"}).status_code == 200
    assert stranger.get("/api/me").json()["user"]["id"] < 0                               # the new session works at once
    with sqlite3.connect(auth._db_path()) as db:
        stored = [h for (h,) in db.execute("SELECT hash FROM email_tokens")]
    assert token not in stored and all(len(h) == 64 for h in stored)                      # only hashes are kept


def test_links_expire_and_requests_are_limited(mail, monkeypatch):
    client, box = mail
    register(client)
    TestClient(web.app).post("/auth/forgot", json={"email": "ali@example.org"})
    token = link(box, "reset").split("t=")[1]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 2 * 3600)                          # the 1-hour link has expired
    with pytest.raises(auth.AccountError):
        auth.reset_password(token, "a-new-password")
    monkeypatch.setattr(time, "time", real)
    auth._SENDS.clear()
    other = TestClient(web.app)
    for _ in range(3):
        assert other.post("/auth/forgot", json={"email": "ali@example.org"}).status_code == 200
    limited = other.post("/auth/forgot", json={"email": "ali@example.org"})
    assert limited.status_code == 429 and limited.json()["code"] == "too_many_emails"


def test_verification_can_be_required(mail, monkeypatch):
    client, box = mail
    monkeypatch.setenv("EMAIL_VERIFY_REQUIRED", "1")
    refused = register(client, "vali@example.org")
    assert refused.status_code == 403 and refused.json()["code"] == "email_unverified"
    account = auth._connect().execute("SELECT id FROM email_accounts WHERE email=?",
                                                                 ("vali@example.org",)).fetchone()[0]
    monkeypatch.setenv("EMAIL_VERIFY_REQUIRED", "0")
    auth.request_verification({"id": -account}, "http://testserver")
    monkeypatch.setenv("EMAIL_VERIFY_REQUIRED", "1")
    client.get(link(box, "verify"))
    assert client.post("/auth/login", json={"email": "vali@example.org", "password": "first-password"}).status_code == 200


def test_everything_is_off_without_smtp(login, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.setenv("EMAIL_VERIFY_REQUIRED", "1")                     # cannot lock people out without mail
    assert register(login, "sami@example.org").status_code == 200
    assert login.get("/api/me").json()["user"]["id"] < 0                  # signed in as before
    config = login.get("/api/me").json()["auth"]
    assert config["email_recovery"] is False and config["email_verify_required"] is False
    off = TestClient(web.app).post("/auth/forgot", json={"email": "sami@example.org"})
    assert off.status_code == 503 and off.json()["code"] == "recovery_not_configured"


def test_telegram_sign_in_is_unchanged(mail):
    client, _ = mail
    assert sign_in(client).status_code == 200 and client.get("/api/me").json()["email"] is None


def test_one_network_is_not_limited_like_one_address(mail, monkeypatch):
    """Students behind one university address each get their own sign-in codes."""
    client, box = mail
    monkeypatch.setenv("EMAIL_SIGNIN", "1")
    for n in range(6):
        assert TestClient(web.app).post("/auth/email/start", json={"email": f"s{n}@example.org"}).status_code == 200
    assert len(box) == 6                                              # 6 sign-in e-mails from one network
