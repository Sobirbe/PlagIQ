"""Optional private noplag-engine client for PlagIQ.

Only configure ENGINE_URL to a trusted *internal* service. No public routes for
corpus ingestion are provided. This client never uses uploaded URLs.
"""
from __future__ import annotations
import asyncio
import os
import html
import re
from corpus import merge
from urllib.parse import urlsplit
import httpx

class EngineError(RuntimeError):
    """Controlled engine failure. `code` is a stable, translatable identifier."""
    def __init__(self, message, code="engine_unavailable"):
        super().__init__(message)
        self.code = code

RIGHTS = ("written_permission", "open_license", "public_domain", "author_deposit", "institution_deposit")
DEPOSITS = ("author_deposit", "institution_deposit")
LANGUAGES = ("uz", "ru", "en")

def configured() -> bool:
    return bool(os.environ.get("ENGINE_URL", "").strip()) and len(os.environ.get("ENGINE_API_KEY", "")) >= 32

def engine_base_url() -> str:
    url = os.environ.get("ENGINE_URL", "").rstrip("/")
    if not url:
        raise EngineError("Private corpus engine not configured.")
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise EngineError("Invalid ENGINE_URL.") from exc
    if parsed.scheme != "http" or parsed.hostname not in ("engine", "noplag-engine", "localhost", "127.0.0.1") and not (parsed.hostname or "").endswith(".railway.internal"):
        raise EngineError("ENGINE_URL must point to a trusted internal HTTP service.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise EngineError("Invalid ENGINE_URL.")
    return url

def safe_path(value):
    """Engine returns relative API paths; reject absolute/foreign URLs."""
    if not isinstance(value, str) or not re.fullmatch(r"/v1/checks/[a-f0-9]{32}(?:/report)?", value):
        raise EngineError("Unexpected private engine response.")
    return value

async def run_check(text: str, language: str, exclude=()) -> dict:
    base=engine_base_url()
    if len(text) > 300000:
        raise EngineError("Document exceeds pilot engine limit.")
    segmentation=language
    if not configured():
        raise EngineError("Private corpus authentication is not configured.")
    try:
        timeout=float(os.environ.get("ENGINE_TIMEOUT","120"))
    except ValueError as exc:
        raise EngineError("Invalid engine timeout configuration.") from exc
    timeout=max(15,min(timeout,180))
    try:
        # The engine fingerprints synchronously inside POST, so the request
        # itself needs the configured scan timeout, not a fixed 15 seconds.
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(timeout, connect=10.0), trust_env=False,
                                     follow_redirects=False,
                                     headers={"Authorization": "Bearer " + os.environ["ENGINE_API_KEY"]}) as client:
            response=await client.post("/v1/checks",json={"query_text":text,"language":segmentation,"exclude":list(exclude)[:5]})
            if response.status_code == 429:
                raise EngineError("Private corpus engine is busy. Try again shortly.", "engine_busy")
            if response.status_code != 202:
                raise EngineError("Engine rejected document ("+str(response.status_code)+").")
            urls=response.json()
            status_url=safe_path(urls.get("status_url"))
            report_url=safe_path(urls.get("report_url"))
            if report_url != status_url + '/report':
                raise EngineError("Inconsistent private engine job paths.")
            loop=asyncio.get_running_loop()
            stop=loop.time()+timeout
            while loop.time() < stop:
                status_resp=await client.get(status_url)
                status_resp.raise_for_status()
                state=status_resp.json()
                if state.get("status")=="complete":
                    result=await client.get(report_url)
                    result.raise_for_status()
                    return result.json()
                if state.get("status")=="failed":
                    raise EngineError("Corpus engine could not finish the scan.")
                await asyncio.sleep(1.5)
            raise EngineError("Corpus search timed out.", "engine_timeout")
    except httpx.HTTPError as exc:
        raise EngineError("Private corpus engine is unavailable.") from exc
    except (ValueError,KeyError,TypeError,AttributeError) as exc:
        raise EngineError("Unexpected corpus engine response.") from exc

def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError()
    return value


