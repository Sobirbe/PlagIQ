"""More OpenAlex excerpts for long documents; topic relevance decides which full texts are downloaded
and which zero-match results are listed (never the percentage); the library is stated in the PDF."""
import re

import fitz
from fastapi.testclient import TestClient

import app as web
import discovery
import fulltext
from test_followup import EN, pdf_text

DOC = ("Pricing to market and exchange rate pass-through. Exporters adjust destination prices when the exchange "
       "rate moves, so import prices react only partly. We estimate pass-through elasticities for manufacturing "
       "exporters using firm-level price data, menu costs and invoicing currency. Markups absorb currency shocks "
       "and pricing-to-market behaviour differs across destination markets. ") * 40


def test_query_count_grows_with_length(monkeypatch):
    monkeypatch.delenv("OPENALEX_MAX_QUERIES", raising=False)
    words = lambda n: " ".join(["word"] * n)
    assert [discovery.query_count(words(n)) for n in (500, 3000, 3001, 8361, 100_000)] == [3, 3, 4, 5, 8]
    monkeypatch.setenv("OPENALEX_MAX_QUERIES", "5")
    assert discovery.query_count(words(100_000)) == 5


def test_long_document_sends_more_spread_excerpts(monkeypatch):
    sent = []
    monkeypatch.setattr(discovery, "_search", lambda q, lang, n: sent.append(q) or [])
    text = "\n".join(f"Section {i}. " + f"Distinctive sentence number {i} about exporters adjusting destination prices carefully. " * 60
                     for i in range(12))
    assert len(text.split()) > 6000
    result = discovery.discover(text, 8, "en")
    assert result["queries"] == discovery.query_count(text) >= 4
    assert len({q for q in sent}) >= 4


def test_relevance_on_and_off_topic():
    stems = discovery.document_stems(DOC, "en")
    on = {"title": "Exchange rate pass-through and pricing to market",
          "abstract": "We study how exporters adjust destination prices and markups after currency shocks."}
    off = {"title": "N = 2 supergravity in five dimensions revisited",
           "abstract": "We construct the off-shell Weyl multiplet of conformal supergravity and its superconformal tensor couplings."}
    garbled = {"title": "Exchange rate pass-through", "abstract": "0( 'RPLQJXH] DQG /LQGD / 7HVDU 1%(5 :RUNLQJ 3DSHU"}
    assert discovery.relevance(on, stems, "en") >= 0.6
    assert discovery.relevance(off, stems, "en") < discovery.MIN_RELEVANCE
    assert discovery.relevance(garbled, stems, "en") >= discovery.MIN_RELEVANCE      # a fitting title is enough
    assert discovery.relevance({"title": "Short", "abstract": ""}, stems, "en") is None
    assert not discovery.off_topic({"relevance": None})


def test_uzbek_inflections_and_scripts_count():
    doc = "O‘qituvchilarning kasbiy kompetensiyasi va ta’lim sifatini oshirish masalalari. " * 30
    src = {"title": "Оқитувчи компетенциялари", "abstract": "Ўқитувчининг касбий компетенцияси ва таълим сифати масаласи"}
    assert discovery.relevance(src, discovery.document_stems(doc, "uz"), "uz") >= 0.5


def records():
    on = {"title": "Exchange rate pass-through and pricing to market", "url": "https://doi.org/10.1/on", "year": 2020,
          "abstract": DOC[:600], "coverage": "abstract",
          "access": {"pdf_urls": ["https://example.org/on.pdf"], "pages": [], "openalex_pdf": False}}
    off = {"title": "Weyl multiplets of conformal supergravity in five dimensions", "url": "https://doi.org/10.1/off", "year": 2001,
           "abstract": "Superconformal tensor multiplets, gauge couplings and supersymmetric vector fields in five dimensions.",
           "coverage": "abstract", "access": {"pdf_urls": ["https://example.org/off.pdf"], "pages": [], "openalex_pdf": False}}
    copied = {"title": "Quantum chromodynamics lattice simulations", "url": "https://doi.org/10.1/copied", "year": 2019,
              "abstract": "Lattice gauge simulations of quark confinement. " + DOC[:400], "coverage": "abstract",
              "access": {"pdf_urls": [], "pages": [], "openalex_pdf": False}}
    return [off, on, copied]


def test_academic_check_ranks_downloads_and_hides_only_unmatched_off_topic(monkeypatch):
    monkeypatch.setattr(web, "discover", lambda text, *a: dict(sources=records(), status="searched", message="", queries=5))
    tried = []

    def fake_fetch(sources, max_works, seconds, paid):
        tried.extend(s["title"] for s in sources if (s.get("access") or {}).get("pdf_urls"))
        return {"attempted": len(tried), "compared": 0, "failed": 0, "timed_out": 0}
    monkeypatch.setattr(fulltext, "fetch_fulltexts", fake_fetch)
    monkeypatch.setattr(fulltext, "settings", lambda: (True, 8, 25, False))
    client = TestClient(web.app)
    r = client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true"},
                    files={"submission": ("a.txt", DOC.encode())})
    data = r.json()
    assert r.status_code == 200, data
    assert tried == ["Exchange rate pass-through and pricing to market"]          # the off-topic paper is never downloaded
    titles = [s["title"] for s in data["sources"]]
    assert "Weyl multiplets of conformal supergravity in five dimensions" not in titles   # off-topic, no match: not listed
    assert "Quantum chromodynamics lattice simulations" in titles                  # off-topic title but it matches: listed
    assert data["off_topic_hidden"] == 1 and data["source_count"] == 3 and data["abstract_count"] == 3
    assert data["percent"] > 0
    text, _ = pdf_text(client, data["report_id"], "en")
    text = " ".join(text.split())
    assert "Off-topic results 1 compared, no matching words, not listed" in text
    assert "Full texts: 0 · abstracts: 3" in text                                   # certificate still counts all compared
    import verify
    cached = {"mode": "academic", "fulltext": {"compared": 0}, "abstract_count": data["abstract_count"], "sources": data["sources"]}
    assert verify.facts(cached, "x" * 12)["n"] == "3"                               # the QR code also counts the hidden one


def test_pdf_and_certificate_state_the_library(monkeypatch):
    monkeypatch.setattr(web, "discover", lambda text, *a: dict(sources=[], status="searched", message="", queries=3))

    async def library(result, original, language, owner):
        return {"searched": True, "size": 2287, "matched_sources": 0}
    monkeypatch.setattr(web, "search_library", library)
    client = TestClient(web.app)
    data = client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true"},
                       files={"submission": ("a.txt", EN.encode())}).json()
    for lang, row, cert in (("en", "PlagIQ library 2 287 indexed sources searched", "and the PlagIQ library"),
                            ("uz", "PlagIQ kutubxonasi 2 287 ta indekslangan manba qidirildi", "hamda PlagIQ kutubxonasi"),
                            ("ru", "Библиотека PlagIQ проверено индексированных источников: 2 287", "библиотека PlagIQ")):
        text = " ".join(pdf_text(client, data["report_id"], lang)[0].split())
        assert row in text and cert in text, (lang, text[:3000])
