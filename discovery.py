"""Bounded academic metadata/abstract discovery through OpenAlex.

Never fetches arbitrary URLs or publisher PDFs. OpenAlex is queried with a
short excerpt of the manuscript; the service only compares returned abstracts.
"""
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor

API = "https://api.openalex.org/works"
MAX_RESPONSE = 5 * 1024 * 1024

def reconstruct_abstract(inverted):
    """Reassemble an OpenAlex inverted abstract index into readable text."""
    if not isinstance(inverted, dict):
        return ""
    positions = {}
    for term, indexes in inverted.items():
        if not isinstance(term, str) or not isinstance(indexes, list):
            continue
        for p in indexes:
            if isinstance(p, int) and 0 <= p < 5000:
                positions[p] = term
    return " ".join(positions[x] for x in sorted(positions))

MAX_QUERIES = 3
QUERY_WORDS = 14
SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
# Search the manuscript's language; Russian and Uzbek together because OpenAlex
# often labels Uzbek Cyrillic works as Russian.
LANGUAGE_FILTER = {"uz": "language:uz|ru", "ru": "language:ru|uz", "en": "language:en"}


NOT_TITLE = re.compile(
    r"^(?:abstract|annotation|annotatsiya|аннотация|аннотация\.|keywords?|kalit so|ключевые слова|introduction|kirish|введение"
    r"|udc|udk|удк|ббк|doi|vol\.|volume|no\.|№|issn|received|accepted|e-?mail)\b", re.I)


# Series labels, publisher and copyright lines printed above or below a title page's real title
# ("Working Paper/Document de travail", "Bank of Canada Working Paper 2007-35", "ISSN 1701-9397").
SERIES_LINE = re.compile(
    r"working paper|document de travail|discussion paper|staff report|technical report|occasional paper|"
    r"policy (?:research )?paper|research paper series|working papers? series|preprint|препринт|issn|isbn|"
    r"www\.|https?://|©|copyright|all rights reserved|^by\b|^\s*(?:muallif|автор)\b|ilmiy[- ]?(?:amaliy )?jurnal|"
    r"научный журнал|научно[- ]практический журнал|conference proceedings|материалы конференции", re.I)


def _looks_like_title(line):
    words = line.split()
    letters = sum(c.isalpha() for c in line)
    return (3 <= len(words) <= 30 and 15 <= len(line) <= 250 and letters >= 0.7 * len(line.replace(" ", ""))
            and not NOT_TITLE.match(line) and not SERIES_LINE.search(line) and "@" not in line)


