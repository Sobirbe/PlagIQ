"""The sample report on the home page ("See a sample report").

Everything that is a measurement is computed by PlagIQ's real code: the matches, the percentage, the edited-copy
passage, the quotation analysis, the highlighting and the status come from running the matcher on the sample
document and the sample sources below. What is invented is labelled as such: the document, the three sources
(fictional titles, no links) and the library figures. The result carries "sample": true and no report id, so it
cannot be downloaded, verified or stored.
"""
from functools import lru_cache

DOCUMENT = (
    "Labour migration and household budgets in the Fergana valley\n\n"
    "This essay looks at how families in three villages near Namangan spend money sent home by relatives working "
    "abroad. I interviewed twelve households in the spring and compared their answers with published studies.\n\n"
    "Remittance inflows have reshaped rural household budgets across the Fergana valley, and families now allocate "
    "these funds between consumption, education and small business investment in roughly equal parts.\n\n"
    "Several families told me that money from abroad is first used to repay debts taken before the trip. "
    "Survey evidence from four hundred families suggests that transfers raise school enrolment and lower seasonal "
    "borrowing, while the effect on farm investment stays small and concentrated among larger farms.\n\n"
    "As one study puts it, “remittances are a safety net before they are an investment” (Karimova, 2021, p. 14). "
    "My interviews support this: only two of the twelve households had invested in equipment.\n\n"
    "In conclusion, the evidence from my small sample agrees with the larger studies, but the role of debt repayment "
    "deserves more attention in future research.\n\n"
    "References\n"
    "Karimova, D. (2021). Remittances and rural households. Sample Journal of Economics, 4(2), 10–25."
)

SOURCES = [
    {"title": "Sample source A — Remittances and rural households (fictional journal article)", "url": "", "year": 2021,
     "coverage": "abstract", "fulltext": (
         "Introduction. Remittance inflows have reshaped rural household budgets across the Fergana valley, and "
         "families now allocate these funds between consumption, education and small business investment in roughly "
         "equal parts. Remittances are a safety net before they are an investment. The data come from household "
         "surveys carried out between 2017 and 2020 in Namangan, Andijan and Fergana regions.")},
    {"title": "Sample source B — Migration and schooling in Central Asia (fictional abstract)", "url": "", "year": 2023,
     "coverage": "abstract", "abstract": (
         "Survey data from four hundred households show that remittances raise school enrolment and reduce seasonal "
         "borrowing, while the effect on agricultural investment is small and concentrated among larger farms with "
         "irrigation access.")},
    {"title": "Sample source C — Irrigation reform in Karakalpakstan (fictional abstract)", "url": "", "year": 2019,
     "coverage": "abstract", "abstract": (
         "This paper studies water user associations and canal maintenance in Karakalpakstan and finds that local "
         "management improved delivery reliability for cotton and wheat farms.")},
]

LIBRARY = {"searched": True, "language": "en", "size": 3400, "language_size": 1250,
           "collections": {"academic": 1100, "wikipedia": 120, "deposits": 30}, "matched_sources": 0}


@lru_cache(maxsize=1)
def build():
    import app
    import checkstatus
    import citations
    import corpus
    import integrity
    text = DOCUMENT
    sources = [dict(s) for s in SOURCES]
    result = app.compare_sources(text, sources, "en")
    intervals = result.pop("intervals")
    exact = result.pop("exact_intervals", intervals)
    lex = corpus.lexical(text, "en")
    review = citations.assess(text, "en", intervals)
    marks = app.marks_for(text, review.pop("segments"), result["sources"])
    for source in result["sources"]:
        for passage in source.get("passages") or []:
            qs, qe = passage["query_start"], passage["query_end"]
            passage["query_excerpt"] = text[qs:qe]
    result.update({
        "sample": True, "mode": "academic", "language": "en", "document": "sample-essay.txt",
        "highlight_html": app.highlight(text, None, marks=marks), "citation": review,
        "percent_all": review["percent_all"], "percent": review["percent_counted"],
        "matched_tokens_counted": review["matched_words_counted"],
        "edited_tokens": max(0, corpus.words_in_intervals(lex, intervals) - corpus.words_in_intervals(lex, exact)),
        "database_status": "searched", "database_message": "Sample data", "queries": 3, "failed_queries": 0,
        "source_count": len(sources), "evaluated_sources": len(sources), "abstract_count": 2,
        "fulltext": {"attempted": 1, "compared": 1, "failed": 0, "timed_out": 0, "reasons": {}},
        "web": {"enabled": False}, "library": dict(LIBRARY), "off_topic_hidden": 0,
        "matched_source_count": sum(1 for s in result["sources"] if s["matched_tokens"]),
        "self_comparison": False, "reference": "None",
        "scope": "SAMPLE — fictional document and sources, compared by PlagIQ's real matcher.",
        "integrity": integrity.assess(text, False)[0],
        "plan": None, "date": "2026-01-01T00:00:00+00:00",
    })
    for key in ("integrity_examples",):
        result.pop(key, None)
    return checkstatus.assess(result)
