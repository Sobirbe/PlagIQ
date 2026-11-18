"""Possible paraphrased reuse (crosslang.search with same_language=True, PARAPHRASE=1): same-language texts only,
sentences already found by word matching and template lines left out, engine and website plumbing, cache privacy and
the PDF. The stand-in encoder gives a paraphrase the same concept vector as its original, as LaBSE does
(measured in evaluation/PARAPHRASE.md)."""
import pymupdf
from fastapi.testclient import TestClient

import app as web
import corpus
import crosslang
import engine_service
from test_archive import engine  # noqa: F401  (fixture)
from test_crosslang import CONCEPTS, StandIn, body, meta

PARAPHRASES = [   # Uzbek rewordings of CONCEPTS[0..2] with almost no words in common with the originals
    "Qishloqdagi xonadonlar mablag‘ini chetdan keladigan pullar ancha o‘zgartirib, xarajatlarni ko‘paytirdi.",
    "Dehqon oilalaridan to‘rt yuztasi so‘ralganda, yuborilgan mablag‘ bolalarning darsga qatnashishini kuchaytirgani aniqlandi.",
    "Fermerlikka kiritilgan sarmoyaga bo‘lgan ta’sir sezilmas darajada bo‘lib, asosan katta xo‘jaliklarda kuzatildi.",
]


class ParaphraseStandIn(StandIn):
    def __init__(self):
        super().__init__()
        for k, p in enumerate(PARAPHRASES):
            self.concept[p.rstrip(".")] = self.concept[CONCEPTS[k][0].rstrip(".")]


def filler(n):
    return " ".join(f"Bu hujjatning o‘z fikri bo‘lgan alohida gap raqami {i} va u boshqa matnlarda uchramaydi." for i in range(n))


def library_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    db = corpus.connect()
    corpus.ingest(db, meta(1, "uz", title="Uzbek original"), body("uz", (0, 1, 2)))
    corpus.ingest(db, meta(2, "ru", title="Russian text"), body("ru", (0, 1, 2)))
    enc = ParaphraseStandIn()
    while crosslang.index_some(db, enc):
        pass
    return db, enc


def test_same_language_mode_finds_paraphrase_and_only_in_that_language(tmp_path, monkeypatch):
    db, enc = library_db(tmp_path, monkeypatch)
    text = " ".join(PARAPHRASES[:2]) + " " + filler(4)
    found = crosslang.search(db, text, "uz", enc, same_language=True)
    assert [f["num"] for f in found] == [1] and found[0]["sentences"] == 2           # the Uzbek text, not the Russian one
    assert [f["num"] for f in crosslang.search(db, text, "uz", enc)] == [2]          # the translation mode is unchanged
    assert crosslang.search(db, PARAPHRASES[0] + " " + filler(4), "uz", enc, same_language=True) == []   # one sentence


def test_sentences_found_by_word_matching_and_template_lines_are_left_out(tmp_path, monkeypatch):
    db, enc = library_db(tmp_path, monkeypatch)
    text = " ".join(PARAPHRASES[:2]) + " " + filler(4)
    first = text.index(PARAPHRASES[0])
    skip = [(first, first + len(PARAPHRASES[0]))]                                  # already an exact match
    assert crosslang.search(db, text, "uz", enc, same_language=True, skip=skip) == []
    keywords = "Kalit so‘zlar: " + PARAPHRASES[1]                                   # a keyword line is template text
    assert crosslang.search(db, PARAPHRASES[0] + " " + keywords + " " + filler(4), "uz", enc, same_language=True) == []
    assert crosslang.template_like("1. Karimov A. Iqtisodiyot asoslari. – T.: Fan, 2015. – B. 34.")
    assert crosslang.template_like("Таянч сўз ва иборалар: таржима, фразеология, эквивалентлик.")
    assert not crosslang.template_like(PARAPHRASES[2])


