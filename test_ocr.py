"""Scanned PDF pages are read by OCR (Tesseract); bounded, reported, and switched off cleanly."""
import io

import pymupdf
import pytest
from fastapi.testclient import TestClient

import extract
from app import app

pytestmark = pytest.mark.skipif(not extract.ocr_available(), reason="Tesseract with uzb, uzb_cyrl, rus, eng is not installed")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
EN = ("Remittance inflows have reshaped rural household budgets across the Fergana valley, and this study examines "
      "how families allocate these funds between consumption, education and small business investment. Survey data "
      "from four hundred households show that remittances raise school enrolment and reduce seasonal borrowing. ") * 2
UZ = ("Ushbu maqolada Farg‘ona vodiysidagi qishloq xo‘jaligi korxonalarining moliyaviy barqarorligi tahlil qilinadi. "
      "Tadqiqot natijalari kichik fermer xo‘jaliklarida kredit olish imkoniyati cheklanganini va sug‘urta xizmatlari "
      "yetarli darajada rivojlanmaganini ko‘rsatadi. Muallif davlat qo‘llab-quvvatlash dasturlarini takomillashtirishni taklif etadi. ") * 2


def scanned(*pages, dpi=200):
    """A PDF whose pages are pictures only (no text layer), as a scanner makes."""
    out = pymupdf.open()
    for text in pages:
        src = pymupdf.open()
        page = src.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 535, 790), text, fontfile=FONT, fontname="serif", fontsize=12)
        png = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY).tobytes("png")
        img = out.new_page(width=page.rect.width, height=page.rect.height)
        img.insert_image(img.rect, stream=png)
    return out.tobytes()


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setenv("OCR_DPI", "200")


def test_scanned_pages_are_read_and_checked(fast):
    client = TestClient(app)
    response = client.post("/api/check", data={"language": "en", "mode": "reference", "reference_text": EN},
                           files={"submission": ("scan.pdf", io.BytesIO(scanned(EN, EN)), "application/pdf")})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ocr"]["pages"] == 2 and result["ocr"]["models"] == "eng"
    assert result["percent"] >= 95
    pdf = client.get("/api/report/" + result["report_id"] + ".pdf?lang=en")
    text = "".join(p.get_text() for p in pymupdf.open(stream=pdf.content, filetype="pdf")).replace("\n", " ")
    assert "2 scanned page(s) were read by OCR" in text


def test_uzbek_latin_scan_picks_the_uzbek_model(fast):
    info = {}
    text = extract.extract_text("scan.pdf", scanned(UZ), ocr=True, info=info)
    import corpus
    assert info["ocr_models"] == "uzb+eng"
    assert [t for t, _, _ in corpus.lexical(text, "uz")] == [t for t, _, _ in corpus.lexical(UZ, "uz")]


def test_short_title_page_does_not_decide_the_models(fast):
    info = {}
    extract.extract_text("scan.pdf", scanned("Gold Prices and Inflation", EN, EN), ocr=True, info=info)
    assert info["ocr_pages"] == 3 and info["ocr_models"] == "eng"


def test_text_pdf_is_not_ocred(fast):
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(60, 60, 535, 790), EN, fontfile=FONT, fontname="serif", fontsize=12)
    info = {}
    assert "Fergana valley" in extract.extract_text("t.pdf", doc.tobytes(), ocr=True, info=info) and info == {}


def test_page_limit_and_off_switch(fast, monkeypatch):
    monkeypatch.setenv("OCR_MAX_PAGES", "1")
    with pytest.raises(extract.ExtractError) as exc:
        extract.extract_text("scan.pdf", scanned(EN, EN), ocr=True)
    assert exc.value.code == "ocr_too_many_pages"
    monkeypatch.setenv("OCR_ENABLED", "0")
    client = TestClient(app)
    response = client.post("/api/check", data={"language": "en", "mode": "reference", "reference_text": EN},
                           files={"submission": ("scan.pdf", io.BytesIO(scanned(EN)), "application/pdf")})
    assert response.status_code == 400 and response.json()["code"] == "no_text"
    assert client.get("/health").json()["ocr"] is False


def test_busy_when_another_scan_is_being_read(fast, monkeypatch):
    monkeypatch.setenv("OCR_QUEUE_SECONDS", "0")
    assert extract._OCR_SLOT.acquire(timeout=5)
    try:
        with pytest.raises(extract.ExtractError) as exc:
            extract.extract_text("scan.pdf", scanned(EN), ocr=True)
        assert exc.value.code == "ocr_busy" and exc.value.status == 503
    finally:
        extract._OCR_SLOT.release()