def summarize(report: dict, max_sources=25) -> dict:
    """Recompute overlap from validated spans; never trust engine totals.

    Every field passed on to the browser or PDF is type-checked, bounded and
    rebuilt from an allowlist, so a faulty or compromised engine cannot inject
    unexpected keys, oversized strings or out-of-range offsets.
    """
    try:
        if not isinstance(report, dict):
            raise ValueError()
        document_length = _int(report["query_text_length"], 1, 300000)
        intervals = report["unique_passages"]
        if not isinstance(intervals, list) or len(intervals) > 300000:
            raise ValueError()
        for pair in intervals:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(type(x) is not int for x in pair)
                    or not 0 <= pair[0] < pair[1] <= document_length):
                raise ValueError()
        intervals = merge(intervals)
        matched = sum(e-s for s, e in intervals)
        raw_counts = report.get('language_counts', {})
        if not isinstance(raw_counts, dict):
            raise ValueError()
        counts = {lang: _int(raw_counts.get(lang, 0), 0, 10**9) for lang in LANGUAGES}
        corpus_size = report.get('corpus_sources')
        if corpus_size is not None and (type(corpus_size) is not int or corpus_size != sum(counts.values())):
            raise ValueError()
        raw_sources = report.get("sources", [])
        if not isinstance(raw_sources, list):
            raise ValueError()
        sources = []
        for src in raw_sources[:max_sources]:
            if not isinstance(src, dict):
                raise ValueError()
            url = str(src.get("source_url") or "")[:500]
            matched_chars = _int(src.get('matched_chars'), 0, document_length)
            raw_passages = src.get('passages', [])
            if not isinstance(raw_passages, list):
                raise ValueError()
            passages = []
            for passage in raw_passages[:5]:
                if (not isinstance(passage, dict) or
                    any(type(passage.get(k)) is not int for k in ('query_start','query_end','source_start','source_end')) or
                    not 0 <= passage['query_start'] < passage['query_end'] <= document_length or
                    not 0 <= passage['source_start'] < passage['source_end']):
                    raise ValueError()
                passages.append({k: passage[k] for k in ('query_start','query_end','source_start','source_end')}
                                | {'source_excerpt': str(passage.get('source_excerpt', ''))[:501]})
            matched_words = src.get("matched_words", 0)
            if type(matched_words) is not int or not 0 <= matched_words <= document_length:
                raise ValueError()
            year = src.get("year")
            passage_count = src.get("passage_count", len(passages))
            deposit = src.get("rights_basis") in DEPOSITS
            if deposit:
                passages = [p | {"source_excerpt": ""} for p in passages]       # unpublished text is never shown
            sources.append({
                "title": str(src.get("source_filename") or "Indexed document")[:250],
                "url": url if urlsplit(url).scheme == "https" else "",
                "coverage": "archive" if deposit else "indexed full-text source",
                "owner": str(src.get("owner") or "")[:40] if deposit else "",
                "collection": str(src.get("collection") or "")[:120] if src.get("rights_basis") == "institution_deposit" else "",
                "matched_chars": matched_chars,
                "matched_tokens": matched_words,
                "authors": str(src.get("authors") or "")[:300],
                "year": year if type(year) is int and 1000 <= year <= 2200 else None,
                "language": src.get("language") if src.get("language") in LANGUAGES else None,
                "rights_basis": src.get("rights_basis") if src.get("rights_basis") in RIGHTS else "unknown",
                "passage_count": passage_count if type(passage_count) is int and passage_count >= len(passages) else len(passages),
                "passages": [p | {"words": _int(raw.get("words", 0), 0, document_length)}
                             for p, raw in zip(passages, raw_passages)],
            })
        version = report.get("corpus_version")
        if version is not None and not (isinstance(version, str) and re.fullmatch(r"[0-9a-f]{12}", version)):
            raise ValueError()
        return {"percent": round(100 * matched / document_length, 2),
                "corpus_version": version,
                "matched_chars": matched, "total_chars": document_length,
                "coverage": str(report.get("coverage") or "unknown")[:500],
                "corpus_size": sum(counts.values()),
                "intervals": intervals, "sources": sources,
                "language_counts": counts,
                "matched_source_count": len(raw_sources),
                "sources_truncated": len(raw_sources) > max_sources}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise EngineError("Invalid corpus evidence response.") from exc

def highlight_html(text: str, intervals) -> str:
    out=[]
    pos=0
    for start,end in sorted(intervals):
        start=max(pos,int(start))
        end=min(len(text),int(end))
        if start >= end:
            continue
        out.append(html.escape(text[pos:start]))
        out.append("<mark>"+html.escape(text[start:end])+"</mark>")
        pos=end
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n","<br>")


