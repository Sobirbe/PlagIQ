"""Full-text comparison at check time (transient; nothing is stored).

For the most relevant open-access works an academic search returns, download
the full text (publisher PDF link, the PDF named on the article page, or
optionally OpenAlex's billed copy), extract it in memory and return it for one
comparison. Texts are discarded after the check. Legal basis confirmed by the
owner's counsel for transient comparison; storing still requires a reuse
licence (see oa_import.py).

Settings (website service):
  FULLTEXT_FETCH=0          turn off (default on)
  FULLTEXT_MAX=8            works fetched per check (1-20)
  FULLTEXT_SECONDS=30       time limit for all downloads in one check (5-90); each request max 10 s
  FULLTEXT_OPENALEX_PDF=1   use OpenAlex's PDF copy first (most reliable; ~$0.01 each)
"""
import os
import re
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urljoin, urlsplit

import extract
from harvest import MAX_PAGE, MAX_PDF, Fetcher, HarvestError, _PageMeta

OPENALEX_CONTENT = "https://content.openalex.org"
MIN_WORDS = 200
MAX_PAGES = 300


def settings():
    def bounded(name, default, low, high):
        try:
            return min(max(int(os.environ.get(name, default)), low), high)
        except ValueError:
            return default
    return (os.environ.get("FULLTEXT_FETCH", "1") != "0", bounded("FULLTEXT_MAX", 8, 1, 20),
            bounded("FULLTEXT_SECONDS", 30, 5, 90), os.environ.get("FULLTEXT_OPENALEX_PDF") == "1")


PER_REQUEST_SECONDS = 10


def make_fetcher():
    return Fetcher(delay=0, timeout=PER_REQUEST_SECONDS)


def _pdf_text(data):
    if not data.startswith(b"%PDF"):
        raise HarvestError("not a PDF")
    import sandbox
    text = sandbox.extract_text("work.pdf", data, max_pages=MAX_PAGES)   # downloaded files: read in a limited process
    if len(text.split()) < MIN_WORDS:
        raise HarvestError("PDF has too little text (scanned?)")
    return text


def _from_url(url, fetcher):
    """A link may give a PDF directly, or (often for doi.org links) an article
    page whose citation_pdf_url names the PDF."""
    body, ctype = fetcher.get(url, MAX_PDF, cross_host=True)
    if body.startswith(b"%PDF"):
        return _pdf_text(body)
    if "html" not in (ctype or "").lower() and not body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        raise HarvestError("not a PDF")
    meta = _LinkMeta()
    meta.feed(body[:MAX_PAGE].decode("utf-8", "replace"))
    page = getattr(fetcher, "last_url", None) or url
    pdf_url = urljoin(page, meta.pdf) if meta.pdf else _pdf_link(meta.links, page, url)
    if not pdf_url:
        raise HarvestError("page names no PDF")
    return _pdf_text(fetcher.get(pdf_url, MAX_PDF, cross_host=True)[0])


