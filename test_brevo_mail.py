"""E-mail through Brevo's web API (for hosting that blocks mail ports). A local fake Brevo server stands in for
api.brevo.com; no real e-mail is sent and no real key is used."""
import http.server
import json
import threading

import pytest
from fastapi.testclient import TestClient

import app as web
import mailer
from test_auth_ai import TOKEN, sign_in


@pytest.fixture()
def brevo(monkeypatch, tmp_path):
    seen, replies = [], []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append({"key": self.headers.get("api-key"), "path": self.path,
                         "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            status, body = replies.pop(0) if replies else (201, {"messageId": "<1@brevo>"})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(mailer, "BREVO_URL", f"http://127.0.0.1:{server.server_port}/v3/smtp/email")
    for k, v in {"BREVO_API_KEY": "xkeysib-test-not-real", "SMTP_FROM": "PlagIQ <codes@example.uz>",
                 "TELEGRAM_BOT_TOKEN": TOKEN, "USERS_DB": str(tmp_path / "u.sqlite3"), "OWNER_TELEGRAM_IDS": "42"}.items():
        monkeypatch.setenv(k, v)
    for k in ("SMTP_HOST", "MAIL_BACKEND", "EMAIL_SIGNIN"):
        monkeypatch.delenv(k, raising=False)
    yield seen, replies
    server.shutdown()


def test_brevo_sends_without_smtp(brevo):
    seen, _ = brevo
    assert mailer.configured() and mailer.backend() == "brevo"
    mailer.send("student@example.uz", "Your code", "123456")
    assert seen[0]["key"] == "xkeysib-test-not-real" and seen[0]["path"] == "/v3/smtp/email"
    assert seen[0]["body"] == {"sender": {"name": "PlagIQ", "email": "codes@example.uz"}, "to": [{"email": "student@example.uz"}],
                               "subject": "Your code", "textContent": "123456"}


def test_brevo_refusals_explained_on_admin(brevo, caplog):
    seen, replies = brevo
    client = TestClient(web.app)
    sign_in(client)
    replies.append((401, {"code": "unauthorized", "message": "Key not found"}))
    top = client.post("/admin/test-email", data={"email": "owner@example.uz"}).text.split("<section>", 1)[0]
    assert "Brevo HTTP 401" in top and "Key not found" in top and "BREVO_API_KEY" in top
    replies.append((400, {"code": "invalid_parameter", "message": "Sender is not valid"}))
    top = client.post("/admin/test-email", data={"email": "owner@example.uz"}).text.split("<section>", 1)[0]
    assert "Sender is not valid" in top and "verified sender" in top
    top = client.post("/admin/test-email", data={"email": "owner@example.uz"}).text.split("<section>", 1)[0]
    assert "sent to owner@example.uz through Brevo" in top
    assert "xkeysib-test-not-real" not in caplog.text


def test_sign_in_code_via_brevo(brevo, monkeypatch):
    seen, _ = brevo
    monkeypatch.setenv("EMAIL_SIGNIN", "1")
    client = TestClient(web.app)
    assert client.get("/api/me").json()["auth"]["email_codes"] is True
    assert client.post("/auth/email/start", json={"email": "new@example.uz"}).json()["sent"] is True
    assert seen[-1]["body"]["to"] == [{"email": "new@example.uz"}] and "sign-in code" in seen[-1]["body"]["subject"]