def test_engine_and_website_show_it_separately_only_when_switched_on(engine, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("CROSSLANG", "1")
    monkeypatch.setattr(crosslang, "_model", ParaphraseStandIn())
    with corpus.connect() as db:
        corpus.ingest(db, meta(1, "uz", title="Uzbek original"), body("uz", (0, 1, 2)))
        while crosslang.index_some(db, crosslang._model):
            pass
    web.RATE.clear(); web.results.clear()
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.db"))
    text = " ".join(PARAPHRASES) + " " + filler(5)
    client = TestClient(web.app)
    check = lambda: client.post("/api/check", data={"mode": "corpus", "language": "uz", "consent": "true"},
                                files={"submission": ("t.txt", text.encode())}).json()
    monkeypatch.delenv("PARAPHRASE", raising=False)
    assert "paraphrased" not in check()                                            # off by default
    monkeypatch.setenv("PARAPHRASE", "1")
    data = check()
    item = data["paraphrased"][0]
    assert item["title"] == "Uzbek original" and item["sentences"] == 3
    assert item["pairs"][0]["query_excerpt"] == PARAPHRASES[0] and item["pairs"][0]["source_excerpt"]
    assert data["matched_tokens"] == 0                                             # never part of the percentage
    assert "query_excerpt" not in repr(web.results[data["report_id"]]["result"]["paraphrased"])
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=en")
    pdf_text = " ".join("".join(p.get_text() for p in pymupdf.open(stream=pdf.content)).split())
    assert "Possible paraphrased reuse (review)" in pdf_text and "NOT included in the percentage" in pdf_text
    with corpus.connect() as db:
        monkeypatch.setenv("CROSSLANG", "0")
        assert not engine_service.paraphrase_enabled()


def test_admin_shows_the_engines_translated_reuse_status(engine, monkeypatch, tmp_path):  # noqa: F811
    """The website rebuilt the engine's /v1/library answer from an allowlist that left `crosslang` out, so /admin
    said "off (optional)" even when the engine had CROSSLANG=1."""
    from test_auth_ai import TOKEN, sign_in
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.sqlite3"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    monkeypatch.setenv("CROSSLANG", "1")
    monkeypatch.setitem(engine_service.CROSSLANG_STATE, "state", "indexing")
    with corpus.connect() as db:
        corpus.ingest(db, meta(1, "uz", title="Uzbek original"), body("uz", (0, 1, 2)))
    client = TestClient(web.app)
    sign_in(client, user_id=42)
    page = client.get("/admin").text
    row = page[page.index("Translated reuse"):].split("</tr>")[0]
    assert "off (optional)" not in row and "indexing: 0 of 1 library texts indexed" in row, row
    monkeypatch.setitem(engine_service.CROSSLANG_STATE, "state", "error")
    monkeypatch.setitem(engine_service.CROSSLANG_STATE, "error", "MemoryError: model too large")
    assert "error: MemoryError: model too large" in client.get("/admin").text


def test_pdf_limitations_say_what_screening_ran():
    """The limitations line said "No translation or paraphrase screening was performed" and "3 short excerpts" in
    every report; it now follows the check: the web excerpts actually searched and the screening the engine ran."""
    import report_pdf
    base = {"mode": "academic", "language": "uz", "percent": 0.0, "total_tokens": 900, "status": "completed_no_matches",
            "sources": [], "checked_at": "2026-11-18T06:00:00Z", "web": {"enabled": True, "excerpts": 5, "providers": ["Brave"]}}
    def limits(result):
        pdf = report_pdf.build_pdf(result, "a" * 12, "en")
        return " ".join("".join(p.get_text() for p in pymupdf.open(stream=pdf)).split())
    assert "top pages found for 5 short excerpts. No translation or paraphrase screening" in limits(base)
    assert "Translation screening compared the document with the PlagIQ library only; no paraphrase" in \
        limits({**base, "translated": []})
    assert "Translation and paraphrase screening compared the document with the PlagIQ library only." in \
        limits({**base, "translated": [], "paraphrased": []})
    assert "No web-wide search was performed. No translation" in limits({**base, "web": {"enabled": False}})
