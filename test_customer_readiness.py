"""Customer trust and transparency: operator-filled privacy notice and support details (never invented), honest
library counts (academic vs Wikipedia vs deposits), a labelled sample report computed by the real matcher, and
error messages that tell form mistakes from failures."""
import pytest
from fastapi.testclient import TestClient

import admin
import app as web
import corpus
import engine_client
import operator_info
import sample_report

FIELDS = {"OPERATOR_NAME": "Example Research LLC", "OPERATOR_ADDRESS": "1 Example street, Tashkent",
          "SUPPORT_EMAIL": "support@example.org", "SUPPORT_TELEGRAM": "@plagiq_help", "PRIVACY_UPDATED": "2026-12-01",
          "PRIVACY_DATA_LOCATION_UZ": "UZ joylashuv", "PRIVACY_DATA_LOCATION_RU": "RU расположение", "PRIVACY_DATA_LOCATION_EN": "EN location"}


@pytest.fixture
def clean(monkeypatch):
    for name in list(FIELDS) + ["PRIVACY_REVIEWED", "SUPPORT_HOURS"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_privacy_notice_shows_pending_fields_until_the_operator_fills_them(clean):
    page = TestClient(web.app).get("/privacy").text
    assert page.count("to be completed by the operator") >= 5 and "Draft." in page and "{{" not in page
    for name, value in FIELDS.items():
        clean.setenv(name, value)
    clean.setenv("PRIVACY_REVIEWED", "1")
    page = TestClient(web.app).get("/privacy").text
    assert "to be completed by the operator" not in page and "Draft." not in page and "Awaiting confirmation" not in page
    assert "Example Research LLC" in page and "EN location" in page and "RU расположение" in page and "@plagiq_help" in page
    clean.setenv("OPERATOR_NAME", "<script>alert(1)</script>")
    assert "<script>alert" not in TestClient(web.app).get("/privacy").text                 # escaped


def test_support_details_and_owner_checklist(clean):
    client = TestClient(web.app)
    assert client.get("/api/support").json() == {"email": "", "telegram": "", "hours": "", "operator": ""}
    clean.setenv("SUPPORT_EMAIL", "support@example.org")
    clean.setenv("SUPPORT_TELEGRAM", "not a valid name!")
    assert client.get("/api/support").json()["telegram"] == ""                             # only real usernames
    clean.setenv("SUPPORT_TELEGRAM", "@plagiq_help")
    assert client.get("/api/support").json()["telegram"] == "plagiq_help"
    row = next(r for r in admin.checklist(None) if r[0] == "Privacy notice and support contact")
    assert row[1] is False and "OPERATOR_NAME" in row[2] and "PRIVACY_REVIEWED" in row[2]


def test_sample_report_is_labelled_and_computed_by_the_matcher():
    data = TestClient(web.app).get("/api/sample-report").json()
    assert data["sample"] is True and "report_id" not in data
    assert all("fictional" in s["title"] for s in data["sources"]) and all(not s["url"] for s in data["sources"])
    # the percentage is a real measurement on the sample texts, not a typed-in number
    q, _, covered, intervals = corpus.compare_texts(sample_report.DOCUMENT,
                                                    [s.get("fulltext") or s.get("abstract") for s in sample_report.SOURCES], "en")
    assert data["matched_tokens"] == corpus.words_in_intervals(q, intervals) and data["total_tokens"] == len(q)
    types = {p["type"] for s in data["sources"] for p in s["passages"]}
    assert types == {"exact", "edited"} and data["citation"]["quotations_cited"] == 1
    assert data["status"] == "completed_matches" and "<mark" in data["highlight_html"]


def test_library_counts_separate_academic_wikipedia_and_deposits(tmp_path):
    db = corpus.connect(str(tmp_path / "c.db"))
    base = {"authors": "A", "year": 2024, "language": "en", "rights_basis": "open_license", "permission_reference": "CC BY"}
    corpus.ingest(db, {**base, "title": "Journal article", "url": "https://journal.uz/a"}, "An academic article about remittances. " * 5)
    corpus.ingest(db, {**base, "title": "Wiki", "url": "https://en.wikipedia.org/wiki/X", "source_type": "wikipedia"}, "Encyclopaedia text about something. " * 5)
    corpus.ingest(db, {**base, "title": "Thesis", "url": "", "rights_basis": "author_deposit", "owner": "u1"}, "A deposited thesis about trade. " * 5)
    counts = corpus.search(db, "unrelated words here for counting only", "en")["collection_counts"]
    assert counts["en"] == {"academic": 1, "wikipedia": 1, "deposits": 1} and counts["uz"] == {}


def test_engine_collection_counts_are_validated():
    good = engine_client._collections({"en": {"academic": 3, "wikipedia": 1, "deposits": 0}, "xx": {"academic": 9}})
    assert good == {"en": {"academic": 3, "wikipedia": 1, "deposits": 0}}
    with pytest.raises(ValueError):
        engine_client._collections({"en": {"academic": -1}})
    assert engine_client._collections(None) == {}


def test_error_messages_tell_mistakes_from_failures():
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto("file://" + str(Path(web.__file__).with_name("index.html")))
        page.locator("[data-lang=en]").click()
        out = page.evaluate("""() => [errorPrefix({code:'choose'}), errorPrefix({code:'no_text', status:'failed_extraction'}),
                                       errorPrefix({code:'engine_unavailable', status:'failed_search'}), errorPrefix({code:'network'}),
                                       errorPrefix({code:'whatever', status:'rejected'})]""")
        browser.close()
    assert out == ["Please check: ", "This file could not be read: ", "Check failed: ", "Check failed: ", "Please check: "]
