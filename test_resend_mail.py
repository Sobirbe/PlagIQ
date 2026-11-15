"""E-mail through Resend's web API (for hosting that blocks mail ports). A local fake server stands in for
api.resend.com; no real e-mail is sent and no real key is used."""
import http.server
import json
import threading

import pytest
from fastapi.testclient import TestClient

import app as web
import mailer
from test_auth_ai import TOKEN, sign_in


@pytest.fixture()
def resend(monkeypatch, tmp_path):
    seen, replies = [], []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append({"auth": self.headers.get("Authorization"), "idem": self.headers.get("Idempotency-Key"),
                         "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            status, body, extra = replies.pop(0) if replies else (200, {"id": "email-1"}, {})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(mailer, "RESEND_URL", f"http://127.0.0.1:{server.server_port}/emails")
    monkeypatch.setattr(mailer.time, "sleep", lambda s: None)
    for k, v in {"RESEND_API_KEY": "re_test_not_real", "SMTP_FROM": "PlagIQ <codes@mail.example.uz>",
                 "TELEGRAM_BOT_TOKEN": TOKEN, "USERS_DB": str(tmp_path / "u.sqlite3"), "OWNER_TELEGRAM_IDS": "42"}.items():
        monkeypatch.setenv(k, v)
    for k in ("SMTP_HOST", "MAIL_BACKEND", "EMAIL_SIGNIN"):
        monkeypatch.delenv(k, raising=False)
    yield seen, replies
    server.shutdown()


def test_resend_sends_without_smtp(resend):
    seen, _ = resend
    assert mailer.configured() and mailer.backend() == "resend"
    assert mailer.send("student@example.uz", "Your code", "123456") == "email-1"
    assert seen[0]["auth"] == "Bearer re_test_not_real" and seen[0]["idem"].startswith("plagiq-")
    assert seen[0]["body"] == {"from": "PlagIQ <codes@mail.example.uz>", "to": ["student@example.uz"],
                               "subject": "Your code", "text": "123456"}


def test_retries_429_and_5xx_with_the_same_idempotency_key(resend):
    seen, replies = resend
    replies += [(429, {"name": "rate_limit_exceeded"}, {"retry-after": "1"}), (500, {"name": "internal_server_error"}, {})]
    assert mailer.send("a@example.uz", "s", "t") == "email-1"
    assert len(seen) == 3 and len({s["idem"] for s in seen}) == 1


def test_other_refusals_are_not_retried(resend):
    seen, replies = resend
    replies.append((403, {"name": "validation_error", "message": "The example.uz domain is not verified."}, {}))
    with pytest.raises(mailer.MailError) as err:
        mailer.send("a@example.uz", "s", "t")
    assert len(seen) == 1 and "403" in str(err.value) and "not verified" in str(err.value)


def test_refusals_explained_on_admin_without_the_key(resend, caplog):
    seen, replies = resend
    client = TestClient(web.app)
    sign_in(client)
    replies.append((401, {"name": "validation_error", "message": "API key is invalid"}, {}))
    top = client.post("/admin/test-email", data={"email": "owner@example.uz"}).text.split("<section>", 1)[0]
    assert "Resend HTTP 401" in top and "RESEND_API_KEY" in top
    replies.append((403, {"name": "validation_error", "message": "You can only send testing emails to your own email address"}, {}))
    top = client.post("/admin/test-email", data={"email": "owner@example.uz"}).text.split("<section>", 1)[0]
    assert "own email address" in top and "verified in Resend" in top
    top = client.post("/admin/test-email", data={"email": "owner@example.uz"}).text.split("<section>", 1)[0]
    assert "sent to owner@example.uz through Resend" in top
    assert "re_test_not_real" not in caplog.text and "re_test_not_real" not in top


def test_sign_in_code_via_resend(resend, monkeypatch):
    seen, _ = resend
    monkeypatch.setenv("EMAIL_SIGNIN", "1")
    client = TestClient(web.app)
    assert client.get("/api/me").json()["auth"]["email_codes"] is True
    assert client.post("/auth/email/start", json={"email": "new@example.uz"}).json()["sent"] is True
    assert seen[-1]["body"]["to"] == ["new@example.uz"] and "sign-in code" in seen[-1]["body"]["subject"]


def test_resend_is_used_before_brevo(resend, monkeypatch):
    seen, _ = resend
    monkeypatch.setenv("BREVO_API_KEY", "xkeysib-not-used")
    assert mailer.backend() == "resend"
    mailer.send("a@example.uz", "s", "t")
    assert seen[-1]["auth"] == "Bearer re_test_not_real"