class _LinkMeta(_PageMeta):
    """citation_pdf_url, and also the page's ordinary links to PDF files (for pages without that tag)."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        href = dict(attrs).get("href") or ""
        if tag == "a" and (urlsplit(href).path.lower().endswith(".pdf") or "/bitstream/" in href) and len(self.links) < 200:
            self.links.append(href)


def _digits(text, years_short=False):
    groups = re.findall(r"\d{2,}", text)
    return {g[-2:] if years_short and len(g) == 4 and g[:2] in ("19", "20") else g for g in groups}


def _pdf_link(links, page, asked):
    """A PDF link on the article page that clearly belongs to this work: the only PDF link on the same website, or
    the one whose file name carries the numbers of the link that was asked for (a DOI such as swp-2007-35 and a file
    wp07-35.pdf). Anything less certain is not used."""
    host = (urlsplit(page).hostname or "").removeprefix("www.")
    same = []
    for href in links:
        full = urljoin(page, href)
        if (urlsplit(full).hostname or "").removeprefix("www.") == host and full not in same:
            same.append(full)
    if len(same) == 1:
        return same[0]
    last = lambda u: ([part for part in urlsplit(u).path.split("/") if part] or [""])[-1]
    wanted = _digits(last(asked), years_short=True)
    if not wanted:
        return None
    fitting = [u for u in same if wanted <= _digits(last(u), years_short=True)]
    return fitting[0] if len(fitting) == 1 else None


def reason(exc):
    """Short, countable category for a failed download."""
    text = str(exc).lower()
    for needle, label in (("timed out", "timed out"), ("timeout", "timed out"), ("robots", "blocked by robots.txt"),
                          ("403", "refused by the website (403)"), ("401", "refused by the website (401)"),
                          ("404", "not found (404)"), ("not a pdf", "not a PDF"), ("names no pdf", "no PDF link on page"),
                          ("text on page", "too little text on page"), ("too little text", "scanned PDF (no text)"), ("resolve", "website not reachable"),
                          ("refused", "address refused"), ("over", "file too large"), ("cost limit", "cost limit")):
        if needle in text:
            return label
    return "other error"


def _openalex_copy(access, fetcher, content_base):
    key = os.environ.get("OPENALEX_API_KEY", "")
    headers = {"Authorization": "Bearer " + key} if key else {}
    data, _ = fetcher.get(f"{content_base or OPENALEX_CONTENT}/works/{access['openalex_id']}.pdf", MAX_PDF,
                          robots=False, headers=headers)
    return _pdf_text(data)


def fetch_one(access, fetcher, allow_paid, content_base=None):
    """Full text for one work, or raise HarvestError listing why each copy failed.
    With allow_paid, OpenAlex's own copy is tried first: it is the most reliable."""
    problems = []
    paid = allow_paid and access.get("openalex_pdf") and access.get("openalex_id")
    if paid:
        try:
            return _openalex_copy(access, fetcher, content_base), "OpenAlex copy"
        except (HarvestError, urllib.error.URLError, OSError, ValueError) as exc:
            problems.append(reason(exc))
    seen = set()
    for url, via in [(u, "pdf") for u in access.get("pdf_urls") or []] + [(u, "article page") for u in access.get("pages") or []]:
        if url in seen:
            continue
        seen.add(url)
        try:
            return _from_url(url, fetcher), via
        except (HarvestError, urllib.error.URLError, OSError, ValueError) as exc:
            problems.append(reason(exc))
    raise HarvestError(problems[-1] if problems else "no open-access copy listed")


def fetch_fulltexts(sources, max_works=8, seconds=25, allow_paid=False, content_base=None, fetcher_factory=None):
    """Adds 'fulltext' (and 'fulltext_via') to up to max_works sources, in place.
    Returns counts; never raises. Works not finished within `seconds` are skipped."""
    candidates = [s for s in sources if (s.get("access") or {}).get("pdf_urls") or (s.get("access") or {}).get("pages")
                  or (allow_paid and (s.get("access") or {}).get("openalex_pdf"))][:max_works]
    stats = {"attempted": len(candidates), "compared": 0, "failed": 0, "timed_out": 0, "reasons": {}}
    if not candidates:
        return stats
    factory = fetcher_factory or make_fetcher
    deadline = time.monotonic() + seconds

    def task(source):
        return fetch_one(source["access"], factory(), allow_paid, content_base)

    pool = ThreadPoolExecutor(max_workers=min(4, len(candidates)))
    futures = {pool.submit(task, s): s for s in candidates}
    done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    for future in done:
        source = futures[future]
        try:
            source["fulltext"], source["fulltext_via"] = future.result()
            stats["compared"] += 1
        except Exception as exc:                          # one failing download never breaks a check
            stats["failed"] += 1
            label = reason(exc)
            stats["reasons"][label] = stats["reasons"].get(label, 0) + 1
    stats["timed_out"] = len(pending)
    if pending:
        stats["reasons"]["timed out"] = stats["reasons"].get("timed out", 0) + len(pending)
    pool.shutdown(wait=False, cancel_futures=True)
    return stats
