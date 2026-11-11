"""Internet search module (Yandex Search API v2), transient comparison.

With the user's separate consent, the same short excerpts already used for the
OpenAlex search (at most 3, each at most 14 words) are searched on the web with
Yandex: first as an exact phrase in quotes, then without quotes if nothing is
found. The top result pages (HTML or PDF) are downloaded, compared once and
discarded, like open-access full texts (see fulltext.py).

Settings (website service):
  YANDEX_API_KEY            API key of a Yandex Cloud service account with search rights
  YANDEX_FOLDER_ID          Yandex Cloud folder ID the key belongs to (not secret)
  WEB_SEARCH=0              turn the module off even when a key is set
  WEB_MAX_PAGES=6           pages downloaded per check (1-15)
  WEB_SECONDS=20            time limit for page downloads per check (5-60)
  YANDEX_SEARCH_URL         override the API address if Yandex changes it
Request format: POST {"query": {"searchType", "queryText", ...}, "folderId",
"responseFormat": "FORMAT_XML"} with header "Authorization: Api-Key ..."; the
response's rawData is base64-encoded XML (<yandexsearch>/<response>/<results>).
"""
import base64
import html
import html.parser
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlsplit
from xml.etree import ElementTree

import fulltext
from harvest import MAX_PDF, HarvestError

API = "https://searchapi.api.cloud.yandex.net/v2/web/search"
SEARCH_TYPE = {"ru": "SEARCH_TYPE_RU", "uz": "SEARCH_TYPE_RU", "en": "SEARCH_TYPE_COM"}
L10N = {"ru": "LOCALIZATION_RU", "uz": "LOCALIZATION_RU", "en": "LOCALIZATION_EN"}
MAX_RESPONSE = 5 * 1024 * 1024
MIN_WORDS = 150
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "aside", "template", "iframe"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "td", "th"}


