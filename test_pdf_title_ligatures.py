"""A working paper checked at 0.44% instead of ~100%: the series label was searched as its title, and
PDF ligatures (ﬁ, ﬂ) broke matching. Both fixed; stored texts are upgraded in the background."""
import corpus
import discovery
import extract


def test_series_labels_are_not_taken_as_the_title():
    text = ("Working Paper/Document de travail\n2007-35\nGold Prices and Inflation\nby Greg Tkacz\nwww.bankofcanada.ca\n\n"
            "Bank of Canada Working Paper 2007-35\nJune 2007\n" + "Body text about gold prices and expected inflation. " * 40)
    assert discovery._first_line_query(text) == "Gold Prices and Inflation"
    for line in ("Bank of Canada Working Paper 2007-35", "ISSN 1701-9397", "© 2007 Bank of Canada", "by Greg Tkacz",
                 "NBER Working Paper Series No. 12876", "Ilmiy-amaliy jurnal, 2024-yil 3-son", "Научный журнал «Экономика»"):
        assert not discovery._looks_like_title(line), line
    assert discovery._looks_like_title("Gold Prices and Inflation")


def test_ligatures_match_plain_letters():
    plain = corpus.lexical("prices and inflation are significant for financial markets", "en")
    ligature = corpus.lexical("prices and inﬂation are signiﬁcant for ﬁnancial markets", "en")
    assert [t for t, _, _ in plain] == [t for t, _, _ in ligature]
    assert ligature[2][1:] == (11, 19)                                   # offsets still point at the original text
    assert extract.expand_ligatures("Inﬂation ﬁnds ﬀ") == "Inflation finds ff"
    result = corpus.compare_texts("Gold prices and inflation are significant for financial market stress today.",
                                  ["Gold prices and inﬂation are signiﬁcant for ﬁnancial market stress today."], "en")
    assert result[1][0]["matched_words"] >= 10


def test_stored_texts_with_ligatures_are_upgraded_in_batches(tmp_path):
    db = corpus.connect(str(tmp_path / "c.db"))
    meta = lambda n: {"title": f"Paper {n}", "authors": "A", "year": 2020, "language": "en", "url": f"https://e.org/{n}",
                      "rights_basis": "open_license", "permission_reference": "CC BY"}
    bodies = ["Gold prices and inﬂation are signiﬁcant for ﬁnancial market stress in every period studied here.",
              "Central banks reﬂect on oﬃcial reserves when the ﬁscal outlook weakens across several emerging economies.",
              "Household surveys conﬁrm that ﬂexible wages oﬀset the eﬀect of rising fuel costs on rural families."]
    for n, body in enumerate(bodies):
        corpus.ingest(db, meta(n), body)
    corpus.ingest(db, meta(9), "A text without any ligature characters at all, written plainly for this test.")
    # Simulate an index built before ligatures were expanded: fingerprints of the raw ligature words.
    import textfold
    saved = dict(textfold.LIGATURES)
    textfold.LIGATURES.clear()
    try:
        db.execute("DELETE FROM windows")
        for num, raw, text in db.execute("SELECT num, metadata, text FROM sources").fetchall():
            corpus._store_windows(db, num, text, "en")
        db.commit()
    finally:
        textfold.LIGATURES.update(saved)
    queries = ["Gold prices and inflation are significant for financial market stress in every period",
               "Central banks reflect on official reserves when the fiscal outlook weakens",
               "Household surveys confirm that flexible wages offset the effect of rising fuel costs"]
    assert not any(corpus.search(db, q, "en")["sources"] for q in queries)   # old index: no match
    assert corpus.ligature_upgrade(db, batch=2) == 1                     # 3 texts with ligatures, 2 done
    assert corpus.ligature_upgrade(db, batch=2) == 0 and corpus.ligature_upgrade(db) == 0
    assert all(len(corpus.search(db, q, "en")["sources"]) == 1 for q in queries)   # all found after the upgrade
