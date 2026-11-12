"""PhD/DSc-size documents are accepted; the Telegram bot answers /start (Telegram simulated)."""
import io
import json

import pytest
from docx import Document
from fastapi.testclient import TestClient

import app as web
import extract
import pricing
import telegram_bot
from test_auth_ai import TOKEN

WORDS = ["tadqiqot", "natija", "tahlil", "usul", "dissertatsiya", "ilmiy", "manba", "xulosa", "jarayon", "model",
         "iqtisodiyot", "taraqqiyot", "omil", "samaradorlik", "baholash", "ko'rsatkich", "tizim", "sohasi"]


def long_text(words):
    # Deterministic, varied text: each sentence differs, so seven-word windows are not all identical.
    out = []
    for i in range(words):
        out.append(WORDS[(i * 7 + i // 13) % len(WORDS)] + ("." if i % 17 == 16 else ""))
    return " ".join(out)


@pytest.fixture()
def client(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "PAYMENTS_ENABLED", "PRIVATE_PILOT_USER", "PRIVATE_PILOT_PASSWORD", "TELEGRAM_BOT_REPLIES"):
        monkeypatch.delenv(name, raising=False)
    web.RATE.clear(); web.results.clear()
    return TestClient(web.app)


def test_phd_plan_and_limits():
    assert pricing.quote(90000)["id"] == "phd" and pricing.quote(150001)["fits"] is False
    assert web.MAX_BYTES == 20 * 1024 * 1024 and web.MAX_CHARS >= 1_000_000 and extract.MAX_PDF_PAGES >= 400


def test_dissertation_docx_is_checked(client):
    text = long_text(100_000)
    doc = Document()
    for start in range(0, len(text), 2000):
        doc.add_paragraph(text[start:start + 2000])
    payload = io.BytesIO()
    doc.save(payload)
    quote = client.post("/api/quote", files={"submission": ("phd.docx", payload.getvalue())}).json()
    assert quote["id"] == "phd" and quote["words"] >= 99_000
    reference = text[50_000:52_000]
    r = client.post("/api/check", data={"language": "uz", "mode": "reference", "reference_text": reference},
                    files={"submission": ("phd.docx", payload.getvalue())})
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["id"] == "phd" and r.json()["percent"] > 0
    assert client.get(f"/api/report/{r.headers['x-report-id']}.pdf").status_code == 200


def test_long_pdf_is_read():
    import pymupdf as fitz
    pdf = fitz.open()
    for _ in range(300):
        pdf.new_page().insert_text((40, 60), "dissertatsiya natija tahlil usul manba xulosa", fontsize=9)
    assert "dissertatsiya" in extract.extract_text("d.pdf", pdf.tobytes())


# ------------------------------------------------------------- Telegram bot

@pytest.fixture()
def bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_REPLIES", "1")
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://plagiq.example")
    sent = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(request, timeout=10):
        sent.append((request.full_url, json.loads(request.data)))
        return Response(b'{"ok": true, "description": "Webhook was set"}')

    monkeypatch.setattr(telegram_bot.urllib.request, "urlopen", fake_open)
    return sent


def update(text="/start", lang="uz", chat_type="private"):
    return {"update_id": 1, "message": {"message_id": 5, "text": text, "chat": {"id": 777, "type": chat_type},
                                        "from": {"id": 777, "language_code": lang}}}


def test_start_gets_reply_with_site_button(client, bot):
    secret = telegram_bot.secret()
    assert client.post("/telegram/webhook", json=update(), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert client.post("/telegram/webhook", json=update()).status_code == 403
    r = client.post("/telegram/webhook", json=update(), headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert r.status_code == 200 and len(bot) == 1
    url, payload = bot[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and payload["chat_id"] == 777
    assert "Telegram orqali kirish" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://plagiq.example/"
    client.post("/telegram/webhook", json=update(lang="ru"), headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert "Войти через Telegram" in bot[1][1]["text"]
    client.post("/telegram/webhook", json=update(chat_type="group"), headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert len(bot) == 2                                   # groups are ignored


def test_webhook_off_by_default(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert not telegram_bot.enabled()
    assert client.post("/telegram/webhook", json=update(),
                       headers={"X-Telegram-Bot-Api-Secret-Token": telegram_bot.secret()}).status_code == 403


def test_register_sets_webhook_with_secret(bot):
    ok, detail = telegram_bot.register()
    url, payload = bot[0]
    assert ok and url.endswith("/setWebhook") and payload["url"] == "https://plagiq.example/telegram/webhook"
    assert payload["secret_token"] == telegram_bot.secret() and payload["allowed_updates"] == ["message"]
    assert telegram_bot.STATUS == {"ok": True, "detail": "Webhook was set"}


def test_webhook_passes_pilot_password(client, bot, monkeypatch):
    monkeypatch.setenv("PRIVATE_PILOT_USER", "pilot")
    monkeypatch.setenv("PRIVATE_PILOT_PASSWORD", "a-long-pilot-password")
    r = client.post("/telegram/webhook", json=update(), headers={"X-Telegram-Bot-Api-Secret-Token": telegram_bot.secret()})
    assert r.status_code == 200
