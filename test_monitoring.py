"""Owner monitoring: one row of figures per check (no text, file name or user), summarised on /admin."""
import io
import sqlite3
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

import admin
import app as web
import fulltext
import metrics
from test_auth_ai import EN, login, sign_in      # noqa: F401  (fixture)

SECRET_NAME = "Karimova_dissertation_final.txt"


def reference(client, text=EN, name=SECRET_NAME):
    return client.post("/api/check", data={"language": "en", "mode": "reference", "reference_text": EN},
                       files={"submission": (name, io.BytesIO(text if isinstance(text, bytes) else text.encode()))})


def test_checks_and_failures_are_counted_without_text_or_names(login):
    assert reference(login).status_code == 200
    blank = pymupdf.open(); blank.new_page()
    assert reference(login, blank.tobytes(), "scan.pdf").json()["status"] == "failed_extraction"
    login.post("/api/check", data={"language": "en", "mode": "academic"},             # refused (no consent): not a check
               files={"submission": ("a.txt", io.BytesIO(EN.encode()))})
    day = metrics.summary(24)
    assert day["checks"] == 2 and day["statuses"] == {"completed_matches": 1, "failed_extraction": 1}
    assert day["errors"] == {"no_text": 1} and day["timing"]["reference"]["checks"] == 1
    with sqlite3.connect(metrics._path()) as db:
        dump = " ".join(str(v) for row in db.execute("SELECT * FROM checks") for v in row)
    assert "Karimova" not in dump and "scan.pdf" not in dump and "remittance" not in dump.lower()


def test_coverage_problems_and_outside_services(login, monkeypatch):
    record = {"title": "W", "url": "https://doi.org/10.1/w", "year": 2024, "coverage": "abstract", "abstract": EN}
    monkeypatch.setattr(web, "discover", lambda text, *a: {"status": "partial", "failed_queries": 2, "sources": [record],
                                                           "queries": 4, "message": ""})
    monkeypatch.setattr(fulltext, "fetch_fulltexts", lambda *a, **k: {"attempted": 3, "compared": 1, "failed": 1, "timed_out": 1,
                                                                      "reasons": {"timed out": 1, "not a PDF": 1}})
    data = login.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true"},
                      files={"submission": ("m.txt", io.BytesIO(EN.encode()))}).json()
    assert data["status"] == "incomplete_coverage"
    day = metrics.summary(24)
    assert day["openalex"] == {"checks": 1, "queries": 4, "failed": 2}
    assert day["downloads"] == {"attempted": 3, "compared": 1, "cut_off": 1}
    assert day["issues"] == {"search_partial": 1, "downloads_cut_off": 1}


def test_old_figures_are_deleted_and_days_are_tashkent_dates(monkeypatch):
    old = time.time() - 100 * 86400
    with sqlite3.connect(metrics._path()) as db:
        metrics._connect().close()
        db.execute("INSERT INTO checks (time, mode, status) VALUES (?, 'reference', 'completed_no_matches')", (old,))
    metrics.record_failure("reference", "failed_internal", "internal_error", 1.0)
    with sqlite3.connect(metrics._path()) as db:
        assert db.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 1             # older than 90 days: gone
    # 20:00 UTC is already the next day in Tashkent (UTC+5)
    evening = time.mktime(time.strptime("2026-11-14 20:00", "%Y-%m-%d %H:%M")) - time.timezone
    metrics.record_check({"mode": "reference", "status": "completed_no_matches", "total_tokens": 10}, 1.0)
    with sqlite3.connect(metrics._path()) as db:
        db.execute("UPDATE checks SET time=? WHERE error IS NULL", (evening,))
    days = dict((d, c) for d, c, _ in metrics.per_day(3, now=evening + 3600))
    assert days.get("2026-11-15") == 1


def test_percentiles_and_ai_cost():
    for seconds in (1, 2, 3, 4, 100):
        metrics.record_check({"mode": "academic", "status": "completed_no_matches", "total_tokens": 250,
                              "ai": {"status": "done"}}, seconds)
    t = metrics.summary(24)["timing"]["academic"]
    assert (t["p50"], t["p95"], t["max"]) == (3, 100, 100)
    assert metrics.summary(24)["ai"] == {"checks": 5, "words": 1250, "usd": 0.75, "failed": 0}   # 3 x 100 words, 5 checks


def test_owner_sees_the_panel_and_others_do_not(login, monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    reference(login)
    assert TestClient(web.app).get("/admin").status_code == 404
    sign_in(login, user_id=42)
    page = login.get("/admin").text
    assert '<section id="monitoring"><h2>Monitoring</h2>' in page
    assert "Completed: matches found" in page and "Time per check" in page and "Stored reports (24 h)" in page
    assert SECRET_NAME not in page


def test_warning_when_many_checks_do_not_complete():
    for _ in range(4):
        metrics.record_failure("academic", "failed_search", "engine_unavailable", 2)
    for _ in range(2):
        metrics.record_check({"mode": "academic", "status": "completed_matches", "total_tokens": 10}, 2)
    html = admin.monitoring()
    assert "4 of 6 checks in the last 24 hours did not complete normally" in html
    assert "Library engine unreachable" in html
