"""Reports of a signed-in account are kept ACCOUNT_REPORT_DAYS (7) days, others REPORT_HOURS (24); the page, the
privacy page and the "ready" message state the periods from the settings (Russian number forms included)."""
import time

from fastapi.testclient import TestClient

import app as web
import notify
import reports_store


def test_periods_and_number_forms(monkeypatch):
    assert reports_store.ttl() == 24 * 3600 and reports_store.ttl(True) == 7 * 86400
    assert [reports_store.period(l, True) for l in ("uz", "ru", "en")] == ["7 kun", "7 дней", "7 days"]
    assert reports_store.period("ru") == "24 часа"
    monkeypatch.setenv("ACCOUNT_REPORT_DAYS", "1")
    assert reports_store.period("ru", True) == "1 день" and reports_store.period("en", True) == "1 day"
    monkeypatch.setenv("ACCOUNT_REPORT_DAYS", "3")
    assert reports_store.period("ru", True) == "3 дня"
    monkeypatch.setenv("REPORT_HOURS", "48")
    assert reports_store.period("ru") == "48 часов"
    monkeypatch.setenv("ACCOUNT_REPORT_DAYS", "0")                  # off: the same period for everyone
    assert reports_store.ttl(True) == 48 * 3600 and reports_store.fill("__KEEP_EN__|__KEEPAFTER_UZ__") == "48 hours|48 soatdan keyin"


def test_new_rows_get_the_period_of_their_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DB", str(tmp_path / "r.db"))
    reports_store.start("a" * 32, "session", 42, "a.txt", "academic")
    reports_store.start("b" * 32, "session", None, "b.txt", "academic")
    now = time.time()
    assert reports_store.get("a" * 32)["expires"] > now + 6 * 86400
    assert now + 23 * 3600 < reports_store.get("b" * 32)["expires"] < now + 25 * 3600


def test_pages_and_message_state_the_periods():
    client = TestClient(web.app)
    page = client.get("/").text
    assert "__KEEP" not in page and "__HOURS" not in page
    assert "Hisobotlar hisobingizda 7 kun (tizimga kirmasdan — 24 soat) saqlanadi" in page
    assert "Отчёты удаляются через 7 дней" in page and "Reports deleted after 7 days" in page
    assert "My checks (last 24 hours)" in page
    privacy = client.get("/privacy").text
    assert "__KEEP" not in privacy and "в аккаунте 7 дней (без входа — 24 часа)" in privacy
    assert "deleted automatically after 7 days (24 hours if you are not signed in)" in privacy
    assert "Hisobot 7 kun saqlanadi" in notify.message("a.pdf", True, "uz", "https://x.uz")[1]
    assert "Ҳисобот 7 кун сақланади" in notify.message("a.pdf", True, "uzc", "https://x.uz")[1]
    assert "Отчёт хранится 7 дней" in notify.message("a.pdf", True, "ru", "https://x.uz")[1]


def test_pdf_time_left_in_days():
    import report_pdf
    assert report_pdf._duration(7 * 1440 - 1, "en") == "6 d 23 h"
    assert report_pdf._duration(3 * 1440, "uz") == "3 kun" and report_pdf._duration(1439, "ru") == "23 ч 59 мин"
