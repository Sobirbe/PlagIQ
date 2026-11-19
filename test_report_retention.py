"""Without an account reports are kept REPORT_HOURS (24); a signed-in account's until deleted (or ACCOUNT_REPORT_DAYS),
at most the newest ACCOUNT_REPORT_LIMIT (50) per account, and the oldest (over a week) go while the disk is low. The
page, the privacy page, the PDF and the "ready" message state the periods from the settings."""
import shutil
import time
from collections import namedtuple

from fastapi.testclient import TestClient

import app as web
import notify
import report_pdf
import reports_store


def test_periods_and_number_forms(monkeypatch):
    assert reports_store.ttl() == 24 * 3600 and reports_store.ttl(True) == reports_store.FOREVER
    assert [reports_store.period(l, True) for l in ("uz", "ru", "en")] == ["o‘chirilguncha", "до удаления", "until deleted"]
    assert reports_store.period("ru") == "24 часа" and reports_store.period("en") == "for 24 hours"
    monkeypatch.setenv("ACCOUNT_REPORT_DAYS", "1")
    assert reports_store.period("ru", True) == "1 день" and reports_store.period("en", True) == "for 1 day"
    monkeypatch.setenv("ACCOUNT_REPORT_DAYS", "3")
    assert reports_store.period("ru", True) == "3 дня" and reports_store.ttl(True) == 3 * 86400
    monkeypatch.setenv("REPORT_HOURS", "48")
    assert reports_store.period("ru") == "48 часов"
    assert reports_store.fill("__KEEP_EN__|__HOURS_EN__") == "in your account for 3 days (48 hours without signing in)|48 hours"


def rows(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DB", str(tmp_path / "r.db"))
    monkeypatch.setenv("ACCOUNT_REPORT_LIMIT", "10")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(1000, 500, 500))   # plenty of room


def test_kept_until_deleted_newest_per_account_and_space(tmp_path, monkeypatch):
    rows(tmp_path, monkeypatch)
    for i in range(12):
        reports_store.start(f"{i:032x}", "s", 42, "a.txt", "academic")
        reports_store.finish(f"{i:032x}", {"status": "completed_no_matches", "total_tokens": 10})
        time.sleep(0.002)
    reports_store.start("f" * 32, "s", None, "b.txt", "academic")
    now = time.time()
    assert reports_store.get(f"{0:032x}")["expires"] > now + 50 * 365 * 86400
    assert now + 23 * 3600 < reports_store.get("f" * 32)["expires"] < now + 25 * 3600
    assert reports_store.purge() == 2                                   # the 2 oldest of 12 (limit 10)
    assert reports_store.get(f"{0:032x}") is None and reports_store.get(f"{2:032x}")
    assert reports_store.purge(now + 365 * 86400) == 1                  # a year on: only the one without an account
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(1000, 900, 100))   # 10 % free
    assert reports_store.purge(now + 6 * 86400) == 0                   # under a week old: kept
    assert reports_store.purge(now + 8 * 86400) == 10                  # older than a week: deleted while low


def test_pages_pdf_and_message_state_the_periods():
    client = TestClient(web.app)
    page = client.get("/").text
    assert "__KEEP" not in page and "__HOURS" not in page
    assert "Hisobotlar hisobingizda o‘chirilguncha (tizimga kirmasdan — 24 soat) saqlanadi" in page
    assert "Отчёты хранятся до удаления" in page and "Reports kept until deleted" in page
    assert "My checks (last 24 hours)" in page
    privacy = client.get("/privacy").text
    assert "__KEEP" not in privacy and "в аккаунте до удаления (без входа — 24 часа)" in privacy
    assert "An account keeps its newest 50 reports" in privacy
    assert "Hisobot hisobingizda o‘chirilguncha saqlanadi." in notify.message("a.pdf", True, "uz", "https://x.uz")[1]
    assert "Ҳисобот ҳисобингизда ўчирилгунча сақланади." in notify.message("a.pdf", True, "uzc", "https://x.uz")[1]
    assert "Отчёт хранится в аккаунте до удаления." in notify.message("a.pdf", True, "ru", "https://x.uz")[1]
    assert report_pdf._duration(7 * 1440 - 1, "en") == "6 d 23 h"