def _first_line_query(text):
    """The title is usually among the first lines; many real titles have only 3-5 words."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in re.split(r"[\n\t]", text)]   # table cells separately
    lines = [line for line in lines if line]
    titles = [line for line in lines[:15] if _looks_like_title(line)]
    if not titles:
        titles = [line for line in lines if 35 <= len(line) <= 220 and len(line.split()) >= 6]
    if not titles:
        titles = [re.sub(r"\s+", " ", text).strip()]
    return " ".join(titles[0].split()[:QUERY_WORDS])[:220]


def _distinctive(sentence):
    words = re.findall(r"[^\W\d_]{6,}", sentence.lower())
    return len(set(words))


def suggest_queries(text, max_queries=MAX_QUERIES):
    """Up to max_queries excerpts of at most 14 words: the title-like first line,
    then the most distinctive sentence from each of the beginning, middle and end
    of the body (bibliography excluded). Never the whole manuscript."""
    from citations import bibliography_span
    bib = bibliography_span(text)
    body = text[:bib[0]] if bib else text
    queries = [_first_line_query(body)]
    # Whole sentences are assigned to a third by where they start, so an excerpt
    # never begins or ends in the middle of a word.
    sentences = [(m.start(), re.sub(r"\s+", " ", m.group()).strip()) for m in SENTENCE.finditer(body)]
    third = max(1, len(body) // 3)
    for part in range(3):
        candidates = [c for start, c in sentences if start // third == part or (part == 2 and start // third > 2)]
        candidates = [c for c in candidates if 8 <= len(c.split()) <= 80 and _distinctive(c) >= 4]
        if candidates:
            best = max(candidates, key=_distinctive)
            queries.append(" ".join(best.split()[:QUERY_WORDS])[:220])
    unique = []
    for query in queries:
        if query.strip() and query.lower() not in (u.lower() for u in unique):
            unique.append(query)
    return unique[:max_queries]


def spread_excerpts(text, n):
    """Up to n excerpts of at most 14 words for web search: the title-like first line,
    then the most distinctive sentence of each of n-1 equal parts of the body
    (bibliography excluded), so copying anywhere in the document can be found."""
    from citations import bibliography_span
    bib = bibliography_span(text)
    body = text[:bib[0]] if bib else text
    excerpts = [_first_line_query(body)]
    sentences = [(m.start(), re.sub(r"\s+", " ", m.group()).strip()) for m in SENTENCE.finditer(body)]
    sentences = [(start, c) for start, c in sentences if 8 <= len(c.split()) <= 80 and _distinctive(c) >= 4]
    parts = max(1, n - 1)
    size = max(1, len(body) // parts + 1)
    for part in range(parts):
        candidates = [c for start, c in sentences if start // size == part]
        if candidates:
            excerpts.append(" ".join(max(candidates, key=_distinctive).split()[:QUERY_WORDS])[:220])
    unique = []
    for excerpt in excerpts:
        if excerpt.strip() and excerpt.lower() not in (u.lower() for u in unique):
            unique.append(excerpt)
    return unique[:n]


# Very common words that make an OpenAlex phrase search needlessly strict.
STOPWORDS = frozenset("""
va bilan uchun hamda ham bu ushbu esa yoki ekan bo'lib bo'yicha qilish kerak orqali sifatida bo'lgan
bir har shu ular uning ularning qilingan etilgan
и в во на для с со по от до из к ко о об что как это а но или также при не
the of and in to is for with that are on as by this from an be at or which its their
""".split())
MIN_RESULTS = 3


def keywords(query, n):
    """The first n meaningful words of an excerpt (4+ letters, not a common function word)."""
    words = re.findall(r"[^\W\d_]+(?:['’ʻʼ‘][^\W\d_]+)*", query)
    norm = lambda w: re.sub(r"['’ʻʼ‘]", "'", w.lower())
    return " ".join([w for w in words if len(w) >= 4 and norm(w) not in STOPWORDS][:n])


def _search_relaxed(query, language, max_results):
    """OpenAlex requires every search word to match, so a long sentence often
    finds nothing. If fewer than MIN_RESULTS come back, retry with its 6, then
    4, most meaningful words. Returns (results, phrase actually used)."""
    results, used = _search(query, language, max_results), query
    for n in (6, 4):
        if len(results) >= MIN_RESULTS:
            break
        shorter = keywords(query, n)
        if len(shorter.split()) < 2 or shorter.lower() == used.lower():
            continue
        retry = _search(shorter, language, max_results)
        if len(retry) > len(results):
            results, used = retry, shorter
    return results, used


def _search(query, language, max_results):
    params = {"search": query, "per_page": str(min(max_results, 10)),
              "select": "id,display_name,doi,publication_year,abstract_inverted_index,primary_location,"
                        "best_oa_location,locations,open_access,has_content"}
    if language in LANGUAGE_FILTER:
        params["filter"] = LANGUAGE_FILTER[language]
    key = os.environ.get("OPENALEX_API_KEY")
    # Sent as a header, never in the URL, so the key cannot appear in access logs.
    auth = {"Authorization": "Bearer " + key} if key else {}
    request = Request(API + "?" + urlencode(params),
                      headers={**auth, "User-Agent": "PlagIQ-research-pilot/0.5 (+academic metadata search)",
                               "Accept": "application/json"})
    with urlopen(request, timeout=9) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError("response too large")
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("invalid OpenAlex response")
    return payload["results"]


# ------------------------------------------------------------- topic relevance

RELEVANCE_STEM = 6            # first letters of a word, so inflected forms (o'qituvchi/o'qituvchilar) match
MIN_RELEVANCE = 0.3           # calibrated on 224 OpenAlex results in 8 topics (EN/RU/UZ), see evaluation/DISCOVERY.md
GENERIC = frozenset("""
analysis research results study studies method methods approach paper article based using different
important problem problems system systems process development model models effect effects factors
however therefore within between particular general significant various number level levels
tadqiqot natija natijalar tahlil usul usullar muammo jarayon rivojlanish tizim omil omillar asosiy
исследование результаты анализ метод методы подход статья работа развитие система процесс модель
""".split())


def _stems(text, language):
    import corpus
    words = [token for token, _, _ in corpus.lexical(text, language or "en")]
    out = set()
    for word in words:
        word = re.sub(r"['’ʻʼ‘`]", "", word)
        if len(word) >= 5 and word not in STOPWORDS and word not in GENERIC and not word.isdigit():
            out.add(word[:RELEVANCE_STEM])
    return out


def document_stems(text, language):
    return _stems(text, language)


def relevance(source, doc_stems, language):
    """Share of a result's distinctive title+abstract words that also occur in the document
    (0..1), or None when the record has too few words to judge (kept, never hidden)."""
    stems = _stems((source.get("title") or "") + " " + (source.get("abstract") or "")[:4000], language)
    if len(stems) < 4:
        return None
    score = len(stems & doc_stems) / len(stems)
    title = _stems(source.get("title") or "", language)
    if len(title) >= 2 and len(title & doc_stems) / len(title) >= 0.5:
        score = max(score, len(title & doc_stems) / len(title))     # e.g. a garbled abstract under a fitting title
    return round(score, 3)


def off_topic(source):
    score = source.get("relevance")
    return score is not None and score < MIN_RELEVANCE


# Chapter and section headings: "1. Introduction", "2.3 Results", "CHAPTER 2", "2-BOB", "I BOB", "ГЛАВА 1",
# "Kirish", "Xulosa", "Введение", "Заключение"... Used to give every chapter of a long work its own search.
HEADING = re.compile(
    r"^[ \t]*(?:(?:chapter|глава|раздел|part|section)\s+[\dIVXLC]+\b[^\n]{0,120}"
    r"|(?:[\dIVX]{1,4}[-–.]?\s*(?:bob|bo['‘’ʻ]lim|боб|бўлим))\b[^\n]{0,120}"
    r"|\d{1,2}(?:\.\d{1,2}){0,2}\.?[ \t]+[^\W\d_][^\n]{2,110}"
    r"|[IVX]{1,5}\.[ \t]+[^\W\d_][^\n]{2,110}"
    r"|(?:introduction|kirish|введение|conclusions?|xulosa|заключение|literature review|methodology|methods|results|"
    r"discussion|natijalar|muhokama|результаты|обсуждение|методы)[ \t]*[.:]?)[ \t]*$", re.I | re.M)
MIN_CHAPTER_WORDS = 150


def chapters(body):
    """(start, end) of the chapters/sections of a text, from its headings. A heading followed by fewer than
    MIN_CHAPTER_WORDS words (a table of contents, a sub-heading) is merged into the next part."""
    starts = sorted({m.start() for m in HEADING.finditer(body)} | {0})
    parts = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        if parts and len(body[parts[-1][0]:start].split()) < MIN_CHAPTER_WORDS:
            parts[-1] = (parts[-1][0], end)                 # previous part too short: extend it
        else:
            parts.append((start, end))
    if len(parts) > 1 and len(body[parts[-1][0]:parts[-1][1]].split()) < MIN_CHAPTER_WORDS:
        last = parts.pop()
        parts[-1] = (parts[-1][0], last[1])
    return parts


def _best_excerpt(body, start, end, taken):
    candidates = [re.sub(r"\s+", " ", m.group()).strip() for m in SENTENCE.finditer(body, start, end)]
    candidates = [c for c in candidates if 8 <= len(c.split()) <= 80 and _distinctive(c) >= 4]
    for sentence in sorted(candidates, key=_distinctive, reverse=True):
        excerpt = " ".join(sentence.split()[:QUERY_WORDS])[:220]
        words = set(excerpt.lower().split())
        if all(len(words & t) / max(1, len(words | t)) < 0.6 for t in taken):     # not a near-duplicate
            return excerpt
    return None


def chapter_queries(text, n):
    """Up to n excerpts: the title, then one from each chapter (headings found in the text). Long chapters
    get extra excerpts when there are fewer chapters than searches; with more chapters than searches the
    longest ones are used. Falls back to equal parts when the text has no headings. Never the whole text."""
    from citations import bibliography_span
    bib = bibliography_span(text)
    body = text[:bib[0]] if bib else text
    parts = chapters(body)
    if len(parts) < 2:
        return spread_excerpts(text, n), 0
    found = len(parts)
    while len(parts) < n - 1:                                 # split the longest part in two
        i = max(range(len(parts)), key=lambda k: parts[k][1] - parts[k][0])
        a, b = parts[i]
        middle = body.find(". ", (a + b) // 2)
        if middle == -1 or middle >= b - 1 or b - a < 4000:
            break
        parts[i:i + 1] = [(a, middle + 1), (middle + 1, b)]
    if len(parts) > n - 1:
        keep = sorted(sorted(range(len(parts)), key=lambda k: parts[k][0] - parts[k][1])[:n - 1])
        parts = [parts[k] for k in keep]
    queries = [_first_line_query(body)]
    taken = [set(queries[0].lower().split())]
    for a, b in parts:
        excerpt = _best_excerpt(body, a, b, taken)
        if excerpt:
            queries.append(excerpt)
            taken.append(set(excerpt.lower().split()))
    return queries[:n], found


def query_count(text):
    """3 excerpts for up to 3,000 words, one more for each further 3,000, at most OPENALEX_MAX_QUERIES (default 8):
    with only 3 short excerpts, a long document is represented by a few generic sentences."""
    try:
        cap = max(MAX_QUERIES, min(int(os.environ.get("OPENALEX_MAX_QUERIES", "8")), 12))
    except ValueError:
        cap = 8
    words = len(text.split())
    return min(cap, MAX_QUERIES + max(0, words - 1) // 3000)


def discover(text, max_results=8, language=None):
    count = query_count(text)
    found_chapters = 0
    if count <= MAX_QUERIES:
        queries = suggest_queries(text)
    else:
        queries, found_chapters = chapter_queries(text, count)
    if not queries:
        return {"status": "no_query", "sources": [], "queries": 0, "message": "No search phrase could be extracted"}
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        outcomes = list(pool.map(lambda q: _safe(_search_relaxed, q, language, max_results), queries))
    used = [o[1] if not isinstance(o, Exception) else q for o, q in zip(outcomes, queries)]
    outcomes = [o[0] if not isinstance(o, Exception) else o for o in outcomes]
    failures = [o for o in outcomes if isinstance(o, Exception)]
    if len(failures) == len(outcomes):
        return {"status": "unavailable", "sources": [], "queries": len(queries), "query_texts": queries,
                "message": "OpenAlex unavailable or rate limited (" + type(failures[0]).__name__ + ")"}
    sources, seen, by_key, duplicates = [], set(), {}, 0
    for results in outcomes:
        if isinstance(results, Exception):
            continue
        for work in results[:max_results]:
            if not isinstance(work, dict) or work.get("id") in seen:
                continue
            seen.add(work.get("id"))
            # The same work often appears several times (a DOI and a repository copy, a later reprint).
            keys = [k for k in (_doi_key(work.get("doi")), _title_key(work.get("display_name"))) if k]
            twin = next((by_key[k] for k in keys if k in by_key), None)
            if twin is not None:
                duplicates += 1
                better = reconstruct_abstract(work.get("abstract_inverted_index")) and not twin.get("abstract")
                if not better:
                    continue
                sources.remove(twin)                        # keep the copy that has an abstract
                inherited = [k for k, v in by_key.items() if v is twin]
                for k in inherited:
                    del by_key[k]
                keys = list(dict.fromkeys(keys + inherited))      # later copies still match the twin's DOI
            title = str(work.get("display_name") or "Untitled")
            url = work.get("doi") or work.get("id") or ""
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
            year = work.get("publication_year")
            sources.append({"title": title[:300], "url": url[:500], "year": year if type(year) is int else None,
                            "abstract": abstract[:20000], "coverage": "abstract" if abstract else "metadata_only",
                            "access": open_access_links(work)})
            for k in keys:
                by_key[k] = sources[-1]
    status = "partial" if failures else "searched"
    return {"status": status, "sources": sources, "queries": len(queries), "query_texts": used,
            "chapters": found_chapters, "duplicates_merged": duplicates,
            "failed_queries": len(failures),
            "message": (f"{len(failures)} of {len(queries)} OpenAlex searches failed; " if failures else "")
                       + "OpenAlex metadata searched; only returned abstracts can be text-compared"}


def open_access_links(work):
    """Where a free full text may be found: PDF links, landing pages, OpenAlex's own copy."""
    best = work.get("best_oa_location") if isinstance(work.get("best_oa_location"), dict) else {}
    locations = [best] + [l for l in work.get("locations") or [] if isinstance(l, dict) and l is not best]
    pdfs, pages = [], []
    for location in locations:
        if not location.get("is_oa", location is best):
            continue
        for value, bucket in ((location.get("pdf_url"), pdfs), (location.get("landing_page_url"), pages)):
            if isinstance(value, str) and value.startswith("https://") and value not in bucket:
                bucket.append(value[:500])
    oa_url = (work.get("open_access") or {}).get("oa_url") if isinstance(work.get("open_access"), dict) else None
    if isinstance(oa_url, str) and oa_url.startswith("https://") and oa_url not in pdfs + pages:
        (pdfs if oa_url.lower().endswith(".pdf") else pages).append(oa_url[:500])
    return {"openalex_id": str(work.get("id") or "").rsplit("/", 1)[-1], "pdf_urls": pdfs[:4], "pages": pages[:3],
            "openalex_pdf": bool((work.get("has_content") or {}).get("pdf")) if isinstance(work.get("has_content"), dict) else False}


def _doi_key(doi):
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", str(doi or "").strip().lower())
    return "doi:" + doi if doi.startswith("10.") else None


def _title_key(title):
    words = re.findall(r"[^\W_]+", str(title or "").lower())
    return "title:" + " ".join(words) if len(words) >= 4 else None


def _safe(fn, *args):
    try:
        return fn(*args)
    except (TimeoutError, HTTPError, URLError, OSError, ValueError) as exc:
        return exc
