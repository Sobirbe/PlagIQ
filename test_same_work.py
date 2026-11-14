"""Copies of one work (journal, preprint, repository copy, mirror) count once when deciding boilerplate.
Before this, a passage copied from a paper that appeared in 3 compared texts was dropped and scored 0%."""
import random

import app
import corpus

PAPER = ("Remittance inflows have reshaped rural household budgets across the Fergana valley, and this study examines "
         "how families allocate these funds between consumption, education and small business investment. Survey data "
         "from four hundred households show that remittances raise school enrolment and reduce seasonal borrowing, while "
         "the effect on agricultural investment is small and concentrated among larger farms with irrigation access. "
         "The results suggest that financial literacy programmes and local credit cooperatives could channel more of these "
         "transfers into productive uses, especially for households headed by women.")
LICENCE = ("This article is distributed under the terms of the Creative Commons Attribution License which permits "
           "unrestricted use and distribution provided the original work is properly cited.")


def unrelated(seed, n=400):
    rng = random.Random(seed)
    words = "economy policy bank credit growth market household region trade labour price index output reform".split()
    return " ".join(rng.choice(words) for _ in range(n))


def test_copies_of_one_work_do_not_hide_copying():
    for copies in (1, 3, 5):
        q, _, covered, _ = corpus.compare_texts(PAPER, [PAPER] * copies, "en")
        assert len(covered) == len(q), copies
    sources = [{"title": "Journal", "fulltext": PAPER + " Conclusion."},
               {"title": "Repository", "fulltext": "Preprint. " + PAPER},
               {"title": "Mirror", "fulltext": PAPER},
               {"title": "Abstract", "abstract": PAPER}]
    assert app.compare_sources(PAPER, sources, "en")["percent"] == 100.0


def test_a_licence_line_in_different_works_is_still_boilerplate():
    papers = [unrelated(k) + " " + LICENCE for k in range(3)]
    q, _, covered, _ = corpus.compare_texts(unrelated(9, 200) + " Acknowledgements. " + LICENCE, papers, "en")
    assert not covered


def test_library_finds_a_work_imported_three_times(tmp_path):
    db = corpus.connect(str(tmp_path / "c.db"))
    meta = lambda n: {"title": f"Copy {n}", "authors": "A", "year": 2024, "language": "en", "url": f"https://e.org/{n}",
                      "rights_basis": "open_license", "permission_reference": "CC BY"}
    for n, text in enumerate([PAPER, "Preprint. " + PAPER, PAPER + "\nRepository copy."]):
        corpus.ingest(db, meta(n), text)
    for n in range(3):
        corpus.ingest(db, {**meta(10 + n), "title": f"Other {n}"}, unrelated(n) + " " + LICENCE)
    found = corpus.search(db, PAPER, "en")["sources"]
    assert {s["title"] for s in found} == {"Copy 0", "Copy 1", "Copy 2"}
    assert all(s["matched_words"] == len(corpus.lexical(PAPER, "en")) for s in found)
    assert not corpus.search(db, unrelated(20, 200) + " Acknowledgements. " + LICENCE, "en")["sources"]


def test_same_work_needs_real_overlap():
    a = frozenset(range(1000))
    assert corpus.same_work(a, frozenset(range(100, 600)))           # one contains half of the other
    assert not corpus.same_work(a, frozenset(range(980, 2000)))      # 20 shared of 1,000: a shared line
    assert not corpus.same_work(frozenset(range(10)), frozenset(range(10)))   # too short to tell
