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
    pass

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
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(15.0), trust_env=False,
                                     headers={"Authorization": "Bearer " + os.environ["ENGINE_API_KEY"]}) as client:
            response=await client.post("/v1/checks",json={"query_text":text,"language":segmentation})
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
            raise EngineError("Corpus search timed out.")
    except httpx.HTTPError as exc:
        raise EngineError("Private corpus engine is unavailable.") from exc
    except (ValueError,KeyError,TypeError,AttributeError) as exc:
        raise EngineError("Unexpected corpus engine response.") from exc

def summarize(report: dict, max_sources=25) -> dict:
    try:
        document_length = report["query_text_length"]
        if type(document_length) is not int or not 0 < document_length <= 300000:
            raise ValueError()
        intervals = report["unique_passages"]
        for pair in intervals:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(type(x) is not int for x in pair)
                    or not 0 <= pair[0] < pair[1] <= document_length):
                raise ValueError()
        intervals = merge(intervals)
        matched = sum(e-s for s, e in intervals)
        counts = report.get('language_counts', {})
        if not isinstance(counts, dict) or any(type(counts.get(lang, 0)) is not int or counts.get(lang, 0) < 0 for lang in ('uz','ru','en')):
            raise ValueError()
        if report.get('corpus_sources') is not None and (type(report['corpus_sources']) is not int or report['corpus_sources'] != sum(counts.values())):
            raise ValueError()
        sources = []
        for src in report.get("sources", [])[:max_sources]:
            url = str(src.get("source_url") or "")[:500]
            if type(src.get('matched_chars')) is not int or not 0 <= src['matched_chars'] <= document_length:
                raise ValueError()
            passages = src.get('passages', [])[:5]
            for passage in passages:
                if (not isinstance(passage, dict) or
                    any(type(passage.get(k)) is not int for k in ('query_start','query_end','source_start','source_end')) or
                    not 0 <= passage['query_start'] < passage['query_end'] <= document_length or
                    not 0 <= passage['source_start'] < passage['source_end']):
                    raise ValueError()
                passage['source_excerpt'] = str(passage.get('source_excerpt', ''))[:500]
            sources.append({
                "title": str(src.get("source_filename") or "Indexed document")[:250],
                "url": url if urlsplit(url).scheme == "https" else "",
                "coverage": "indexed full-text source",
                "matched_chars": int(src.get("matched_chars") or 0),
                "authors": str(src.get("authors") or ""),
                "year": src.get("year"),
                "rights_basis": str(src.get("rights_basis") or "unknown"),
                "passages": passages,
            })
        return {"percent": round(100 * matched / document_length, 2),
                "matched_chars": matched, "total_chars": document_length,
                "coverage": str(report.get("coverage") or "unknown"),
                "corpus_size": report.get("corpus_sources"),
                "intervals": intervals, "sources": sources,
                "language_counts": report.get("language_counts", {}),
                "sources_truncated": len(report.get("sources", [])) > max_sources}
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
