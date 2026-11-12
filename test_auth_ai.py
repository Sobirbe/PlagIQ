"""Telegram sign-in, sessions, account store, and the Pangram AI detector (simulated)."""
import hashlib
import hmac
import http.server
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import aidetect
import app as web
import auth
import verify

TOKEN = "123456789:AAtest-token-for-plagiq-tests_0123456789"
EN = ("The rapid growth of remittance inflows has reshaped rural household budgets across the Fergana valley, "
      "and this study examines how families allocate these funds between consumption and investment. ") * 6


def signed(data, token=TOKEN):
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    return {**data, "hash": hmac.new(hashlib.sha256(token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()}


@pytest.fixture()
def login(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "plagiq_bot")
    monkeypatch.setenv("USERS_DB", str(tmp_path / "users.sqlite3"))
    monkeypatch.delenv("LOGIN_REQUIRED", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    web.RATE.clear(); web.results.clear()
    return TestClient(web.app)


def sign_in(client, user_id=42, first="Dilnoza", last="Karimova"):
    data = signed({"id": user_id, "first_name": first, "last_name": last, "username": "dilnoza",
                   "auth_date": int(time.time())})
    return client.post("/auth/telegram", json=data)


def test_telegram_signature_checks():
    good = signed({"id": 1, "first_name": "A", "auth_date": int(time.time())})
    import os
    os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
    try:
        assert auth.check_telegram(good)["id"] == 1
        assert auth.check_telegram({**good, "first_name": "B"}) is None                    # tampered
        assert auth.check_telegram(signed({"id": 1, "auth_date": int(time.time())}, "999:other")) is None
        old = signed({"id": 1, "first_name": "A", "auth_date": int(time.time()) - 3 * 86400})
        assert auth.check_telegram(old) is None                                            # replay of old data
        assert auth.check_telegram({"id": 1}) is None and auth.check_telegram("x") is None
    finally:
        del os.environ["TELEGRAM_BOT_TOKEN"]


def test_session_cookie_cannot_be_forged(login):
    user = {"id": 7, "name": "A", "username": ""}
    cookie = auth.issue(user)
    assert auth.read(cookie) == user
    payload, sig = cookie.split(".")
    assert auth.read(payload + "." + sig[:-2] + "AA") is None
    assert auth.read(cookie, now=time.time() + 40 * 86400) is None                          # expired


def test_login_required_for_checks_and_history_recorded(login):
    files = {"submission": ("m.txt", (EN * 7).encode())}                     # over 1,000 words: sign-in needed
    blocked = login.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": EN}, files=files)
    assert blocked.status_code == 401 and blocked.json()["code"] == "login_required_long"
    config = login.get("/api/me").json()["auth"]
    assert {k: config[k] for k in ("enabled", "required", "bot_id", "bot_username", "telegram", "email", "anon_max_words")} == {
        "enabled": True, "required": True, "bot_id": "123456789", "bot_username": "plagiq_bot", "telegram": True,
        "email": True, "anon_max_words": 1000}
    response = sign_in(login)
    assert response.status_code == 200 and response.json()["user"]["name"] == "Dilnoza Karimova"
    assert "httponly" in response.headers["set-cookie"].lower() and "samesite=strict" in response.headers["set-cookie"].lower()
    data = login.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": EN},
                      files={"submission": ("m.txt", EN.encode())}).json()
    me = login.get("/api/me").json()
    assert me["user"]["id"] == 42 and me["history"][0]["percent"] == data["percent"]
    assert me["history"][0]["report"] == data["report_id"][:12] and me["history"][0]["words"] > 0
    import sqlite3
    dump = "\n".join(sqlite3.connect(auth._db_path()).iterdump())
    assert "remittance" not in dump.lower() and "m.txt" not in dump                         # no text, no file name
    assert login.post("/auth/logout").json() == {"signed_out": True}
    login.cookies.clear()
    assert login.get("/api/me").json()["user"] is None


def test_bad_login_and_account_deletion(login):
    bad = login.post("/auth/telegram", json={"id": 1, "auth_date": int(time.time()), "hash": "00"})
    assert bad.status_code == 401 and bad.json()["code"] == "login_failed"
    sign_in(login)
    assert login.delete("/api/me").json() == {"deleted": True}
    import sqlite3
    assert sqlite3.connect(auth._db_path()).execute("SELECT COUNT(*) FROM users").fetchone() == (0,)


def test_login_optional_and_off(login, monkeypatch):
    monkeypatch.setenv("LOGIN_REQUIRED", "0")
    ok = login.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": EN},
                    files={"submission": ("m.txt", EN.encode())})
    assert ok.status_code == 200
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert login.get("/health").json()["login"]["enabled"] is False
    assert login.post("/auth/telegram", json={}).json()["code"] == "login_not_configured"


# ------------------------------------------------------------------ AI detector

@pytest.fixture()
def pangram(monkeypatch):
    seen = {"posts": [], "polls": 0}
    result = {"stage": "STAGE_SUCCESS", "text": EN, "version": "4.1", "headline": "AI Assisted", "prediction": "",
              "prediction_short": "Mixed", "fraction_ai": 0.42, "fraction_ai_assisted": 0.13, "fraction_human": 0.45,
              "windows": [{"text": "The rapid growth of remittance inflows", "label": "AI-Generated", "ai_assistance_score": 0.97,
                           "confidence": "High", "start_index": 0, "end_index": 38, "word_count": 6, "is_humanized": True},
                          {"text": "Human passage here", "label": "Human Written", "ai_assistance_score": 0.02,
                           "confidence": "High", "start_index": 39, "end_index": 57, "word_count": 3, "is_humanized": False}]}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["posts"].append((self.headers["x-api-key"], body))
            if self.headers["x-api-key"] != "pg-key":
                return self.reply(401, {})
            self.reply(200, {"task_id": "123e4567-e89b-12d3-a456-426614174000"})

        def do_GET(self):
            seen["polls"] += 1
            self.reply(200, result if seen["polls"] > 1 else {"task_id": "x", "stage": "STAGE_PREPROCESSING"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PANGRAM_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PANGRAM_API_KEY", "pg-key")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    web.RATE.clear(); web.results.clear()
    yield seen
    server.shutdown()


def test_detect_polls_and_summarises(pangram):
    summary, flagged = aidetect.detect(EN, "en", 180, poll=0.01)
    assert summary["status"] == "done" and summary["fraction_ai"] == 42.0 and summary["fraction_ai_assisted"] == 13.0
    assert summary["verdict"] == "mixed" and summary["segments"] == 2 and summary["segments_flagged"] == 1
    assert summary["humanized_segments"] == 1 and summary["billed_units"] == 2 and summary["version"] == "4.1"
    assert flagged == [{"label": "ai", "score": 0.97, "confidence": "High", "humanized": True, "words": 6,
                        "text": "The rapid growth of remittance inflows"}]
    key, body = pangram["posts"][0]
    assert key == "pg-key" and body == {"text": EN, "model": "pangram-4", "public_dashboard_link": False}


def test_uzbek_is_never_sent_and_errors_are_reported(pangram, monkeypatch):
    assert aidetect.detect("o‘zbekcha matn " * 20, "uz", 40)[0] == {"status": "unsupported_language",
                                                                    "provider": "Pangram", "language": "uz"}
    monkeypatch.setenv("AI_MAX_WORDS", "100")
    assert aidetect.detect(EN, "en", 180)[0]["status"] == "too_long"
    assert pangram["posts"] == []
    monkeypatch.setenv("AI_MAX_WORDS", "15000")
    monkeypatch.setenv("PANGRAM_API_KEY", "wrong")
    assert aidetect.detect(EN, "en", 180)[0] == {"status": "error", "provider": "Pangram", "error": "key_invalid"}


def test_ai_check_endpoint_consent_cache_pdf_and_qr(pangram, monkeypatch):
    monkeypatch.setattr(aidetect.time, "sleep", lambda s: None)
    client = TestClient(web.app)
    files = lambda: {"submission": ("m.txt", EN.encode())}
    refused = client.post("/api/check", data={"mode": "ai", "language": "en"}, files=files())
    assert refused.json()["code"] == "ai_consent_required" and pangram["posts"] == []
    data = client.post("/api/check", data={"mode": "ai", "language": "en", "ai_consent": "true"}, files=files()).json()
    assert data["mode"] == "ai" and data["ai"]["fraction_ai"] == 42.0 and data["ai_passages"][0]["label"] == "ai"
    cached = repr(web.results[data["report_id"]])
    assert "ai_passages" not in cached and "The rapid growth of remittance inflows" not in cached
    monkeypatch.setenv("VERIFY_SECRET", "v" * 40)
    import pymupdf
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?lang=uz").content
    text = " ".join("".join(p.get_text() for p in pymupdf.open(stream=pdf)).split())
    assert "AI yozgan yoki AI yordamida (Pangram bahosi)" in text and "55.0%" in text
    assert "AI yozgan 42.0% · AI yordamida 13.0% · inson 45.0%" in text and "Faqat AI detektor" in text
    assert "The rapid growth of remittance inflows" not in text
    facts = verify.facts(web.results[data["report_id"]]["result"], data["report_id"])
    assert facts["m"] == "i" and facts["x"] == "55"
    url = verify.link(web.results[data["report_id"]]["result"], data["report_id"], "https://p.example")
    from urllib.parse import parse_qsl, urlsplit
    params = dict(parse_qsl(urlsplit(url).query))
    assert verify.check(params)[0] and not verify.check({**params, "x": "5"})[0]           # AI figure is signed
    page = client.get("/verify?" + urlsplit(url).query + "&ui=en").text
    assert "AI detector estimate (Pangram)" in page and "55%" in page


def test_ai_alongside_plagiarism_check(pangram):
    client = TestClient(web.app)
    data = client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": EN,
                                           "ai_check": "true", "ai_consent": "true"},
                       files={"submission": ("m.txt", EN.encode())}).json()
    assert data["percent"] == 100.0 and data["ai"]["status"] == "done"
    uz = client.post("/api/check", data={"mode": "reference", "language": "uz", "reference_text": "Bu o‘zbekcha matn " * 20,
                                         "ai_check": "true", "ai_consent": "true"},
                     files={"submission": ("m.txt", ("Bu o‘zbekcha matn " * 20).encode())}).json()
    assert uz["ai"]["status"] == "unsupported_language" and len(pangram["posts"]) == 1


def test_old_qr_links_without_ai_field_still_verify(monkeypatch):
    monkeypatch.setenv("VERIFY_SECRET", "v" * 40)
    result = {"mode": "academic", "percent": 3.5, "total_tokens": 100, "language": "en", "date": "2026-11-11T07:00:00",
              "sha256": "ab" * 32, "fulltext": {"compared": 1}, "sources": []}
    fields = verify.facts(result, "c" * 32)
    assert "x" not in fields
    from urllib.parse import parse_qsl, urlsplit
    assert verify.check(dict(parse_qsl(urlsplit(verify.link(result, "c" * 32, "https://p")).query)))[0]
