"""Internet search module (Yandex Search API v2 and/or Brave Search API), transient comparison.

With the user's separate consent, short excerpts (at most 14 words each) spread
across the whole document, one per WEB_WORDS_PER_EXCERPT words and at most
WEB_MAX_QUERIES, are searched on the web with
the configured providers: first as an exact phrase in quotes, then without
quotes if nothing is found. Provider order depends on the language (Yandex first
for Russian and Uzbek, Brave first for English); the second provider is used for
an excerpt only if the first fails. The top result pages (HTML or PDF) are downloaded, compared once and
discarded, like open-access full texts (see fulltext.py).

Settings (website service):
  BRAVE_API_KEY             Brave Search API subscription token (api-dashboard.search.brave.com)
  BRAVE_INTERVAL=1          seconds between Brave requests (plan rate limit; 0-5)
  YANDEX_API_KEY            API key of a Yandex Cloud service account with search rights
  YANDEX_FOLDER_ID          Yandex Cloud folder ID the key belongs to (not secret)
  WEB_SEARCH=0              turn the module off even when a key is set
  WEB_MAX_QUERIES=20        most excerpts searched per document (3-40)
  WEB_WORDS_PER_EXCERPT=500 one excerpt per this many words (200-5000)
  WEB_MAX_PAGES=10          pages downloaded per check (1-20); pages found by several
                            excerpts are downloaded first
  WEB_SECONDS=20            time limit for page downloads per check (5-60)
  YANDEX_SEARCH_URL         override the API address if Yandex changes it
Yandex request format: POST {"query": {"searchType", "queryText", ...}, "folderId",
"responseFormat": "FORMAT_XML"} with header "Authorization: Api-Key ..."; the
response's rawData is base64-encoded XML (<yandexsearch>/<response>/<results>).
Brave: GET .../res/v1/web/search?q=... with header X-Subscription-Token; JSON
web.results[].url/title.
"""
import base64
import html
import html.parser
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree

import fulltext
from harvest import MAX_PDF, HarvestError

API = "https://searchapi.api.cloud.yandex.net/v2/web/search"
BRAVE_API = "https://api.search.brave.com/res/v1/web/search"
BRAVE_LANG = {"ru": "ru", "en": "en"}                       # no Uzbek search language at Brave: no filter
SEARCH_TYPE = {"ru": "SEARCH_TYPE_RU", "uz": "SEARCH_TYPE_RU", "en": "SEARCH_TYPE_COM"}
L10N = {"ru": "LOCALIZATION_RU", "uz": "LOCALIZATION_RU", "en": "LOCALIZATION_EN"}
MAX_RESPONSE = 5 * 1024 * 1024
MIN_WORDS = 150
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "aside", "template", "iframe"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "td", "th"}


def _bounded(name, default, low, high):
    try:
        return min(max(float(os.environ.get(name, default)), low), high)
    except ValueError:
        return default


def providers(language=None):
    """Configured provider names, in the preferred order for `language`."""
    if os.environ.get("WEB_SEARCH", "1") == "0":
        return []
    names = []
    if os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID"):
        names.append("Yandex")
    if os.environ.get("BRAVE_API_KEY"):
        names.append("Brave")
    return sorted(names, key=lambda n: n != "Brave") if language == "en" else names


def settings():
    return (bool(providers()), int(_bounded("WEB_MAX_PAGES", 10, 1, 20)), int(_bounded("WEB_SECONDS", 20, 5, 60)))


def max_queries():
    return int(_bounded("WEB_MAX_QUERIES", 20, 3, 40))


def excerpt_count(words):
    """How many excerpts to search for a document of `words` words (at least 3)."""
    per = int(_bounded("WEB_WORDS_PER_EXCERPT", 500, 200, 5000))
    return max(3, min(max_queries(), -(-words // per)))


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


_brave_lock = threading.Lock()
_brave_last = [0.0]


def _brave(query, language):
    params = {"q": query, "count": "10", "safesearch": "moderate", "text_decorations": "false"}
    if language in BRAVE_LANG:
        params["search_lang"] = BRAVE_LANG[language]
    request = urllib.request.Request((os.environ.get("BRAVE_SEARCH_URL") or BRAVE_API) + "?" + urlencode(params),
                                     headers={"X-Subscription-Token": os.environ.get("BRAVE_API_KEY", ""),
                                              "Accept": "application/json"})
    with _brave_lock:                                       # respect the plan's requests-per-second limit
        wait = _bounded("BRAVE_INTERVAL", 1, 0, 5) - (time.monotonic() - _brave_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE + 1)
        finally:
            _brave_last[0] = time.monotonic()
    if len(raw) > MAX_RESPONSE:
        raise HarvestError("search response too large")
    return parse_brave(json.loads(raw))


def parse_brave(data):
    """[(url, title)] from a Brave web search JSON response."""
    items = ((data.get("web") or {}).get("results") or []) if isinstance(data, dict) else []
    results = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = " ".join(html.unescape(str(item.get("title") or "")).split())
        if url.startswith(("https://", "http://")):
            results.append((url[:500], title[:300] or url[:300]))
    return results


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


UNQUOTED_RETRIES = 3        # only the first excerpts get an unquoted second search (cost)


def search(queries, language):
    """Per excerpt: exact phrase (then unquoted for the first excerpts if nothing is found);
    the next provider only if the first one fails. Results are ordered by how many
    excerpts found the page, then by first appearance.
    Returns ([(url, title)], searches made, errors, providers used)."""
    engines = {"Yandex": _yandex, "Brave": _brave}
    found, seen, calls, errors, used = [], {}, 0, [], []
    for index, query in enumerate(queries):
        hit_here = set()
        for name in providers(language):
            failed = False
            for text in ((f'"{query}"', query) if index < UNQUOTED_RETRIES else (f'"{query}"',)):
                calls += 1
                try:
                    hits = engines[name](text, language)
                except (HarvestError, urllib.error.URLError, OSError, ValueError, ElementTree.ParseError) as exc:
                    label = f"search refused ({exc.code})" if isinstance(exc, urllib.error.HTTPError) else fulltext.reason(exc)
                    errors.append(f"{name}: {label}")
                    failed = True
                    break
                if name not in used:
                    used.append(name)
                for url, title in hits:
                    key = url.split("#")[0].rstrip("/")
                    if key not in seen:
                        seen[key] = [0, len(found)]
                        found.append((url, title))
                    if key not in hit_here:
                        hit_here.add(key)
                        seen[key][0] += 1
                if hits:
                    break
            if not failed:
                break
    order = {url: seen[url.split("#")[0].rstrip("/")] for url, _ in found}
    found.sort(key=lambda item: (-order[item[0]][0], order[item[0]][1]))
    return found, calls, errors, used


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
        return [], {"enabled": enabled, "providers": [], "searches": 0, "results": 0, "attempted": 0, "compared": 0,
                    "failed": 0, "timed_out": 0, "reasons": {}, "errors": []}
    results, calls, errors, used = search(queries, language)
    sources, stats = fetch_pages(results, max_pages, seconds, fetcher_factory)
    return sources, {"enabled": True, "providers": used or providers(language), "excerpts": len(queries), "searches": calls,
                     "results": len(results), "errors": errors, **stats}
