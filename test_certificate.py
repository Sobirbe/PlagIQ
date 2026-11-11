"""Certificate-of-check cover page: same facts as the QR verification, no manuscript text."""
import pymupdf
import pytest

import certificate
import report_pdf
import verify

RESULT = {"mode": "academic", "language": "en", "percent": 12.5, "total_tokens": 3894, "matched_tokens": 487,
          "source_count": 5, "evaluated_sources": 5, "database_status": "searched", "queries": 3, "reference": "None",
          "document": "thesis.docx", "date": "2026-11-11T07:15:13+00:00", "sha256": "7b23f18ebbe7c55c" + "0" * 48,
          "fulltext": {"attempted": 3, "compared": 2},
          "web": {"enabled": True, "providers": ["Brave"], "compared": 1},
          "sources": [{"title": "A", "coverage": "fulltext", "matched_tokens": 400}, {"title": "B", "coverage": "fulltext"},
                      {"title": "C", "coverage": "abstract"}, {"title": "D", "coverage": "abstract"},
                      {"title": "E", "coverage": "web", "matched_tokens": 87}],
          "integrity": {"status": "review", "signals": [{"id": "chatbot_phrases", "count": 1}], "references": None},
          "citation": {"percent_all": 14.0, "percent_counted": 12.5, "matched_words_all": 545, "matched_words_counted": 487,
                       "total_words": 3894, "exclusions": {"quotations": True, "bibliography": False, "min_words": 7},
                       "excluded": {"quotations": 58, "bibliography": 0, "short": 0}, "locations": {"other": 487},
                       "quotations_found": 2, "quotations_cited": 1, "bibliography": None},
          "scope": "x", "ai_detection": "Not performed"}
ID = "014996eea688" + "0" * 20


def first_page(result, lang="en", url=None):
    doc = pymupdf.open(stream=report_pdf.build_pdf(result, ID, lang, 14, url))
    return " ".join(doc[0].get_text().split()), doc


def test_certificate_facts_match_signed_verification(monkeypatch):
    monkeypatch.setenv("VERIFY_SECRET", "s" * 40)
    url = verify.link(RESULT, ID, "https://plagiq.example")
    text, doc = first_page(RESULT, url=url)
    for expected in ("CERTIFICATE OF CHECK", "0149-96EE-A688", "thesis.docx", "2026-11-11 07:15 UTC", "3 894", "12.5%",
                     "before exclusions: 14.0%", "Full texts: 2 · abstracts: 2 · web pages: 1", "quotations",
                     "Review suggested (1)", "OpenAlex and the web (Brave)", "Verify this certificate",
                     "not a certificate of originality"):
        assert expected in text, expected
    signed = verify.facts(RESULT, ID)
    assert signed["n"] == "5" and signed["p"] == "12.5" and signed["w"] == "3894"     # 2 + 2 + 1 sources, as printed
    assert len(doc[0].get_drawings()) > 100 and url[:40] in text.replace(" ", "")      # QR drawn, link printed
    assert "CERTIFICATE OF CHECK" not in " ".join(doc[1].get_text().split())          # evidence starts on page 2


def test_without_secret_says_verification_is_off():
    text, _ = first_page(dict(RESULT, percent=None, not_evaluated_reason="no_abstracts"), "ru")
    assert "СЕРТИФИКАТ ПРОВЕРКИ" in text and "Онлайн-проверка на этом сервере не настроена" in text and "Не оценено" in text


@pytest.mark.parametrize("mode, expected", [("reference", "1 reference document"), ("corpus", "5 indexed sources")])
def test_sources_line_per_mode(mode, expected):
    assert dict(certificate.facts(dict(RESULT, mode=mode), "en"))["Sources compared"] == expected


def test_long_file_name_and_uzbek_fit_on_one_page():
    text, doc = first_page(dict(RESULT, document="Magistrlik_dissertatsiyasi_" * 8 + ".docx"), "uz")
    assert "TEKSHIRUV SERTIFIKATI" in text and "Sertifikatni tekshirish" in text
    assert "Faqat tekshiruv fakti tasdiqlanadi" in text                               # bottom notice still on page 1
    assert "dissertats…di" in text and "_.docx" in text                               # shortened, extension kept
    blocks = doc[0].get_text("blocks")
    heading = next(b for b in blocks if "Sertifikatni tekshirish" in b[4])
    above = [b for b in blocks if b[1] < heading[1] and b is not heading]
    assert all(b[3] <= heading[1] for b in above), [b[4][:30] for b in above if b[3] > heading[1]]    # nothing overlaps
