"""Possible translated reuse (crosslang.py): sentence splitting, the library index, the 2-sentence / 0.80 rule,
boilerplate, deposits, the engine and website plumbing, cache privacy and the PDF. A stand-in encoder gives
translations of the same sentence nearly the same vector (as LaBSE does); one test uses the real model when
CROSSLANG_TEST_MODEL points to its files."""
import hashlib
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app as web
import corpus
import crosslang
import engine_service
from test_archive import engine  # noqa: F401  (fixture)

# The same "meaning" in three languages; the stand-in encoder maps each to one concept vector.
CONCEPTS = [
    ("Pul o‘tkazmalari qishloq oilalarining byudjetini sezilarli darajada o‘zgartirdi va iste’molni oshirdi.",
     "Денежные переводы существенно изменили бюджеты сельских семей и увеличили потребление.",
     "Remittances significantly changed the budgets of rural families and increased consumption."),
    ("To‘rt yuz xonadon so‘rovi o‘tkazmalar maktabga borishni oshirganini ko‘rsatdi.",
     "Опрос четырёхсот домохозяйств показал, что переводы повышают посещаемость школ.",
     "A survey of four hundred households showed that transfers raise school attendance."),
    ("Qishloq xo‘jaligiga investitsiyalar ta’siri kichik bo‘lib, yirik fermer xo‘jaliklarida jamlangan.",
     "Влияние на сельскохозяйственные инвестиции невелико и сосредоточено в крупных хозяйствах.",
     "The effect on agricultural investment is small and concentrated among larger farms."),
    ("Mavsumiy qarz olish o‘tkazmalar oladigan oilalarda sezilarli darajada kamaydi.",
     "Сезонные заимствования заметно сократились в семьях, получающих переводы.",
     "Seasonal borrowing fell noticeably in families that receive remittances."),
]
BOILER = ("Ushbu maqola Creative Commons litsenziyasi asosida tarqatiladi va erkin foydalanish mumkin.",
          "Статья распространяется на условиях лицензии Creative Commons и доступна для свободного использования.")


class StandIn:
    """Vectors: a fixed random vector per concept (+ tiny noise per wording); unrelated sentences get their own."""
    def __init__(self):
        rng = np.random.default_rng(3)
        self.concept = {}
        for k, triple in enumerate(CONCEPTS + [BOILER]):
            base = rng.normal(size=crosslang.DIM)
            for s in triple:
                self.concept[s.rstrip(".")] = base

    def encode(self, sentences, batch=32):
        out = []
        for s in sentences:
            seed = int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)
            noise = np.random.default_rng(seed).normal(size=crosslang.DIM)
            base = self.concept.get(s.rstrip("."))
            v = base + 0.15 * noise if base is not None else noise
            out.append(v / np.linalg.norm(v))
        return np.array(out, dtype=np.float32)


def meta(n, language, **extra):
    return {"title": f"Text {n}", "authors": "A. Author", "year": 2021, "url": f"https://journal.example.org/{n}",
            "language": language, "rights_basis": "open_license", "permission_reference": "CC BY", **extra}


FILLER = {"uz": "Bu boshqa mavzudagi oddiy gap bo‘lib, tarjima bilan bog‘liq emas {i}.",
          "ru": "Это обычное предложение на другую тему, не связанное с переводом {i}.",
          "en": "This is an ordinary sentence on another subject, unrelated to translation {i}."}


def body(lang, concepts=(), filler=6):
    col = {"uz": 0, "ru": 1, "en": 2}[lang]
    parts = [CONCEPTS[c][col] for c in concepts] + [FILLER[lang].format(i=i) for i in range(filler)]
    return " ".join(parts)


@pytest.fixture()
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    db = corpus.connect()
    nums = {}
    for key, lang, concepts in (("ru_source", "ru", (0, 1, 2, 3)), ("en_other", "en", ()), ("uz_same", "uz", (0, 1, 2))):
        corpus.ingest(db, meta(len(nums), lang, title=key), body(lang, concepts))
        nums[key] = db.execute("SELECT MAX(num) FROM sources").fetchone()[0]
    enc = StandIn()
    while crosslang.index_some(db, enc):
        pass
    yield db, enc, nums
    db.close()


def test_sentences_split_and_trim():
    text = "Birinchi gap juda muhim va uzun bo‘lib, olti so‘zdan ko‘p. Qisqa gap.\nYangi xatboshi ham olti so‘zdan ortiq gapdan iborat."
    assert [text[s:e] for s, e in crosslang.sentences(text)] == [
        "Birinchi gap juda muhim va uzun bo‘lib, olti so‘zdan ko‘p.", "Yangi xatboshi ham olti so‘zdan ortiq gapdan iborat."]


