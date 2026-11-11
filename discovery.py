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


def _looks_like_title(line):
    words = line.split()
    letters = sum(c.isalpha() for c in line)
    return (3 <= len(words) <= 30 and 15 <= len(line) <= 250 and letters >= 0.7 * len(line.replace(" ", ""))
            and not NOT_TITLE.match(line) and "@" not in line)


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


def discover(text, max_results=8, language=None):
    queries = suggest_queries(text)
    if not queries:
        return {"status": "no_query", "sources": [], "queries": 0, "message": "No search phrase could be extracted"}
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        outcomes = list(pool.map(lambda q: _safe(_search, q, language, max_results), queries))
    failures = [o for o in outcomes if isinstance(o, Exception)]
    if len(failures) == len(outcomes):
        return {"status": "unavailable", "sources": [], "queries": len(queries), "query_texts": queries,
                "message": "OpenAlex unavailable or rate limited (" + type(failures[0]).__name__ + ")"}
    sources, seen = [], set()
    for results in outcomes:
        if isinstance(results, Exception):
            continue
        for work in results[:max_results]:
            if not isinstance(work, dict) or work.get("id") in seen:
                continue
            seen.add(work.get("id"))
            title = str(work.get("display_name") or "Untitled")
            url = work.get("doi") or work.get("id") or ""
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
            year = work.get("publication_year")
            sources.append({"title": title[:300], "url": url[:500], "year": year if type(year) is int else None,
                            "abstract": abstract[:20000], "coverage": "abstract" if abstract else "metadata_only",
                            "access": open_access_links(work)})
    status = "partial" if failures else "searched"
    return {"status": status, "sources": sources, "queries": len(queries), "query_texts": queries,
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


def _safe(fn, *args):
    try:
        return fn(*args)
    except (TimeoutError, HTTPError, URLError, OSError, ValueError) as exc:
        return exc
