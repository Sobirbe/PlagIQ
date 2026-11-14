"""The report's owner can exclude sources (e.g. their own earlier paper) and get a recalculated report; the
original figures stay visible and can be restored; PDFs, certificate, QR code and CSV follow the choice."""
import csv
import io
from urllib.parse import parse_qs, urlsplit

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app as web
import engine_client
import fulltext
import verify

A = ("Remittance inflows have reshaped rural household budgets across the Fergana valley, and this study examines how "
     "families allocate these funds between consumption, education and small business investment.")
B = ("Survey data from four hundred households show that remittances raise school enrolment and reduce seasonal "
     "borrowing, while the effect on agricultural investment remains small and concentrated among larger farms.")
SHARED = "The results suggest that financial literacy programmes and local credit cooperatives could channel more transfers."
OWN = "My own introduction explains why I chose this topic and how my fieldwork in Namangan was organised last spring."
DOC = OWN + " " + A + " " + SHARED + " " + B


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VERIFY_SECRET", "s" * 40)
    sources = [{"title": "My own 2024 article", "url": "https://doi.org/10.1/own", "year": 2024, "coverage": "abstract",
                "abstract": A + " " + SHARED},
               {"title": "=HYPERLINK(\"http://evil\")", "url": "https://doi.org/10.1/other", "year": 2023,
                "coverage": "abstract", "abstract": SHARED + " " + B}]
    monkeypatch.setattr(web, "discover", lambda text, *a: {"status": "searched", "sources": [dict(s) for s in sources],
                                                           "queries": 3, "message": ""})
    monkeypatch.setattr(fulltext, "fetch_fulltexts", lambda *a, **k: {"attempted": 0, "compared": 0, "failed": 0,
                                                                      "timed_out": 0, "reasons": {}})
    web.RATE.clear(); web.results.clear()
    return TestClient(web.app)


def run(client):
    return client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true"},
                       files={"submission": ("m.txt", io.BytesIO(DOC.encode()))}).json()


def index_of(data, title):
    return next(i for i, s in enumerate(data["sources"]) if s["title"] == title)


def test_exclude_recalculates_and_restore_brings_back_the_original(client):
    data = run(client)
    own = index_of(data, "My own 2024 article")
    excluded = client.post(f"/api/report/{data['report_id']}/exclude", json={"sources": [own]}).json()
    words = lambda t: len(t.split())
    assert data["matched_tokens"] == words(A) + words(SHARED) + words(B)
    assert excluded["matched_tokens"] == words(SHARED) + words(B)          # shared text still counts (other source has it)
    assert excluded["percent"] < data["percent"]
    assert excluded["before_source_exclusion"]["percent"] == data["percent"]
    assert excluded["excluded_sources"] == [{"index": own, "title": "My own 2024 article", "words": words(A) + words(SHARED)}]
    assert excluded["sources"][own]["excluded"] and 'data-s=' in excluded["highlight_html"]
    assert '">Remittance' in data["highlight_html"] and '">Remittance' not in excluded["highlight_html"]   # A no longer marked
    assert '">The results suggest' in excluded["highlight_html"]                                        # shared text still is
    restored = client.post(f"/api/report/{data['report_id']}/exclude", json={"sources": []}).json()
    for key in ("percent", "percent_all", "matched_tokens", "status", "highlight_html"):
        assert restored[key] == data[key], key
    assert "excluded_sources" not in restored and not any(s.get("excluded") for s in restored["sources"])


def test_choice_is_kept_and_shown_in_pdf_certificate_and_qr(client):
    data = run(client)
    rid = data["report_id"]
    client.post(f"/api/report/{rid}/exclude", json={"sources": [index_of(data, "My own 2024 article")]})
    web.results.clear()                                                    # reopened from the 24-hour store
    reopened = client.get(f"/api/report/{rid}").json()
    assert reopened["excluded_sources"] and reopened["percent"] < data["percent"]
    pdf = client.get(f"/api/report/{rid}.pdf?full=1&lang=en")
    text = " ".join(p.get_text() for p in pymupdf.open(stream=pdf.content, filetype="pdf")).replace("\n", " ")
    assert "Sources excluded by the report owner" in text and "(excluded by the report owner)" in text
    assert "1 source(s) excluded by the report owner (before:" in text
    url = verify.link(reopened, rid, "https://plagiq.test")
    params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert params["e"] == "1" and params["p"] == f"{float(reopened['percent']):g}" and verify.check(params)[0]
    assert "Sources excluded by the report owner" in client.get("/verify?" + urlsplit(url).query + "&ui=en").text


def test_only_the_owner_and_only_sources_with_positions(client):
    data = run(client)
    rid = data["report_id"]
    assert TestClient(web.app).post(f"/api/report/{rid}/exclude", json={"sources": [0]}).status_code == 404
    assert client.post(f"/api/report/{rid}/exclude", json={"wrong": 1}).status_code == 400
    web.results[rid]["result"]["sources"][0]["spans"] = []                 # e.g. from an older engine
    response = client.post(f"/api/report/{rid}/exclude", json={"sources": [0]})
    assert response.status_code == 400 and response.json()["code"] == "source_not_excludable"


def test_source_list_csv(client):
    data = run(client)
    rid = data["report_id"]
    client.post(f"/api/report/{rid}/exclude", json={"sources": [index_of(data, "My own 2024 article")]})
    response = client.get(f"/api/report/{rid}/sources.csv")
    assert response.headers["content-type"].startswith("text/csv") and response.content.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][:3] == ["Rank", "Title", "Link or DOI"] and len(rows) == 3
    titles = {r[1]: r for r in rows[1:]}
    assert titles["My own 2024 article"][-1] == "yes"
    assert "'=HYPERLINK(\"http://evil\")" in titles                     # no spreadsheet formula
    assert TestClient(web.app).get(f"/api/report/{rid}/sources.csv").status_code == 404


def test_library_sources_carry_validated_spans():
    report = {"query_text_length": 100, "unique_passages": [[0, 50]], "language_counts": {"uz": 0, "ru": 0, "en": 1},
              "corpus_version": "0123456789ab",
              "sources": [{"source_filename": "S", "source_url": "https://e.org", "matched_chars": 50, "matched_words": 8,
                           "intervals": [[0, 20], [20, 50]], "passages": []}]}
    assert engine_client.summarize(report)["sources"][0]["spans"] == [[0, 50]]
    report["sources"][0]["intervals"] = [[40, 500]]
    with pytest.raises(engine_client.EngineError):
        engine_client.summarize(report)