def settings():
    def bounded(name, default, low, high):
        try:
            return min(max(int(os.environ.get(name, default)), low), high)
        except ValueError:
            return default
    configured = bool(os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID"))
    return (configured and os.environ.get("WEB_SEARCH", "1") != "0", bounded("WEB_MAX_PAGES", 6, 1, 15),
            bounded("WEB_SECONDS", 20, 5, 60))


# ----------------------------------------------------------------- search

def _yandex(query, language):
    body = {"query": {"searchType": SEARCH_TYPE.get(language, "SEARCH_TYPE_RU"), "queryText": query,
                      "familyMode": "FAMILY_MODE_MODERATE", "page": "0", "fixTypoMode": "FIX_TYPO_MODE_OFF"},
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": "10", "docsInGroup": "1"},
            "maxPassages": "2", "l10n": L10N.get(language, "LOCALIZATION_RU"),
            "folderId": os.environ.get("YANDEX_FOLDER_ID", ""), "responseFormat": "FORMAT_XML"}
    request = urllib.request.Request(os.environ.get("YANDEX_SEARCH_URL") or API, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Api-Key " + os.environ.get("YANDEX_API_KEY", ""),
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise HarvestError("search response too large")
    data = json.loads(raw)
    return parse_results(base64.b64decode(data.get("rawData") or ""))


def parse_results(xml_bytes):
    """[(url, title)] from Yandex XML; [] for 'no results'."""
    if not xml_bytes or b"<!DOCTYPE" in xml_bytes[:2048].upper():
        return []
    root = ElementTree.fromstring(xml_bytes)
    error = root.find(".//response/error")
    if error is not None:
        if error.get("code") == "15":                       # nothing found
            return []
        raise HarvestError(f"Yandex error {error.get('code')}: {(error.text or '').strip()[:100]}")
    results = []
    for doc in root.iterfind(".//results//doc"):
        url = (doc.findtext("url") or "").strip()
        title = " ".join("".join(doc.find("title").itertext()).split()) if doc.find("title") is not None else url
        if url.startswith(("https://", "http://")):
            results.append((url[:500], title[:300] or url[:300]))
    return results


def search(queries, language):
    """Exact-phrase search per excerpt, unquoted if the phrase finds nothing.
    Returns ([(url, title)], searches made, errors)."""
    found, seen, calls, errors = [], set(), 0, []
    for query in queries:
        for text in (f'"{query}"', query):
            calls += 1
            try:
                hits = _yandex(text, language)
            except (HarvestError, urllib.error.URLError, OSError, ValueError, ElementTree.ParseError) as exc:
                errors.append(fulltext.reason(exc) if not isinstance(exc, urllib.error.HTTPError) else f"search refused ({exc.code})")
                break
            for url, title in hits:
                key = url.split("#")[0].rstrip("/")
                if key not in seen:
                    seen.add(key)
                    found.append((url, title))
            if hits:
                break
    return found, calls, errors


# ----------------------------------------------------------------- pages

class _Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def decode(body, content_type):
    """Bytes to text using the declared charset; many older RU/UZ sites use windows-1251."""
    declared = re.search(r"charset=([\w-]+)", content_type or "", re.I) or \
        re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', body[:4096], re.I)
    charsets = [declared.group(1).decode() if isinstance(declared.group(1), bytes) else declared.group(1)] if declared else []
    for charset in charsets + ["utf-8", "windows-1251"]:
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def html_text(body, content_type=""):
    parser = _Text()
    parser.feed(decode(body, content_type))
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def page_text(url, fetcher):
    if url.startswith("http://") and urlsplit(url).hostname not in getattr(fetcher, "allow_http_hosts", ()):
        url = "https://" + url[len("http://"):]             # fetch over HTTPS only
    body, ctype = fetcher.get(url, MAX_PDF, cross_host=True)
    if body.startswith(b"%PDF"):
        return fulltext._pdf_text(body)
    text = html_text(body, ctype)
    if len(text.split()) < MIN_WORDS:
        raise HarvestError("too little text on page")
    return text


def fetch_pages(results, max_pages, seconds, fetcher_factory=None):
    """Download up to max_pages result pages within `seconds`; returns (sources, stats)."""
    chosen = results[:max_pages]
    stats = {"attempted": len(chosen), "compared": 0, "failed": 0, "timed_out": 0, "reasons": {}}
    sources = []
    if not chosen:
        return sources, stats
    factory = fetcher_factory or fulltext.make_fetcher
    pool = ThreadPoolExecutor(max_workers=min(4, len(chosen)))
    futures = {pool.submit(page_text, url, factory()): (url, title) for url, title in chosen}
    done, pending = wait(futures, timeout=seconds)
    for future in done:
        url, title = futures[future]
        try:
            text = future.result()
        except Exception as exc:
            stats["failed"] += 1
            label = fulltext.reason(exc)
            stats["reasons"][label] = stats["reasons"].get(label, 0) + 1
            continue
        stats["compared"] += 1
        host = urlsplit(url).hostname or ""
        sources.append({"title": title or host, "url": url, "year": None, "coverage": "web", "abstract": "",
                        "fulltext": text, "fulltext_via": "web"})
    stats["timed_out"] = len(pending)
    if pending:
        stats["reasons"]["timed out"] = stats["reasons"].get("timed out", 0) + len(pending)
    pool.shutdown(wait=False, cancel_futures=True)
    return sources, stats


def run(queries, language, fetcher_factory=None):
    enabled, max_pages, seconds = settings()
    if not enabled or not queries:
        return [], {"enabled": enabled, "searches": 0, "results": 0, "attempted": 0, "compared": 0,
                    "failed": 0, "timed_out": 0, "reasons": {}, "errors": []}
    results, calls, errors = search(queries, language)
    sources, stats = fetch_pages(results, max_pages, seconds, fetcher_factory)
    return sources, {"enabled": True, "searches": calls, "results": len(results), "errors": errors, **stats}
