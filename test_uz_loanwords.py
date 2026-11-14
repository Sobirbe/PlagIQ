"""Russian loanwords that Uzbek Latin and Cyrillic spell differently match across scripts and spellings,
without changing how native Uzbek words fold."""
import json

import corpus
import textfold

PAIRS = [("sentabr", "сентябрь"), ("sentyabr", "сентябрь"), ("oktabr", "октябрь"), ("noyabr", "ноябрь"),
         ("rejissor", "режиссёр"), ("aktyor", "актёр"), ("kompyuter", "компьютер"), ("obyekt", "объект"),
         ("ob'yekt", "объект"), ("subyekt", "субъект"), ("podyezd", "подъезд"), ("sivilizatsiya", "цивилизация"),
         ("tsivilizatsiya", "цивилизация"), ("litsey", "лицей"), ("sirk", "цирк"), ("relef", "рельеф"),
         ("byuro", "бюро"), ("shchyot", "щёт"), ("fevral", "февраль"), ("iyun", "июнь")]
NATIVE = [("ta'lim", "таълим"), ("ma'no", "маъно"), ("san'at", "санъат"), ("tayyor", "тайёр"), ("dunyo", "дунё"),
          ("yo'l", "йўл"), ("O‘zbekiston", "Ўзбекистон"), ("yer", "ер")]


def words(text):
    return [w for w, _, _ in corpus.lexical(text, "uz")]


def test_loanword_spellings_fold_together():
    for latin, cyrillic in PAIRS:
        assert words(latin) == words(cyrillic), (latin, cyrillic)


def test_native_words_keep_their_distinctions():
    for latin, cyrillic in NATIVE:
        assert words(latin) == words(cyrillic), latin
    assert words("ta'lim") != words("talim") and words("yer") != words("er") and words("tayyor") != words("tayor")
    assert words("san'at") != words("sanat") and words("ma'no") != words("mano")


def test_offsets_still_point_into_the_original_text():
    text = "Konferensiya 15 sentyabrda ob'yektda boshlandi."
    folded, starts, ends = textfold.fold_with_map(text)
    assert len(folded) == len(starts) == len(ends)
    tokens = corpus.lexical(text, "uz")
    assert [text[s:e] for _, s, e in tokens] == ["Konferensiya", "15", "sentyabrda", "ob'yektda", "boshlandi"]


def test_passage_with_other_spelling_is_found_in_the_library(tmp_path):
    db = corpus.connect(str(tmp_path / "c.db"))
    source = ("Konferensiya 2024-yil 15-sentabrda boshlandi va unda rejissor hamda aktyorlar ishtirok etdi. Ushbu obyekt "
              "sivilizatsiya tarixini oʻrganish uchun muhim ahamiyatga ega boʻlib, litsey oʻquvchilari uchun ham ochiq.")
    corpus.ingest(db, {"title": "S", "authors": "A", "year": 2024, "language": "uz", "url": "https://e.uz/1",
                       "rights_basis": "open_license", "permission_reference": "CC BY"}, source)
    student = ("Конференция 2024 йил 15 сентябрда бошланди ва унда режиссёр ҳамда актёрлар иштирок этди. Ушбу объект "
               "цивилизация тарихини ўрганиш учун муҳим аҳамиятга эга бўлиб, лицей ўқувчилари учун ҳам очиқ.")
    found = corpus.search(db, student, "uz")["sources"]
    assert found and found[0]["matched_words"] == len(words(student))                # every word, across scripts


def test_existing_library_is_refingerprinted_in_batches(tmp_path):
    db = corpus.connect(str(tmp_path / "c.db"))
    meta = lambda n: {"title": f"T{n}", "authors": "A", "year": 2024, "language": "uz", "url": f"https://e.uz/{n}",
                      "rights_basis": "open_license", "permission_reference": "CC BY"}
    for n in range(3):
        corpus.ingest(db, meta(n), f"Matn {n}: 15-sentyabrda ochilgan rejissyorlar festivali va kino sanʼati haqida maqola raqami {n}.")
    db.execute("DELETE FROM corpus_meta WHERE key='uz_loanwords'") if db.execute(
        "SELECT name FROM sqlite_master WHERE name='corpus_meta'").fetchone() else None
    db.commit()
    assert corpus.uz_fold_upgrade(db, batch=2) == 1
    assert corpus.uz_fold_upgrade(db, batch=2) == 0 and corpus.uz_fold_upgrade(db) == 0
    assert db.execute("SELECT value FROM corpus_meta WHERE key='uz_loanwords'").fetchone()


def test_one_rare_shared_phrase_makes_a_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "SEED_SAMPLE", 1)                  # sample every phrase, to make the test exact
    db = corpus.connect(str(tmp_path / "c.db"))
    meta = lambda n: {"title": f"T{n}", "authors": "A", "year": 2024, "language": "en", "url": f"https://e.org/{n}",
                      "rights_basis": "open_license", "permission_reference": "CC BY"}
    corpus.ingest(db, meta(1), "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi.")
    corpus.ingest(db, meta(2), "Rho sigma tau upsilon phi chi psi omega and other letters with more ordinary words here.")
    query = corpus.lexical("One two three gamma delta epsilon four five six seven eight nine ten", "en")
    nums = dict(db.execute("SELECT json_extract(metadata, '$.title'), num FROM sources").fetchall())
    assert corpus._seed_candidates(db, query) == [nums["T1"]]      # one phrase, held by one text only
