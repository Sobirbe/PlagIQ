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

RIGHTS = ("written_permission", "open_license", "public_domain")
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

async def run_check(text: str, language: str) -> dict:
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
            response=await client.post("/v1/checks",json={"query_text":text,"language":segmentation})
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
            year = src.get("year")
            passage_count = src.get("passage_count", len(passages))
            sources.append({
                "title": str(src.get("source_filename") or "Indexed document")[:250],
                "url": url if urlsplit(url).scheme == "https" else "",
                "coverage": "indexed full-text source",
                "matched_chars": matched_chars,
                "authors": str(src.get("authors") or "")[:300],
                "year": year if type(year) is int and 1000 <= year <= 2200 else None,
                "language": src.get("language") if src.get("language") in LANGUAGES else None,
                "rights_basis": src.get("rights_basis") if src.get("rights_basis") in RIGHTS else "unknown",
                "passage_count": passage_count if type(passage_count) is int and passage_count >= len(passages) else len(passages),
                "passages": passages,
            })
        return {"percent": round(100 * matched / document_length, 2),
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
