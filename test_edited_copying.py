"""Edited copying: copied text with words replaced, inserted or deleted is found, verified by word alignment,
reported as its own match type, and kept out of boilerplate and unrelated texts on the same topic."""
import io

import pymupdf
from fastapi.testclient import TestClient

import app as web
import corpus
import engine_client

SOURCE = ("Remittance inflows have reshaped rural household budgets across the Fergana valley, and this study examines "
          "how families allocate these funds between consumption, education and small business investment. Survey data "
          "from four hundred households show that remittances raise school enrolment and reduce seasonal borrowing, while "
          "the effect on agricultural investment is small and concentrated among larger farms with irrigation access. "
          "The results suggest that financial literacy programmes and local credit cooperatives could channel more of these "
          "transfers into productive uses, especially for households headed by women.")
OWN = ("My essay begins with a short description of the labour market in Namangan and the reasons why young people "
       "leave for seasonal work abroad. ")
LICENCE = ("This article is distributed under the terms of the Creative Commons Attribution License which permits "
           "unrestricted use and distribution provided the original work is properly cited.")


def edit(text, step, how):
    words = text.split()
    if how == "replace":
        return " ".join("banana" if i % step == step - 1 else w for i, w in enumerate(words))
    if how == "insert":
        return " ".join(w + (" indeed" if i % step == step - 1 else "") for i, w in enumerate(words))
    return " ".join(w for i, w in enumerate(words) if i % step != step - 1)


def test_edited_copies_are_found_and_labelled():
    for how in ("replace", "insert", "delete"):
        copied = edit(SOURCE, 4 if how == "replace" else 5, how)
        q, (evidence,), covered, _ = corpus.compare_texts(OWN + copied, [SOURCE], "en")
        own_words = len(corpus.lexical(OWN, "en"))
        assert evidence and evidence["edited_words"] > 0.8 * (len(q) - own_words), how
        assert not any(k < own_words - 1 for k in covered), how                  # the student's own text is not marked
        edited = [p for p in evidence["passages"] if p["type"] == "edited"]
        assert edited and 0.6 <= edited[0]["similarity"] <= 1, how
        assert evidence["exact_intervals"] == [] or how != "replace"


def test_exact_copies_stay_exact():
    q, (evidence,), _, _ = corpus.compare_texts(OWN + SOURCE, [SOURCE], "en")
    assert evidence["edited_words"] == 0 and {p["type"] for p in evidence["passages"]} == {"exact"}


def test_one_word_in_three_is_beyond_reach():
    q, (evidence,), covered, _ = corpus.compare_texts(OWN + edit(SOURCE, 3, "replace"), [SOURCE], "en")
    assert not covered                      # documented limit: no three-word phrase survives


def test_same_topic_different_text_is_not_flagged():
    other = ("Labour migration from rural Uzbekistan has grown since 2017, and remittances now form a large share of "
             "household income in the Fergana valley. This paper estimates how migrant households spend these transfers, "
             "comparing spending on food, housing, schooling and farm equipment with non-migrant households, using a panel "
             "of two thousand families. Households receiving remittances spend more on education and housing, but invest "
             "little in agriculture unless they own irrigated land. Programmes that improve access to credit may help.")
    q, (evidence,), covered, _ = corpus.compare_texts(other, [SOURCE], "en")
    assert not covered


def test_edited_boilerplate_is_not_counted():
    import random
    vocabulary = "wheat yield soil salinity irrigation district water canal cotton harvest farm drainage".split()
    rngs = [random.Random(k) for k in range(3)]
    works = [" ".join(rng.choice(vocabulary) for _ in range(400)) + " " + LICENCE for rng in rngs]
    q, _, covered, _ = corpus.compare_texts(OWN + edit(LICENCE + " " + LICENCE, 6, "replace"), works, "en")
    assert not covered


def test_library_finds_edited_copies_after_the_index_upgrade(tmp_path):
    db = corpus.connect(str(tmp_path / "c.db"))
    corpus.ingest(db, {"title": "Remittances", "authors": "A", "year": 2024, "language": "en", "url": "https://e.org/1",
                       "rights_basis": "open_license", "permission_reference": "CC BY"}, SOURCE)
    student = OWN + edit(SOURCE, 4, "replace")
    found = corpus.search(db, student, "en")
    assert found["sources"][0]["edited_words"] > 50 and found["exact_passages"] == []
    assert found["sources"][0]["passages"][0]["type"] == "edited"
    db.execute("DELETE FROM seeds")                                  # a library indexed before seeds existed
    db.execute("DELETE FROM corpus_meta WHERE key='seeds_indexed'") if db.execute(
        "SELECT name FROM sqlite_master WHERE name='corpus_meta'").fetchone() else None
    db.commit()
    assert corpus.search(db, student, "en")["sources"] == []
    assert corpus.seeds_upgrade(db, batch=10) == 0
    assert corpus.search(db, student, "en")["sources"][0]["edited_words"] > 50
    assert corpus.remove(db, corpus.search(db, student, "en")["sources"][0]["sha256"])
    assert db.execute("SELECT COUNT(*) FROM seeds").fetchone()[0] == 0


def test_engine_summary_keeps_match_types():
    report = {"query_text_length": 100, "unique_passages": [[0, 60]], "exact_passages": [[0, 20]],
              "language_counts": {"uz": 0, "ru": 0, "en": 1}, "corpus_version": "0123456789ab",
              "sources": [{"source_filename": "S", "source_url": "https://e.org", "matched_chars": 60, "matched_words": 10,
                           "edited_words": 7, "passage_count": 2,
                           "passages": [{"query_start": 0, "query_end": 20, "source_start": 0, "source_end": 20, "type": "exact"},
                                        {"query_start": 20, "query_end": 60, "source_start": 30, "source_end": 70,
                                         "type": "edited", "similarity": 0.74}]}]}
    summary = engine_client.summarize(report)
    assert summary["exact_intervals"] == [[0, 20]] and summary["sources"][0]["edited_tokens"] == 7
    assert [p["type"] for p in summary["sources"][0]["passages"]] == ["exact", "edited"]
    assert summary["sources"][0]["passages"][1]["similarity"] == 0.74


def test_check_reports_edited_words_and_pdf_labels_them():
    web.RATE.clear(); web.results.clear()
    client = TestClient(web.app)
    data = client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": SOURCE},
                       files={"submission": ("m.txt", (OWN + edit(SOURCE, 4, "replace")).encode())}).json()
    assert data["status"] == "completed_matches" and data["edited_tokens"] > 50
    assert data["sources"][0]["passages"][0]["type"] == "edited"
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=en")
    text = " ".join(p.get_text() for p in pymupdf.open(stream=pdf.content, filetype="pdf")).replace("\n", " ")
    assert "Of which edited copying" in text and "edited copy," in text