def test_translation_found_only_in_other_languages_with_two_sentences(library):
    db, enc, nums = library
    assert crosslang.index_status(db)["indexed"] == 3
    found = crosslang.search(db, body("uz", (0, 1), filler=4), "uz", enc)
    assert [f["num"] for f in found] == [nums["ru_source"]]                       # not the Uzbek text (exact matcher's job)
    assert found[0]["sentences"] == 2 and all(p[4] >= 0.80 for p in found[0]["pairs"])
    assert crosslang.search(db, body("uz", (0,), filler=4), "uz", enc) == []      # one sentence is not enough
    assert crosslang.search(db, body("uz", (), filler=8), "uz", enc) == []        # unrelated text


def test_boilerplate_sentence_is_ignored(library, monkeypatch):
    db, enc, nums = library
    for n in range(5):                                                            # the licence line in 5 Russian texts
        corpus.ingest(db, meta(100 + n, "ru", title=f"licence {n}"), BOILER[1] + " " + body("ru", (), filler=3 + n))
    while crosslang.index_some(db, enc):
        pass
    found = crosslang.search(db, BOILER[0] + " " + body("uz", (3,), filler=4), "uz", enc)
    assert found == []                                                            # boilerplate + 1 real sentence


def test_removed_texts_leave_the_index(library):
    db, enc, nums = library
    db.execute("DELETE FROM sources WHERE num=?", (nums["ru_source"],)); db.commit()
    crosslang.index_some(db, enc)
    assert db.execute("SELECT COUNT(*) FROM crosslang_sents WHERE source=?", (nums["ru_source"],)).fetchone()[0] == 0


def test_engine_and_website_show_it_separately(engine, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("CROSSLANG", "1")
    monkeypatch.setattr(crosslang, "_model", StandIn())
    with corpus.connect() as db:
        corpus.ingest(db, meta(1, "ru", title="Russian source"), body("ru", (0, 1, 2)))
        corpus.ingest(db, meta(2, "ru", title="Deposit", rights_basis="author_deposit", owner="user:" + "a" * 24,
                               url=""), body("ru", (3,), filler=2) + " " + CONCEPTS[2][1])
        while crosslang.index_some(db, crosslang._model):
            pass
    web.RATE.clear(); web.results.clear()
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.db"))
    text = body("uz", (0, 1, 2, 3), filler=6)
    client = TestClient(web.app)
    data = client.post("/api/check", data={"mode": "corpus", "language": "uz", "consent": "true"},
                       files={"submission": ("t.txt", text.encode())}).json()
    items = {t["title"]: t for t in data["translated"]}
    assert set(items) == {"Russian source", "Deposit"}
    ru = items["Russian source"]
    assert ru["sentences"] == 3 and ru["pairs"][0]["query_excerpt"] == CONCEPTS[0][0] and ru["pairs"][0]["source_excerpt"]
    assert items["Deposit"]["archive"] and all(p["source_excerpt"] == "" for p in items["Deposit"]["pairs"])
    assert data["matched_tokens"] == 0 and data["percent"] in (None, 0)          # never part of the percentage
    cached = repr(web.results[data["report_id"]]["result"]["translated"])
    assert "query_excerpt" not in cached                                          # manuscript excerpts not cached
    web.results.clear()
    reopened = client.get(f"/api/report/{data['report_id']}").json()
    assert reopened["translated"][0]["pairs"][0]["query_excerpt"]
    import pymupdf
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=en")
    pdf_text = " ".join("".join(p.get_text() for p in pymupdf.open(stream=pdf.content)).split())
    assert "Possible translated reuse (review)" in pdf_text and "NOT included in the percentage" in pdf_text


def test_off_by_default_engine_adds_nothing(engine, monkeypatch):  # noqa: F811
    monkeypatch.delenv("CROSSLANG", raising=False)
    with corpus.connect() as db:
        assert engine_service.translated(db, body("uz", (0, 1)), "uz") is None


@pytest.mark.skipif(not os.environ.get("CROSSLANG_TEST_MODEL"), reason="real LaBSE files not available (set CROSSLANG_TEST_MODEL)")
def test_real_model_finds_translation_not_same_topic(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    enc = crosslang.Encoder(os.environ["CROSSLANG_TEST_MODEL"])
    db = corpus.connect()
    ru = " ".join(c[1] for c in CONCEPTS) + " " + " ".join(FILLER["ru"].format(i=i) for i in range(3))
    corpus.ingest(db, meta(1, "ru", title="ru"), ru)
    same_topic = ("Migratsiya va oilaviy daromadlar o‘rtasidagi bog‘liqlik ko‘plab tadqiqotlarda o‘rganilgan. Mualliflar "
                  "Farg‘ona vodiysidagi qishloqlarda dala ishlarini olib borgan. Natijalar mintaqaviy farqlarni ko‘rsatadi. "
                  "Kelgusida uzoq muddatli kuzatuvlar zarur bo‘ladi.")
    while crosslang.index_some(db, enc):
        pass
    found = crosslang.search(db, " ".join(c[0] for c in CONCEPTS[:3]), "uz", enc)
    assert found and found[0]["sentences"] >= 2
    assert crosslang.search(db, same_topic, "uz", enc) == []
    db.close()
