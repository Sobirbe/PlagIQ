"""Citation-aware classification of verified matches (deterministic, tested rules).

Nothing is excluded by default. When a user chooses an exclusion, the report
shows the percentage before and after, and which words were set aside. These
rules locate quotation marks, nearby reference markers and a bibliography
section; they do not judge whether a citation is correct or adequate.
"""
import re

from corpus import lexical, merge

# Typographic quotation pairs used in English, Russian and Uzbek texts.
PAIRS = {"«": "»", "“": "”", "„": "“", "‚": "‘"}
MAX_QUOTE = 3000          # characters; longer "quotes" are almost always unmatched marks
CITE_WINDOW = 80          # characters after a closing quote searched for a reference marker
CITATION = re.compile(
    r"\[\s*\d{1,4}(?:\s*[,;–-]\s*\d{1,4})*(?:\s*,\s*(?:p|pp|b|bet|с|стр|б|бет)\.?\s*\d+(?:\s*[–-]\s*\d+)?)?\s*\]"
    r"|\([^()\n]{0,120}?\b(?:1[5-9]|20)\d{2}[a-zа-я]?\b[^()\n]{0,60}\)"
    r"|\[[^\[\]\n]{0,120}?\b(?:1[5-9]|20)\d{2}[a-zа-я]?\b[^\[\]\n]{0,60}\]"
    r"|^[\s.,;:]*[¹²³⁴⁵⁶⁷⁸⁹⁰]+", re.IGNORECASE)

_A = r"['ʻʼ‘’`]"
HEADINGS = re.compile(
    r"^[ \t]*(?:\d{1,2}[.)]?[ \t]+|[IVX]{1,4}[.)][ \t]+)?(?:"
    r"references|reference list|bibliography|works cited|literature cited|literature|sources"
    r"|список (?:использованной |использованных |цитируемой )?(?:литературы|источников)|использованная литература"
    r"|библиографический список|библиография|литература"
    rf"|(?:foydalanilgan )?adabiyotlar(?: ro{_A}?yxati)?|(?:foydalanilgan )?manbalar(?: ro{_A}?yxati)?"
    r"|(?:фойдаланилган )?адабиётлар(?: рўйхати)?|(?:фойдаланилган )?манбалар(?: рўйхати)?"
    r")[ \t]*[:.]?[ \t]*$", re.IGNORECASE | re.MULTILINE)
END_HEADINGS = re.compile(
    r"^[ \t]*(?:appendix|appendices|приложени[ея]|ilova(?:lar)?|илова(?:лар)?)\b[^\n]{0,80}$",
    re.IGNORECASE | re.MULTILINE)
MIN_WORD_OPTIONS = (7, 10, 15, 20, 30)


def quotation_spans(text):
    """[start, end) spans between paired quotation marks, including the marks."""
    spans, stack = [], []
    straight = None
    for i, ch in enumerate(text):
        if ch == "\n" and text[i - 1:i] == "\n":          # paragraph break resets straight quotes
            straight = None
        if stack and ch == PAIRS[text[stack[-1]]]:
            start = stack.pop()
            if not stack and i + 1 - start <= MAX_QUOTE:
                spans.append([start, i + 1])
            continue
        if ch in PAIRS:
            stack.append(i)
            if len(stack) > 8:
                stack = stack[-8:]
            continue
        if ch == '"' and not stack:
            if straight is None:
                straight = i
            else:
                if i + 1 - straight <= MAX_QUOTE:
                    spans.append([straight, i + 1])
                straight = None
    return merge(spans)


def is_cited(text, end):
    window = text[end:end + CITE_WINDOW]
    stop = re.search(r'[«“„"]', window)
    return bool(CITATION.search(window[:stop.start()] if stop else window))


def bibliography_span(text):
    """(start, end, heading) of the last references section after 30% of the text, else None."""
    found = [m for m in HEADINGS.finditer(text) if m.start() >= 0.3 * len(text)]
    if not found:
        return None
    start = found[-1].start()
    after = END_HEADINGS.search(text, found[-1].end())
    return (start, after.start() if after else len(text), found[-1].group().strip())


def _location(ws, we, quotes, bib):
    if bib and bib[0] <= ws < bib[1]:
        return "bibliography"
    for qs, qe, cited in quotes:
        if qs <= ws and we <= qe:
            return "quotation_cited" if cited else "quotation_uncited"
    return "other"


def assess(text, language, intervals, exclude_quotes=False, exclude_bibliography=False, min_words=7, q=None,
           template_spans=None, template_name=None):
    """Classify matched words and apply only the exclusions the user selected.

    `intervals` are validated character spans of matched text. Each matched word
    gets one location (quotation with/without a nearby reference, bibliography,
    other) and at most one exclusion reason, checked in the order institution template text
    (`template_spans`: wording the student's university requires), bibliography, quotation, short match.
    The denominator (all manuscript words) never changes.
    """
    q = q if q is not None else lexical(text, language)
    quotes = [(s, e, is_cited(text, e)) for s, e in quotation_spans(text)]
    bib = bibliography_span(text)
    locations = dict.fromkeys(("quotation_cited", "quotation_uncited", "bibliography", "other"), 0)
    excluded = dict.fromkeys(("template", "bibliography", "quotations", "short"), 0)
    template = merge([list(x) for x in template_spans or []])
    in_template = lambda ws, we: any(s <= ws and we <= e for s, e in template)
    segments, k = [], 0
    for start, end in merge(intervals):
        while k < len(q) and q[k][1] < start:
            k += 1
        run = []
        while k < len(q) and q[k][2] <= end:
            run.append(q[k])
            k += 1
        for _, ws, we in run:
            where = _location(ws, we, quotes, bib)
            locations[where] += 1
            reason = ("template" if template and in_template(ws, we) else
                      "bibliography" if exclude_bibliography and where == "bibliography" else
                      "quotations" if exclude_quotes and where.startswith("quotation") else
                      "short" if len(run) < min_words else None)
            if reason:
                excluded[reason] += 1
            label = (where, reason)
            if segments and segments[-1][2] == label and not any(c.isalnum() for c in text[segments[-1][1]:ws]):
                segments[-1][1] = we
            else:
                segments.append([ws, we, label])
    if not template:
        excluded.pop("template")
    matched = sum(locations.values())
    counted = matched - sum(excluded.values())
    total = len(q)
    pct = lambda n: round(100 * n / total, 2) if total else 0
    return {
        "total_words": total, "matched_words_all": matched, "matched_words_counted": counted,
        "percent_all": pct(matched), "percent_counted": pct(counted),
        "locations": locations, "excluded": excluded,
        "exclusions": {"quotations": bool(exclude_quotes), "bibliography": bool(exclude_bibliography),
                       "min_words": int(min_words), "template": template_name if template else None},
        "quotations_found": len(quotes), "quotations_cited": sum(c for _, _, c in quotes),
        "bibliography": ({"start": bib[0], "end": bib[1], "heading": bib[2][:80]} if bib else None),
        "segments": [(s, e, where, reason) for s, e, (where, reason) in segments],
    }
