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
    cached = repr(web.results[data["report_id"]])
    assert "query_excerpt" not in cached and "My introduction" not in cached                   # manuscript never cached


def test_highlight_without_sources_still_works():
    html = web.highlight("one two three", [(0, 3, "other", None)])
    assert html == '<mark class="m">one</mark> two three'
