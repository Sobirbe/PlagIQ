"""Reports are kept for 24 hours: listed under "My checks", reopened from the same browser or the signed-in
account, then deleted. Checks cut off by a restart are marked interrupted and refunded."""
import io
import sqlite3
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app as web
import reports_store
from test_auth_ai import EN, login, sign_in      # noqa: F401  (fixture)
from test_payments import balance, pay, top_up   # noqa: F401  (fixture)

SOURCE = EN


def check(client, text=EN, name="essay.txt"):
    return client.post("/api/check", data={"language": "en", "mode": "reference", "reference_text": SOURCE},
                       files={"submission": (name, io.BytesIO(text.encode() if isinstance(text, str) else text))})


def test_listed_and_reopened_after_memory_is_gone(login):
    data = check(login).json()
    listing = login.get("/api/reports").json()
    assert listing["hours"] == 24 and len(listing["reports"]) == 1
    row = listing["reports"][0]
    assert row["id"] == data["report_id"] and row["state"] == "done" and row["filename"] == "essay.txt"
    assert row["status"] == "completed_matches" and row["percent"] == 100.0
    web.results.clear()                                            # e.g. after a restart
    reopened = login.get(f"/api/report/{data['report_id']}").json()
    assert reopened["reopened"] and reopened["percent"] == data["percent"]
    assert reopened["highlight_html"] == data["highlight_html"]
    assert reopened["sources"][0]["passages"][0]["query_excerpt"] == data["sources"][0]["passages"][0]["query_excerpt"]
    assert not any(k.startswith("_") for k in reopened)
    pdf = login.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=en")
    text = " ".join(p.get_text() for p in pymupdf.open(stream=pdf.content, filetype="pdf"))
    assert pdf.status_code == 200 and "Fergana valley" in text                     # highlighted appendix still there


def test_only_the_owner_sees_a_report(login):
    sign_in(login)
    data = check(login).json()
    stranger = TestClient(web.app)
    assert stranger.get("/api/reports").json()["reports"] == []
    assert stranger.get(f"/api/report/{data['report_id']}").status_code == 404
    other_device = TestClient(web.app)                              # same account, another browser
    sign_in(other_device)
    assert [r["id"] for r in other_device.get("/api/reports").json()["reports"]] == [data["report_id"]]
    assert other_device.get(f"/api/report/{data['report_id']}.pdf").status_code == 200
    someone_else = TestClient(web.app)
    sign_in(someone_else, user_id=77)
    assert someone_else.get(f"/api/report/{data['report_id']}").status_code == 404


def test_failed_checks_are_listed_and_rejected_requests_are_not(login):
    blank = pymupdf.open(); blank.new_page()
    response = check(login, blank.tobytes(), "scan.pdf")
    assert response.json()["status"] == "failed_extraction"
    login.post("/api/check", data={"language": "en", "mode": "academic"},          # no consent: not a check
               files={"submission": ("a.txt", io.BytesIO(EN.encode()))})
    rows = login.get("/api/reports").json()["reports"]
    assert [(r["state"], r["error"], r["filename"]) for r in rows] == [("failed", "no_text", "scan.pdf")]


def test_deleted_after_24_hours_and_on_request(login):
    first = check(login).json()["report_id"]
    second = check(login).json()["report_id"]
    assert login.delete(f"/api/report/{first}").json() == {"deleted": True}
    assert [r["id"] for r in login.get("/api/reports").json()["reports"]] == [second]
    assert reports_store.purge(time.time() + 24 * 3600 + 1) == 1
    assert login.get("/api/reports").json()["reports"] == []
    with sqlite3.connect(reports_store._path()) as db:
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2                   # incremental: space is given back
        assert db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0


def test_flagged_ai_passages_and_integrity_examples_are_never_stored(login):
    data = check(login).json()
    stored = reports_store.get(data["report_id"])["result"]
    assert not {"ai_passages", "integrity_examples", "highlight_html", "search_phrases"} & set(stored)


def test_account_deletion_removes_reports(login):
    sign_in(login)
    identifier = check(login).json()["report_id"]
    assert login.delete("/api/me").status_code == 200
    assert reports_store.get(identifier) is None


def test_check_cut_off_by_a_restart_is_refunded(pay):
    sign_in(pay)
    top_up(pay, 20000)
    user = {"id": 42}
    import payments
    charge = payments.charge(user, 15000, "reference check, 1200 words")
    reports_store.start("a" * 32, "session", 42, "thesis.docx", "reference")
    reports_store.charged("a" * 32, charge, 15000)
    assert balance(pay) == 5000
    web.recover_interrupted()                                      # what start-up does
    assert balance(pay) == 20000
    row = reports_store.listing("session", 42)[0]
    assert (row["state"], row["error"]) == ("failed", "interrupted")
    web.recover_interrupted()                                      # never refunded twice
    assert balance(pay) == 20000
