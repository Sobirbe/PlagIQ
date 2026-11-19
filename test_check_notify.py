""""Your check is ready" when the page was closed before the check ended (notify.py): only when the page stopped
polling, Telegram first for Telegram accounts, e-mail otherwise or when Telegram refuses, in the page's language,
with the file name and a link to "My checks" but no percentage or document text."""
import time

import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import mailer
import notify
import progress
from test_auth_ai import login, pilot_tester  # noqa: F401  (fixture)

TEXT = ("Remittance inflows reshape rural household budgets across the Fergana valley through seasonal consumption "
        "smoothing and informal credit networks. ") * 12
TOKEN = "ab" * 16


@pytest.fixture()
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "_start", lambda target, *args: target(*args))   # at once, not in the background
    monkeypatch.setattr(notify, "_telegram", lambda chat, body, link, button, opener=None: out.append(("telegram", chat, body, link)) or True)
    monkeypatch.setenv("MAIL_BACKEND", "dev")
    mailer.OUTBOX.clear()
    return out


def test_progress_tells_whether_the_page_is_still_polling(monkeypatch):
    assert progress.watching("") is None and progress.watching("cd" * 16) is None    # no token: cannot tell
    progress.begin(TOKEN)
    assert progress.watching(TOKEN)                                                  # just started
    clock = [time.monotonic() + 60]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    assert progress.watching(TOKEN) is False                                         # no poll for a minute
    progress.read(TOKEN)
    assert progress.watching(TOKEN)


def test_messages_in_the_page_language_without_percentages():
    subject, body, link, button = notify.message("Dissertatsiya.docx", True, "uz", "https://www.plagiq.uz/")
    assert subject == "PlagIQ: «Dissertatsiya.docx» tekshiruvi tayyor" and link == "https://www.plagiq.uz/#history"
    assert "Mening tekshiruvlarim" in body and "%" not in body and link in body
    assert "текширувлари" in notify.message("a.pdf", True, "uzc", "https://x.uz")[1]         # Cyrillic from Latin
    assert notify.message("a.pdf", False, "ru", "https://x.uz")[0] == "PlagIQ: проверка «a.pdf» не завершилась"
    assert notify.message("a.pdf", True, "xx", "https://x.uz")[0].endswith("tekshiruvi tayyor")  # unknown: Uzbek


def test_telegram_first_then_email(sent, monkeypatch):
    monkeypatch.setattr(auth, "identities", lambda user: ["telegram:42", "email:student@example.uz"])
    monkeypatch.setattr(auth, "telegram_enabled", lambda: True)
    assert notify.deliver({"id": 42}, "a.pdf", True, "en", "https://x.uz") == "telegram"
    assert sent[0][:2] == ("telegram", 42) and not mailer.OUTBOX
    monkeypatch.setattr(notify, "_telegram", lambda *a, **k: (_ for _ in ()).throw(OSError("403 bot blocked")))
    assert notify.deliver({"id": 42}, "a.pdf", True, "en", "https://x.uz") == "email"        # Telegram refused
    assert mailer.OUTBOX[-1]["to"] == "student@example.uz" and "a.pdf" in mailer.OUTBOX[-1]["subject"]
    monkeypatch.setattr(auth, "identities", lambda user: [])
    assert notify.deliver({"id": 42}, "a.pdf", True, "en", "https://x.uz") is None          # nowhere to send


def test_only_when_the_page_was_closed_and_switch_off(sent, monkeypatch):
    monkeypatch.setattr(auth, "identities", lambda user: ["email:student@example.uz"])
    user = {"id": -3}
    progress.begin(TOKEN)
    assert not notify.after_check(user, TOKEN, False, "a.pdf", True, "uz", "https://x.uz")    # page still polling
    assert not notify.after_check(None, "", True, "a.pdf", True, "uz", "https://x.uz")       # no account
    assert not notify.after_check(user, "", False, "a.pdf", True, "uz", "https://x.uz")      # no token, still connected
    assert notify.after_check(user, "", True, "a.pdf", True, "uz", "https://x.uz")           # no token, connection gone
    monkeypatch.setattr(progress, "watching", lambda token: False)
    monkeypatch.setenv("CHECK_NOTIFY", "0")
    assert not notify.after_check(user, TOKEN, True, "a.pdf", True, "uz", "https://x.uz")
    assert len(mailer.OUTBOX) == 1


def test_a_check_that_ends_after_the_page_left_sends_one_message(login, sent, monkeypatch):  # noqa: F811
    pilot_tester(login)
    monkeypatch.setattr(auth, "telegram_enabled", lambda: True)
    form = {"mode": "reference", "language": "en", "reference_text": TEXT[:600], "progress": TOKEN, "ui_lang": "ru"}
    login.post("/api/check", data=form, files={"submission": ("paper.txt", TEXT.encode())})
    assert sent == []                                                       # the page was watching (just started)
    monkeypatch.setattr(progress, "watching", lambda token: False)          # the page stopped polling
    login.post("/api/check", data=form, files={"submission": ("paper.txt", TEXT.encode())})
    assert len(sent) == 1 and sent[0][1] == 42
    assert "Проверка документа «paper.txt» завершена" in sent[0][2]
    page = TestClient(web.app).get("/").text
    assert 'body.append("ui_lang",current)' in page and "Natija tayyor bo‘lgach, sizga {c} orqali xabar beramiz" in page
