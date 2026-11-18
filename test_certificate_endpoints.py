"""The one-page certificate PDF, the sample report and certificate (the sample's PDF links used to point to
/api/report/undefined.pdf), and the optional author / title / document type on the certificate."""
import io

import pymupdf
from fastapi.testclient import TestClient

import app as web
from test_auth_ai import login, pilot_tester  # noqa: F401  (fixture)

TEXT = ("Remittance inflows reshape rural household budgets across the Fergana valley through seasonal consumption "
        "smoothing and informal credit networks. ") * 12


def pages(content):
    doc = pymupdf.open(stream=content)
    return doc.page_count, " ".join(" ".join(p.get_text() for p in doc).split())


def test_sample_report_and_certificate_download_and_say_sample():
    client = TestClient(web.app)
    for path in ("/api/sample-report.pdf?lang=uz", "/api/sample-report.pdf?full=1&lang=en", "/api/sample-certificate.pdf?lang=ru"):
        r = client.get(path)
        assert r.status_code == 200 and r.content.startswith(b"%PDF"), path
    n, text = pages(client.get("/api/sample-certificate.pdf?lang=uz").content)
    assert n == 1 and "NAMUNA" in text and "haqiqiy tekshiruv emas" in text and "/verify?" not in text
    assert "SAMPLE" in pages(client.get("/api/sample-report.pdf?full=1&lang=en").content)[1]
    page = client.get("/").text
    assert "/api/sample-certificate.pdf" in page and 'id="report-cert"' in page


def test_certificate_pdf_for_a_check_with_document_details(login, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VERIFY_SECRET", "v" * 40)
    pilot_tester(login)
    data = login.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT[:600],
                                           "doc_author": "  A. Karimov\u0007 ", "doc_title": "Household budgets",
                                           "doc_type": "dissertation"},
                      files={"submission": ("paper.txt", TEXT.encode())}).json()
    rid = data["report_id"]
    r = login.get(f"/api/report/{rid}/certificate.pdf?lang=en")
    assert r.status_code == 200 and 'filename="PlagIQ-certificate.pdf"' in r.headers["content-disposition"]
    n, text = pages(r.content)
    assert n == 1
    for expected in ("PLAGIQ — SIMILARITY CHECK STATEMENT", "Household budgets", "Author: A. Karimov",
                     "Document type: Doctoral dissertation", "File: paper.txt", "Sources with matches 1", "Originality:"):
        assert expected in text, expected
    assert web.results[rid]["result"]["document_meta"] == {"author": "A. Karimov", "title": "Household budgets",
                                                          "type": "dissertation"}
    assert TestClient(web.app).get(f"/api/report/{rid}/certificate.pdf").status_code == 404   # owner only
    bad = login.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT[:600],
                                          "doc_type": "<script>"}, files={"submission": ("p.txt", TEXT.encode())}).json()
    assert "document_meta" not in web.results[bad["report_id"]]["result"]          # unknown type ignored, nothing else given
