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
  FULLTEXT_SECONDS=25       time limit for all downloads in one check (5-90)
  FULLTEXT_OPENALEX_PDF=1   allow OpenAlex's PDF copy when no other copy works (~$0.01 each)
"""
import os
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urljoin

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
            bounded("FULLTEXT_SECONDS", 25, 5, 90), os.environ.get("FULLTEXT_OPENALEX_PDF") == "1")


def make_fetcher():
    return Fetcher(delay=0)


def _pdf_text(data):
    if not data.startswith(b"%PDF"):
        raise HarvestError("not a PDF")
    text = extract.extract_text("work.pdf", data, max_pages=MAX_PAGES)
    if len(text.split()) < MIN_WORDS:
        raise HarvestError("PDF has too little text (scanned?)")
    return text


def fetch_one(access, fetcher, allow_paid, content_base=None):
    """Full text for one work, or raise HarvestError with the reason."""
    problems = []
    for url in access.get("pdf_urls") or []:
        try:
            return _pdf_text(fetcher.get(url, MAX_PDF, cross_host=True)[0]), "pdf"
        except (HarvestError, urllib.error.URLError, OSError, ValueError) as exc:
            problems.append(str(exc)[:80])
    for page in access.get("pages") or []:
        try:
            body, _ = fetcher.get(page, MAX_PAGE, cross_host=True)
            meta = _PageMeta()
            meta.feed(body.decode("utf-8", "replace"))
            if meta.pdf:
                url = urljoin(getattr(fetcher, "last_url", None) or page, meta.pdf)
                return _pdf_text(fetcher.get(url, MAX_PDF, cross_host=True)[0]), "article page"
            problems.append("page names no PDF")
        except (HarvestError, urllib.error.URLError, OSError, ValueError) as exc:
            problems.append(str(exc)[:80])
    if allow_paid and access.get("openalex_pdf") and access.get("openalex_id"):
        key = os.environ.get("OPENALEX_API_KEY", "")
        headers = {"Authorization": "Bearer " + key} if key else {}
        data, _ = fetcher.get(f"{content_base or OPENALEX_CONTENT}/works/{access['openalex_id']}.pdf", MAX_PDF,
                              robots=False, headers=headers)
        return _pdf_text(data), "OpenAlex copy"
    raise HarvestError("; ".join(problems) or "no open-access copy listed")


def fetch_fulltexts(sources, max_works=8, seconds=25, allow_paid=False, content_base=None, fetcher_factory=None):
    """Adds 'fulltext' (and 'fulltext_via') to up to max_works sources, in place.
    Returns counts; never raises. Works not finished within `seconds` are skipped."""
    candidates = [s for s in sources if (s.get("access") or {}).get("pdf_urls") or (s.get("access") or {}).get("pages")
                  or (allow_paid and (s.get("access") or {}).get("openalex_pdf"))][:max_works]
    stats = {"attempted": len(candidates), "compared": 0, "failed": 0, "timed_out": 0}
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
        except Exception:                                 # one failing download never breaks a check
            stats["failed"] += 1
    stats["timed_out"] = len(pending)
    pool.shutdown(wait=False, cancel_futures=True)
    return stats
