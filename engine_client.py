"""Optional private noplag-engine client for PlagIQ.

Only configure ENGINE_URL to a trusted *internal* service. No public routes for
corpus ingestion are provided. This client never uses uploaded URLs.
"""
from __future__ import annotations
import asyncio
import os
import html
from urllib.parse import urlsplit
import httpx

class EngineError(RuntimeError):
    pass

def configured() -> bool:
    return bool(os.environ.get("ENGINE_URL", "").strip())

def engine_base_url() -> str:
    url = os.environ.get("ENGINE_URL", "").rstrip("/")
    if not url:
        raise EngineError("Private corpus engine not configured.")
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("engine", "noplag-engine", "localhost", "127.0.0.1") and not (parsed.hostname or "").endswith(".railway.internal"):
        raise EngineError("ENGINE_URL must point to a trusted internal HTTP service.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise EngineError("Invalid ENGINE_URL.")
    return url

def safe_path(value):
    """Engine returns relative API paths; reject absolute/foreign URLs."""
    if not isinstance(value, str) or not value.startswith("/v1/checks/") or value.startswith("//"):
        raise EngineError("Unexpected private engine response.")
    parsed=urlsplit(value)
    if parsed.netloc or parsed.scheme or parsed.fragment or parsed.query:
        raise EngineError("Invalid engine URL.")
    return value

async def run_check(text: str, language: str) -> dict:
    base=engine_base_url()
    if len(text) > 300000:
        raise EngineError("Document exceeds pilot engine limit.")
    segmentation={"uz":"en","ru":"ru","en":"en"}.get(language, "en")
    timeout=float(os.environ.get("ENGINE_TIMEOUT","120"))
    timeout=max(15,min(timeout,180))
    try:
        async with httpx.AsyncClient(base_url=base,timeout=httpx.Timeout(15.0)) as client:
            response=await client.post("/v1/checks",json={"query_text":text,"language":segmentation})
            if response.status_code != 202:
                raise EngineError("Engine rejected document ("+str(response.status_code)+").")
            urls=response.json()
            status_url=safe_path(urls.get("status_url"))
            report_url=safe_path(urls.get("report_url"))
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
    except httpx.RequestError as exc:
        raise EngineError("Private corpus engine is unavailable.") from exc
    except (ValueError,KeyError,TypeError) as exc:
        raise EngineError("Unexpected corpus engine response.") from exc

def summarize(report: dict, max_sources=25) -> dict:
    document_length=int(report.get("query_text_length",0))
    intervals=[]
    for interval in report.get("unique_passages",[]):
        if isinstance(interval,(list,tuple)) and len(interval)==2:
            start,end=int(interval[0]),int(interval[1])
            if 0 <= start < end <= document_length:
                intervals.append([start,end])
    sources=[]
    for src in report.get("sources",[])[:max_sources]:
        sources.append({
            "title":str(src.get("source_filename") or "Indexed document")[:250],
            "url":str(src.get("source_url") or "")[:500],
            "coverage":"indexed full-text source",
            "matched_chars":int(src.get("matched_chars") or 0),
            "similarity_pct":float(src.get("similarity_pct") or 0.0),
        })
    return {
        "percent":round(float(report.get("overall_similarity_pct") or 0),2),
        "matched_chars":int(report.get("total_matched_chars") or 0),
        "total_chars":document_length,
        "coverage":str(report.get("coverage") or "unknown"),
        "corpus_size":report.get("corpus_sources"),
        "intervals":intervals,
        "sources":sources,
    }

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
