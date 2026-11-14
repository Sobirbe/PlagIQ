"""Every check gets an explicit status; a partial or failed search never reads as a clean 0%."""
import io
from urllib.parse import parse_qs, urlsplit

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app as web
import checkstatus
import fulltext
import verify

TEXT = ("Remittance inflows have reshaped rural household budgets across the Fergana valley, and this study examines "
        "how families allocate these funds between consumption, education and small business investment. ") * 3
OTHER = "An unrelated abstract about wheat yields, irrigation scheduling and soil salinity in Karakalpakstan farms."


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("FULLTEXT_FETCH", raising=False)
    web.RATE.clear(); web.results.clear()
    return TestClient(web.app, raise_server_exceptions=False)


def academic(client, monkeypatch, discovery, fetch_stats=None):
    monkeypatch.setattr(web, "discover", lambda text, *a: {"message": "", "queries": 3, "query_texts": ["q"], **discovery})
    monkeypatch.setattr(fulltext, "fetch_fulltexts", lambda *a, **k: fetch_stats or
                        {"attempted": 0, "compared": 0, "failed": 0, "timed_out": 0, "reasons": {}})
    return client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true"},
                       files={"submission": ("m.txt", TEXT.encode())})


def rec(abstract):
    return {"title": "W", "url": "https://doi.org/10.1/w", "year": 2024, "coverage": "abstract", "abstract": abstract}


def test_complete_search_with_and_without_matches(client, monkeypatch):
    data = academic(client, monkeypatch, {"status": "searched", "sources": [rec(TEXT)]}).json()
    assert data["status"] == "completed_matches" and data["percent"] == 100.0 and data["coverage_issues"] == []
    data = academic(client, monkeypatch, {"status": "searched", "sources": [rec(OTHER)]}).json()
    assert data["status"] == "completed_no_matches" and data["percent"] == 0


def test_partial_search_and_cut_off_downloads_are_incomplete(client, monkeypatch):
    stats = {"attempted": 4, "compared": 1, "failed": 2, "timed_out": 1,
             "reasons": {"timed out": 1, "website not reachable": 1, "refused by the website (403)": 1}}
    data = academic(client, monkeypatch, {"status": "partial", "failed_queries": 2,
                                          "sources": [rec(TEXT[:300] + " " + OTHER)]}, stats).json()
    assert data["status"] == "incomplete_coverage"
    assert {"code": "search_partial", "count": 2} in data["coverage_issues"]
    assert {"code": "downloads_cut_off", "count": 2} in data["coverage_issues"]     # the 403 is not counted
    assert checkstatus.headline(data, "en") == f"at least {data['percent']}%"


def test_refused_downloads_alone_do_not_make_a_check_incomplete(client, monkeypatch):
    stats = {"attempted": 2, "compared": 0, "failed": 2, "timed_out": 0,
             "reasons": {"blocked by robots.txt": 1, "no PDF link on page": 1}}
    data = academic(client, monkeypatch, {"status": "searched", "sources": [rec(OTHER)]}, stats).json()
    assert data["status"] == "completed_no_matches"


def test_search_service_down_is_failed_search_without_a_percentage(client, monkeypatch):
    data = academic(client, monkeypatch, {"status": "unavailable", "sources": []}).json()
    assert data["status"] == "failed_search" and data["percent"] is None


def test_library_down_is_incomplete(client, monkeypatch):
    async def down(*a, **k):
        return {"searched": False, "error": "engine_unavailable"}
    monkeypatch.setattr(web, "search_library", down)
    data = academic(client, monkeypatch, {"status": "searched", "sources": [rec(TEXT)]}).json()
    assert data["status"] == "incomplete_coverage" and data["percent"] == 100.0
    assert data["coverage_issues"] == [{"code": "library_unavailable", "count": 1}]


def test_reports_and_verification_say_incomplete(client, monkeypatch):
    monkeypatch.setenv("VERIFY_SECRET", "s" * 40)
    stats = {"attempted": 1, "compared": 0, "failed": 0, "timed_out": 1, "reasons": {"timed out": 1}}
    data = academic(client, monkeypatch, {"status": "searched", "sources": [rec(TEXT)]}, stats).json()
    assert data["status"] == "incomplete_coverage"
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=en")
    text = " ".join(p.get_text() for p in pymupdf.open(stream=pdf.content, filetype="pdf")).replace("\n", " ")
    assert "at least 100.0%" in text and "Incomplete: part of the sources could not be searched" in text
    assert "full text(s) could not be downloaded in time" in text
    cached = web.results[data["report_id"]]["result"]
    url = verify.link(cached, data["report_id"], "https://plagiq.test")
    params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert params["c"] == "i" and verify.check(params)[0]
    page = client.get("/verify?" + urlsplit(url).query + "&ui=en").text
    assert "at least 100% — Incomplete" in page
    params.pop("c")
    assert not verify.check(params)[0]                     # the flag is signed: it cannot be removed


def test_reference_check_is_complete():
    result = {"mode": "reference", "percent": 0, "matched_tokens": 0}
    assert checkstatus.assess(result)["status"] == "completed_no_matches"


def test_errors_carry_a_status(client, monkeypatch):
    bad = client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT},
                      files={"submission": ("m.exe", b"MZ...")}).json()
    assert bad["code"] == "unsupported_type" and bad["status"] == "failed_extraction"
    empty = client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT},
                        files={"submission": ("m.pdf", _blank_pdf())}).json()
    assert empty["code"] == "no_text" and empty["status"] == "failed_extraction"

    async def crash(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(web, "analyse", crash)
    response = client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT},
                           files={"submission": ("m.txt", TEXT.encode())})
    assert response.status_code == 500
    assert response.json() == {"detail": response.json()["detail"], "code": "internal_error", "status": "failed_internal"}
    assert "Remittance" not in response.text


def test_engine_errors_are_failed_search():
    assert checkstatus.error_status("engine_unavailable", 503) == "failed_search"
    assert checkstatus.error_status("rate_limited", 429) == "rejected"


def _blank_pdf():
    doc = pymupdf.open()
    doc.new_page()
    return doc.tobytes()
