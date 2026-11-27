"""A journal's own pages (cover, editorial board, contents, imprint, running headers) are not a match, and are not
used as search phrases. Found on a real reprint: 21.8% "match" with two other articles of the same issue, all of it
the journal's pages, while the article itself was never searched for (2 of 3 search phrases were the journal name
and an editorial board member's institute)."""
import random

import corpus
from test_archive import engine  # noqa: F401  (fixture)
from test_auth_ai import login  # noqa: F401  (fixture)
import discovery
from test_real_eval import WORDS


def prose(rng, sentences=30):
    return " ".join(" ".join(rng.choice(WORDS) for _ in range(12)).capitalize() + "." for _ in range(sentences))


rng = random.Random(11)
MASTHEAD = ("Bosh muharrir Oblomurodov Narzullo iqtisodiyot fanlari doktori professor. Tahrir hay'ati a'zolari "
            "ro'yxati va jurnal haqida ma'lumot. " + prose(rng, 12))
IMPRINT = "Mas'ul muharrir Beglayev. Materiallar ko'chirib bosilganda jurnalga havola qilinishi shart. " + prose(rng, 4)
JOURNAL = "https://doi.org/10.68372/"


def reprint(body):
    # a word of its own at the end of each body: with this small test vocabulary two bodies could otherwise end on
    # the same word, and the seven words across that edge into the shared imprint would really be shared
    return MASTHEAD + " " + body + f" Yakun{rng.randrange(10**9)}. " + IMPRINT


def test_two_articles_of_one_journal_share_only_its_pages():
    mine, a, b = prose(rng, 60), prose(rng, 60), prose(rng, 60)
    doc, sources = reprint(mine), [reprint(a), reprint(b)]
    keys = [corpus.journal_key(JOURNAL + "x1"), corpus.journal_key(JOURNAL + "x2")]
    assert keys == ["10.68372", "10.68372"]
    _, before, _, _ = corpus.compare_texts(doc, sources, "uz")                 # as before: the pages matched
    assert all(before)
    _, after, _, _ = corpus.compare_texts(doc, sources, "uz", journals=keys)
    assert after == [None, None]                                                # now: nothing but the journal


def test_real_copying_from_an_article_of_that_journal_is_still_found():
    mine, a, b = prose(rng, 60), prose(rng, 60), prose(rng, 60)
    copied = " ".join(a.split()[300:420])                                       # 120 words from article A's body
    intro = " ".join(b.split()[:80])                                            # and B's introduction
    doc = reprint(mine + " " + copied + " " + intro)
    keys = ["10.68372", "10.68372"]
    q, (ea, eb), covered, _ = corpus.compare_texts(doc, [reprint(a), reprint(b)], "uz", journals=keys)
    assert ea and ea["matched_words"] >= 110 and eb and eb["matched_words"] >= 70
    text = " ".join(w for w, _, _ in q)
    assert "bosh muharrir" not in " ".join(q[i][0] for i in sorted(covered))   # the masthead is not counted


def test_repositories_are_not_a_journal():
    assert corpus.journal_key("https://doi.org/10.5281/zenodo.8239732") is None
    assert corpus.journal_key("https://cyberleninka.ru/article/n/x") is None
    assert corpus.journal_key("https://oila-ilmiy.uz/index.php/sgf/article/view/105") == "oila-ilmiy.uz"
    shared = prose(rng, 8)
    a, b = shared + " " + prose(rng, 60), shared + " " + prose(rng, 60)
    _, found, _, _ = corpus.compare_texts(shared + " " + prose(rng, 40), [a, b], "uz", journals=[None, None])
    assert all(found)                                                           # two Zenodo works: still matches


