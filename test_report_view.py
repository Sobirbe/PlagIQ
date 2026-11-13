"""Clearer report: matches coloured by source, side-by-side passages, nothing extra cached."""
import re

from fastapi.testclient import TestClient

import app as web

A = ("Remittance inflows reshape rural household budgets across the Fergana valley through seasonal consumption "
     "smoothing, investment in housing, and payment of school fees for younger children")
B = "Cotton cooperatives in Khorezm reorganised their irrigation schedules after the water reform of the last decade"
TEXT = f"My introduction sentence here. {A}. My own middle part follows. {B}. My conclusion."


def test_marks_carry_the_source_rank_and_excerpts_stay_private(monkeypatch):
    sources = [{"title": "Small source", "url": "https://doi.org/10.1/b", "year": 2020, "abstract": B + ".", "coverage": "abstract"},
               {"title": "Big source", "url": "https://doi.org/10.1/a", "year": 2021,
                "abstract": A + ". " + "Extra words about remittance studies.", "coverage": "abstract"}]
    monkeypatch.setattr(web, "discover", lambda text, *a: dict(sources=[dict(s) for s in sources], status="searched",
                                                               message="", queries=1, query_texts=["q"]))
    monkeypatch.setenv("FULLTEXT_FETCH", "0")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    web.RATE.clear(); web.results.clear()
    data = TestClient(web.app).post("/api/check", data={"mode": "academic", "language": "en", "consent": "true"},
                                    files={"submission": ("m.txt", TEXT.encode())}).json()
    ranked = sorted(data["sources"], key=lambda s: -s["matched_tokens"])
    assert ranked[0]["title"] == "Big source" and ranked[0]["spans"]
    marks = re.findall(r'<mark class="[^"]*" data-s="(\d)">([^<]*)</mark>', data["highlight_html"])
    assert {(s, text.split()[0]) for s, text in marks} == {("0", "Remittance"), ("1", "Cotton")}   # colour = list position
    passage = ranked[0]["passages"][0]
    assert passage["query_excerpt"].startswith("Remittance inflows") and passage["source_excerpt"].startswith("Remittance")
    cached = web.results[data["report_id"]]["result"]
    assert "query_excerpt" not in repr(cached)                        # side-by-side excerpts are never cached
    assert "_text" not in data and "_marks" not in data                # not sent to the browser a second time
    assert cached["_text"].startswith("My introduction")              # kept in memory with the report, for the PDF appendix
    assert [m[3] for m in cached["_marks"]] == [0, 1]


def test_highlight_without_sources_still_works():
    html = web.highlight("one two three", [(0, 3, "other", None)])
    assert html == '<mark class="m">one</mark> two three'


def _pdf_text(client, identifier, lang):
    import pymupdf
    pdf = client.get(f"/api/report/{identifier}.pdf?lang={lang}")
    assert pdf.status_code == 200
    with pymupdf.open(stream=pdf.content, filetype="pdf") as doc:
        return " ".join(" ".join(page.get_text() for page in doc).split())


def test_pdf_overview_and_highlighted_appendix(monkeypatch):
    sources = [{"title": "Big source", "url": "https://doi.org/10.1/a", "year": 2021, "abstract": A + ".", "coverage": "abstract"},
               {"title": "Small source", "url": "https://doi.org/10.1/b", "year": 2020, "abstract": B + ".", "coverage": "abstract"}]
    monkeypatch.setattr(web, "discover", lambda text, *a: dict(sources=[dict(s) for s in sources], status="searched",
                                                               message="", queries=1, query_texts=["q"]))
    monkeypatch.setenv("FULLTEXT_FETCH", "0")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    web.RATE.clear(); web.results.clear()
    client = TestClient(web.app)
    data = client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true"},
                       files={"submission": ("m.txt", TEXT.encode())}).json()
    text = _pdf_text(client, data["report_id"], "en")
    assert "Overview" in text and "Matched and counted" in text and "Sources by share of the document" in text
    share = round(100 * max(s["matched_tokens"] for s in data["sources"]) / data["total_tokens"], 2)
    assert f"{share:.2f}%" in text                                         # the biggest source's share of the document
    assert "Appendix: your document with matches highlighted" in text
    assert "My introduction sentence here." in text and "My own middle part follows." in text   # the whole document
    uz = _pdf_text(client, data["report_id"], "uz")
    assert "Ilova: mosliklar belgilangan hujjatingiz" in uz and "Umumiy ko‘rinish" in uz
    # Another browser session never gets the report, and deleting it removes the kept text.
    assert TestClient(web.app).get(f"/api/report/{data['report_id']}.pdf").status_code == 404
    assert client.delete(f"/api/report/{data['report_id']}").status_code == 200
    assert data["report_id"] not in web.results


def test_appendix_marks_have_colours_and_numbers():
    import report_pdf
    styles = report_pdf._styles()
    import html
    result = {"percent": 50.0, "mode": "reference", "_text": "Alpha beta gamma.\nDelta epsilon zeta eta.",
              "_marks": [[0, 10, "m", 0], [18, 31, "qc", None], [32, 40, "m x", 1]]}
    story = report_pdf.highlighted_section(result, "en", styles, lambda v: html.escape(str(v)))
    markup = " ".join(p.text for p in story if hasattr(p, "text"))
    assert report_pdf.PALETTE[0] in markup and report_pdf.QUOTE_BG in markup and "<u>" in markup
    assert markup.count(report_pdf.BADGE) == 2                           # each match from a source is numbered once
    assert report_pdf.highlighted_section({"percent": None, "_text": "x"}, "en", styles, html.escape) == []
