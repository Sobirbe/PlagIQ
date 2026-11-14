"""A CamScanner syllabus was searched by its stamp text ("UNIVERSITY OFWORLD Y g"). The PDF's stored title is now
used when the text confirms it; title-page headers and OCR junk are not taken as titles; scanner logos and
stray-symbol lines are dropped from OCR text."""
import io

import pymupdf
from fastapi.testclient import TestClient

import discovery
import extract

BODY = ("This course provides students with theoretical and practical knowledge of the economic nature of financial "
        "risks and the relationship between risk and return in modern markets. ") * 8


def test_stored_title_is_used_only_when_the_text_confirms_it():
    text = "UNIVERSITY OFWORLD Y g\nECONOMY AND DIPLOMACY\nCourse: Financial Risk Management\n" + BODY
    assert discovery._first_line_query(text, "Financial risk management ") == "Financial risk management"
    for stale in ("(anonymous)", "Microsoft Word - syllabus_final.docx", "Untitled", "Slide 1", "Scan",
                  "Gold Prices and Inflation"):                       # last one: words not in the text
        assert discovery.stored_title(stale, text) is None, stale
    assert discovery.suggest_queries(text, title="Financial risk management")[0] == "Financial risk management"


def test_title_page_headers_and_ocr_junk_are_not_titles():
    for line in ("UNIVERSITY OFWORLD Y g", "MINISTRY OF FOREIGN AFFAIRS OF THE REPUBLIC OF UZBEKISTAN",
                 "TOSHKENT DAVLAT IQTISODIYOT UNIVERSITETI", "ТАШКЕНТСКИЙ ГОСУДАРСТВЕННЫЙ ЭКОНОМИЧЕСКИЙ УНИВЕРСИТЕТ",
                 "DEPARTMENT OF INTERNATIONAL FINANCE AND INVESTMENTS", "На правах рукописи УДК 336",
                 "Scanned with CamScanner today", "4, eens R  ie WE)"):
        assert not discovery._looks_like_title(line), line
    for title in ("University rankings in the context of research evaluation", "Gold Prices and Inflation",
                  "PECULIARITIES OF MID-TERM PLANNING OF PUBLIC FINANCE IN UZBEKISTAN"):
        assert discovery._looks_like_title(title), title
    page = ("O‘ZBEKISTON RESPUBLIKASI OLIY TA’LIM, FAN VA INNOVATSIYALAR VAZIRLIGI\nTOSHKENT DAVLAT IQTISODIYOT UNIVERSITETI\n"
            "Qo‘lyozma huquqida\nRaqamli iqtisodiyot sharoitida tijorat banklarining kredit siyosati\n" + BODY)
    assert discovery._first_line_query(page) == "Raqamli iqtisodiyot sharoitida tijorat banklarining kredit siyosati"


def test_ocr_text_loses_scanner_logos_and_symbol_lines():
    raw = "Course description\n(9 CamScanner\n| ‘ ж\nRisk measures of the\nportfolio\n1,3 | 5 2\nCs]CamScanner\n"
    assert extract._clean_ocr(raw) == "Course description\nRisk measures of the\nportfolio\n1,3 | 5 2\n"
    assert extract._clean_ocr("of the\nva\n") == "of the\nva\n"            # short real words are kept


def test_pdf_title_reaches_the_search(monkeypatch):
    import app
    seen = {}

    def fake_discover(text, max_results=8, language=None, title=None):
        seen["title"] = title
        return {"status": "searched", "sources": [], "queries": 1, "message": "", "query_texts": []}
    monkeypatch.setattr(app, "discover", fake_discover)
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(60, 60, 535, 790), "Course: Financial Risk Management\n" + BODY, fontsize=10)
    doc.set_metadata({"title": "Financial risk management"})
    response = TestClient(app.app).post("/api/check", data={"language": "en", "mode": "academic", "consent": "true"},
                                        files={"submission": ("s.pdf", io.BytesIO(doc.tobytes()), "application/pdf")})
    assert response.status_code in (200, 422), response.text
    assert seen["title"] == "Financial risk management"