def test_library_articles_of_one_journal_do_not_match_its_pages(tmp_path, monkeypatch):
    """The oila-ilmiy.uz case: ten short articles carry the journal's front pages; a check of another article of
    the journal matched all ten at the same 1.03%."""
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    db = corpus.connect()
    for i in range(10):
        corpus.ingest(db, {"title": f"Article {i}", "authors": "A", "year": 2024, "language": "uz",
                           "url": f"https://oila-ilmiy.uz/index.php/sgf/article/view/{100 + i}",
                           "rights_basis": "open_license", "permission_reference": "CC BY"}, reprint(prose(rng, 25)))
    copied = prose(rng, 20)
    corpus.ingest(db, {"title": "Source of the copy", "authors": "B", "year": 2023, "language": "uz",
                       "url": "https://other-journal.uz/view/1", "rights_basis": "open_license",
                       "permission_reference": "CC BY"}, prose(rng, 20) + " " + copied + " " + prose(rng, 20))
    report = corpus.search(db, reprint(prose(rng, 40) + " " + copied), "uz")
    assert [s["title"] for s in report["sources"]] == ["Source of the copy"]


def test_search_phrases_come_from_the_article_pages():
    header = "JURNAL NOMI ilmiy-amaliy elektron jurnali\n2026-yil, noyabr.\n№11-son.\nISSN: 3060-5245\n{n}\n"
    pages = [(1, "MUQOVA"), (2, MASTHEAD), (3, "MUNDARIJA " + prose(rng, 6))]
    body_pages = [(143 + k, ("HOUSEHOLD FINANCIAL VULNERABILITY IN UZBEKISTAN\n" if k == 0 else "") + prose(rng, 25))
                  for k in range(6)]
    text = "\n".join(header.format(n=n) + content for n, content in pages + body_pages + [(240, IMPRINT)])
    body = discovery.article_body(text)
    assert body.lstrip().startswith("HOUSEHOLD FINANCIAL VULNERABILITY") and "Bosh muharrir" not in body
    assert "JURNAL NOMI" not in body and "ISSN" not in body
    assert discovery.suggest_queries(body)[0].startswith("HOUSEHOLD FINANCIAL")
    plain = prose(rng, 80)
    assert discovery.article_body(plain) == plain                               # no running header: unchanged


def reprint_pdf(article_pages):
    import pymupdf
    header = "JURNAL NOMI ilmiy-amaliy elektron jurnali\n2026-yil, noyabr.\n№11-son.\nISSN: 3060-5245\n{n}\n"
    pages = [(1, "MUQOVA"), (2, MASTHEAD), (3, "MUNDARIJA " + prose(rng, 6))] + \
            [(143 + k, body) for k, body in enumerate(article_pages)] + [(240, IMPRINT)]
    doc = pymupdf.open()
    for n, content in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), header.format(n=n) + content, fontsize=8)
    return doc.tobytes()


def test_a_reprint_whose_article_is_in_the_library_is_found_in_full(engine, login, monkeypatch, tmp_path):  # noqa: F811
    """The owner's case end to end: the article's published version is in the library; the check of the reprint
    PDF finds the article in full, leaves the journal's pages out and says so (result and PDF)."""
    import pymupdf
    import app as web
    from test_auth_ai import pilot_tester
    article_pages = [prose(rng, 22) for _ in range(5)]
    published = " ".join(article_pages)
    with corpus.connect() as db:
        corpus.ingest(db, {"title": "The article (published)", "authors": "S.", "year": 2026, "language": "uz",
                           "url": "https://doi.org/10.68372/abc", "rights_basis": "open_license",
                           "permission_reference": "CC BY"}, published)
    pilot_tester(login)
    data = login.post("/api/check", data={"mode": "corpus", "language": "uz", "consent": "true"},
                      files={"submission": ("reprint.pdf", reprint_pdf(article_pages), "application/pdf")}).json()
    assert data["percent"] >= 95, data.get("percent")
    assert data["journal_pages"]["words"] > 100
    pdf = login.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=en")
    text = " ".join("".join(p.get_text() for p in pymupdf.open(stream=pdf.content)).split())
    assert "Journal pages Left out: this is a journal reprint, only the article was checked" in text
    page = web.app and login.get("/").text
    assert "journalPages" in page


def test_a_thesis_with_a_running_header_is_checked_whole():
    header = "Dissertatsiya ishi Toshkent davlat universiteti\n{n}\n"
    pages = [(None, "Titul varag‘i")] + [(k, prose(rng, 20)) for k in range(1, 9)]
    text = "\n".join(header.format(n=n if n else "") + c for n, c in pages)
    assert discovery.article_body(text) == text                                # no ISSN, no page jump: unchanged