def _client():
    return httpx.AsyncClient(base_url=engine_base_url(), timeout=httpx.Timeout(60.0, connect=10.0), trust_env=False,
                             follow_redirects=False, headers={"Authorization": "Bearer " + os.environ["ENGINE_API_KEY"]})


async def deposit(item: dict) -> str:
    """Add a work to the protected archive; returns its id."""
    try:
        async with _client() as client:
            response = await client.post("/v1/archive", json=item)
    except httpx.HTTPError as exc:
        raise EngineError("Private corpus engine is unavailable.") from exc
    if response.status_code == 422:
        raise EngineError("The archive refused this document: " + str(response.json().get("detail", ""))[:200], "archive_refused")
    if response.status_code != 201:
        raise EngineError("Archive deposit failed (" + str(response.status_code) + ").")
    identifier = response.json().get("id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", str(identifier)):
        raise EngineError("Unexpected private engine response.")
    return identifier


async def list_deposits(owner: str) -> list:
    try:
        async with _client() as client:
            response = await client.get("/v1/archive", params={"owner": owner})
            response.raise_for_status()
            items = response.json().get("items", [])
    except (httpx.HTTPError, ValueError) as exc:
        raise EngineError("Private corpus engine is unavailable.") from exc
    out = []
    for item in items[:500] if isinstance(items, list) else []:
        if isinstance(item, dict) and re.fullmatch(r"[0-9a-f]{64}", str(item.get("id", ""))):
            out.append({"id": item["id"], "title": str(item.get("title", ""))[:300], "authors": str(item.get("authors", ""))[:300],
                        "year": item.get("year") if type(item.get("year")) is int else None,
                        "language": item.get("language") if item.get("language") in LANGUAGES else None,
                        "words": item.get("words") if type(item.get("words")) is int else None,
                        "deposited": str(item.get("deposited") or "")[:10], "collection": str(item.get("collection") or "")[:120]})
    return out


async def remove_deposit(identifier: str, owner: str) -> bool:
    try:
        async with _client() as client:
            response = await client.delete(f"/v1/archive/{identifier}", params={"owner": owner})
    except httpx.HTTPError as exc:
        raise EngineError("Private corpus engine is unavailable.") from exc
    return response.status_code == 200


async def export_archive():
    """Stream the engine's deposit backup (gzip JSON lines)."""
    client = _client()
    request = client.build_request("GET", "/v1/archive/export")
    response = await client.send(request, stream=True)
    if response.status_code != 200:
        await response.aclose(); await client.aclose()
        raise EngineError("Archive export failed (" + str(response.status_code) + ").")

    async def body():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
    return body()


async def library():
    """Library counts from the private engine for the public page; None if unavailable."""
    if not configured():
        return None
    try:
        async with httpx.AsyncClient(base_url=engine_base_url(), timeout=httpx.Timeout(10.0), trust_env=False,
                                     follow_redirects=False,
                                     headers={"Authorization": "Bearer " + os.environ["ENGINE_API_KEY"]}) as client:
            response = await client.get("/v1/library")
            response.raise_for_status()
            data = response.json()
        counts = data.get("language_counts") or {}
        imports = data.get("imports") or {}
        finished = [str(v.get("finished"))[:25] for v in imports.values() if isinstance(v, dict) and v.get("finished")]
        return {"language_counts": {lang: _int(counts.get(lang, 0), 0, 10**9) for lang in LANGUAGES},
                "total": _int(data.get("total", 0), 0, 10**9),
                "disk": {k: _int((data.get("disk") or {}).get(k, 0), 0, 10**15) for k in ("db_bytes", "staging_bytes", "free_bytes", "total_bytes")}
                        if isinstance(data.get("disk"), dict) else None,
                "imports": {str(k)[:40]: {f: v.get(f) for f in ("finished", "imported", "error", "staged", "journals", "downloaded")
                                          if isinstance(v.get(f), (int, str, type(None)))}
                            for k, v in (data.get("imports") or {}).items() if isinstance(v, dict)},
                "by_source": {k: _int(v, 0, 10**9) for k, v in (data.get("by_source") or {}).items()
                              if k in ("wikipedia", "cyberleninka", "journals", "archive", "university", "other")},
                "last_import": max(finished) if finished else None,
                "auto_import": [l for l in data.get("auto_import") or [] if l in LANGUAGES]}
    except (httpx.HTTPError, EngineError, ValueError, TypeError, AttributeError, KeyError):
        return None
