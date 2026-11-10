import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
import re
import time
import uuid
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartParser

import citations
import corpus
import extract
from discovery import discover
import html
from engine_client import EngineError, configured as corpus_configured, run_check, summarize
from report_pdf import build_pdf
from security import SecurityMiddleware, MAX_REQUEST

# The whole request is capped by SecurityMiddleware, so keep uploaded parts in
# memory instead of letting Starlette spill files over 1 MB to a temp file on disk.
MultiPartParser.spool_max_size = MAX_REQUEST + 1024 * 1024

VERSION = "0.5"


@asynccontextmanager
async def lifespan(app):
    async def cleanup():
        while True:
            await asyncio.sleep(30)
            purge_reports()
    task = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="PlagIQ Pilot", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
results = {}
REPORT_TTL = 900
MAX_REPORTS = 50
MAX_BYTES = 2 * 1024 * 1024
MAX_CHARS = 300000
MIN_TOKENS = 7
WINDOW = 7
RATE = {}
RATE_LIMIT = 12
active_scans = 0


class ApiError(HTTPException):
    """HTTP error with a stable `code` the browser translates (UZ/RU/EN)."""
    def __init__(self, status, code, message):
        super().__init__(status, message)
        self.code = code


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    code = getattr(exc, "code", None) or {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "request_failed")
    return JSONResponse({"detail": exc.detail, "code": code}, exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # FastAPI's default 422 body echoes submitted values; never reflect manuscript text.
    return JSONResponse({"detail": "Missing or invalid form field. Choose a document and try again.",
                         "code": "invalid_request"}, 422)


def purge_reports():
    now = time.monotonic()
    for key in list(results):
        if results[key]["expires"] <= now:
            del results[key]


def tokens(value, language):
    return corpus.tokens(value, language)


METHOD = "Exact seven-word matching after normalization (verified word by word)"


def _percent(matched, total):
    return round(100 * matched / total, 2) if total else 0


def _source_entry(evidence, **fields):
    evidence = evidence or {}
    return {**fields, "matched_tokens": evidence.get("matched_words", 0),
            "matched_chars": evidence.get("matched_chars", 0),
            "passage_count": evidence.get("passage_count", 0), "passages": evidence.get("passages", [])}


def _measure(q, covered, intervals):
    return {"matched_tokens": len(covered), "total_tokens": len(q),
            "matched_chars": sum(e - s for s, e in intervals), "unit": "words",
            "percent": _percent(len(covered), len(q))}, intervals


def compare(text, source, language):
    """Manuscript vs one user-supplied reference."""
    q, (evidence,), covered, intervals = corpus.compare_texts(text, [source], language)
    result, intervals = _measure(q, covered, intervals)
    result.update({"language": language, "method": METHOD, "ai_detection": "Not performed",
                   "total_chars": len(text), "intervals": intervals,
                   "passages": (evidence or {}).get("passages", []), "evidence": evidence})
    return result


def compare_sources(text, sources, language):
    """Union of verified matches across returned abstracts (metadata-only records are not compared)."""
    q, evidence, covered, intervals = corpus.compare_texts(text, [s.get("abstract", "") for s in sources], language)
    result, intervals = _measure(q, covered, intervals)
    entries = [_source_entry(e, title=s.get("title", "Untitled"), url=s.get("url", ""), year=s.get("year"),
                             coverage=s.get("coverage", "metadata_only")) for s, e in zip(sources, evidence)]
    result.update({"language": language, "method": METHOD + " against returned abstracts",
                   "total_chars": len(text), "intervals": intervals, "sources": entries,
                   "ai_detection": "Not performed"})
    return result


def extract_text(name, content):
    """Extract text from bounded bytes. Runs in a worker thread, not the event loop."""
    try:
        return extract.extract_text(name, content)
    except extract.ExtractError as exc:
        raise ApiError(exc.status, exc.code, str(exc))


async def read_file(upload, kind="document"):
    name = (upload.filename or "").lower()
    if not name.endswith((".txt", ".docx", ".pdf")):
        raise ApiError(400, "unsupported_type", "Only TXT, DOCX and text-based PDF supported")
    content = await upload.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ApiError(413, "file_too_large", "File exceeds 2 MB")
    text = await run_in_threadpool(extract_text, name, content)
    if len(text) > MAX_CHARS:
        raise ApiError(413, "text_too_long", "Extracted text exceeds the 300,000-character pilot limit")
    if not text.strip():
        raise ApiError(400, "no_text", "No readable text detected. Scanned or image-only PDFs need OCR first.")
    return text


def client_key(request):
    """Client address for throttling.

    Behind a reverse proxy (Railway), the TCP peer is the proxy, so every user
    would share one allowance. Set TRUSTED_PROXY_HOPS=1 only when the app is
    reachable exclusively through a proxy that appends the real client address
    to X-Forwarded-For; the rightmost trusted entry cannot be forged by clients.
    """
    try:
        hops = max(0, min(int(os.getenv("TRUSTED_PROXY_HOPS", "0") or 0), 5))
    except ValueError:
        hops = 0
    if hops:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops][:64]
    return request.client.host if request.client else "unknown"


def rate_limit(request):
    # Temporary in-memory safeguard; NOT a substitute for production perimeter limits.
    key = client_key(request)
    now = time.monotonic()
    history = [t for t in RATE.get(key, []) if now-t < 3600]
    if len(history) >= RATE_LIMIT:
        raise ApiError(429, "rate_limited", "Hourly pilot scan allowance reached.")
    history.append(now)
    RATE[key] = history
    if len(RATE) > 3000:
        # Drop expired entries first; only then the oldest keys. Never reset everyone.
        for stale in [k for k, v in RATE.items() if not v or now - v[-1] >= 3600]:
            del RATE[stale]
        while len(RATE) > 3000:
            RATE.pop(next(iter(RATE)))


def scan_capacity():
    try:
        return max(1, min(int(os.getenv("SCAN_CONCURRENCY", "2")), 8))
    except ValueError:
        return 2


def clean_filename(name, fallback):
    name = re.sub(r"[\x00-\x1f\x7f]", "", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return (name or fallback)[:180]


def require_tokens(text, language, code, message):
    count = len(tokens(text, language))
    if count < MIN_TOKENS:
        raise ApiError(400, code, message)
    return count


@app.get("/health")
def health():
    return {"status":"ok","version":VERSION,"corpus_engine_configured":corpus_configured()}


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(Path(__file__).with_name("index.html").read_text("utf-8"), headers={"Cache-Control":"no-store, max-age=0"})


@app.post("/api/check")
async def check(request:Request, submission:UploadFile=File(...),
                reference:UploadFile|None=File(None),reference_text:str=Form(""),
                language:str=Form("uz"),mode:str=Form("reference"),consent:bool=Form(False),
                exclude_quotes:bool=Form(False),exclude_bibliography:bool=Form(False),min_words:int=Form(7)):
    global active_scans
    purge_reports()
    if language not in ("uz","ru","en"):
        raise ApiError(400, "invalid_language", "Invalid language")
    if mode not in ("reference","academic","corpus"):
        raise ApiError(400, "invalid_mode", "Invalid mode")
    if min_words not in citations.MIN_WORD_OPTIONS:
        raise ApiError(400, "invalid_option", "Unsupported minimum match length")
    options = dict(exclude_quotes=exclude_quotes, exclude_bibliography=exclude_bibliography, min_words=min_words)
    if mode == "academic" and not consent:
        raise ApiError(400, "consent_required", "Explicit consent is required before sending a short query excerpt to OpenAlex")
    if mode == "corpus":
        if not corpus_configured():
            raise ApiError(503, "engine_not_configured", "Private PlagIQ corpus engine is not configured.")
        if not consent:
            raise ApiError(400, "consent_required", "Explicit consent is required for private corpus processing.")
    if active_scans >= scan_capacity():
        raise ApiError(503, "server_busy", "PlagIQ is processing other documents. Try again in a minute.")
    rate_limit(request)
    active_scans += 1
    try:
        result = await analyse(submission, reference, reference_text, language, mode, options)
    finally:
        active_scans -= 1
    return finish(request, result)


async def analyse(submission, reference, reference_text, language, mode, options=None):
    original = await read_file(submission)
    require_tokens(original, language, "too_short",
                   "At least 7 words of readable text are needed for seven-word matching.")
    if mode == "reference":
        source = await read_file(reference) if reference and reference.filename else reference_text
        if not source.strip():
            raise ApiError(400, "reference_missing", "Provide a reference document or text, or choose academic discovery")
        if len(source) > MAX_CHARS:
            raise ApiError(413, "reference_too_long", "Reference exceeds pilot limit")
        require_tokens(source, language, "reference_too_short",
                       "The reference needs at least 7 words of readable text.")
        result = await run_in_threadpool(compare, original, source, language)
        same = original.strip() == source.strip()
        name = clean_filename(reference.filename, "Pasted reference") if reference and reference.filename else "Pasted reference"
        evidence = result.pop("evidence")
        result.pop("passages")
        sources = [_source_entry(evidence, title=name, url="", year=None, coverage="reference")] if evidence else []
        result.update({"mode":"reference", "reference":name, "matched_source_count":len(sources),
                       "sources":sources,"database_status":"not_searched","source_count":1,
                       "scope":"One user-provided reference; no public academic databases searched.",
                       "self_comparison":same})
    elif mode == "academic":
        discovery = await run_in_threadpool(discover, original, 8, language)
        result = await run_in_threadpool(compare_sources, original, discovery["sources"], language)
        evaluated = sum(bool(s.get("abstract")) for s in discovery["sources"])
        if not evaluated:
            result["percent"] = None
            result["not_evaluated_reason"] = "provider_unavailable" if discovery["status"] not in ("searched", "partial") else "no_abstracts"
        result["evaluated_sources"] = evaluated
        result["matched_source_count"] = sum(1 for e in result["sources"] if e["matched_tokens"])
        result.update({"mode":"academic","reference":"None","source_count":len(discovery["sources"]),
                       "database_status":discovery["status"],"database_message":discovery["message"],
                       "queries":discovery["queries"],"self_comparison":False,
                       "scope":"OpenAlex metadata discovery, and exact token matching against returned abstracts only. NO full-text publisher corpus or web-wide plagiarism check."})
    else:
        try:
            engine_report = await run_check(original, language)
            summary = summarize(engine_report)
            if summary['total_chars'] != len(original):
                raise EngineError("Engine returned inconsistent document length.")
        except EngineError as exc:
            raise ApiError(503, exc.code, str(exc))
        evaluated = summary["language_counts"].get(language, 0) > 0
        q = await run_in_threadpool(corpus.lexical, original, language)
        matched_words = corpus.words_in_intervals(q, summary["intervals"])
        result = {
            "mode":"corpus", "reference":"PlagIQ authorized private corpus",
            "percent":_percent(matched_words, len(q)) if evaluated else None,
            "not_evaluated_reason": None if evaluated else "no_sources_in_language",
            "matched_tokens":matched_words, "unit":"words",
            "matched_chars":summary["matched_chars"],"total_chars":summary["total_chars"],
            "total_tokens":len(q),"language":language, "corpus_version":summary["corpus_version"],
            "method":METHOD + " against the indexed corpus",
            "database_status":"searched","database_message":"Matched against ingested corpus only, not the whole web.",
            "source_count":summary["corpus_size"],"sources":summary["sources"],
            "matched_source_count":summary["matched_source_count"],
            "ai_detection":"Not performed","self_comparison":False,
            "scope":"Internal indexed corpus only. Coverage depends on lawfully ingested documents; search may be partial.",
            "corpus_coverage":summary["coverage"],
            "language_counts":summary["language_counts"],
            "sources_truncated":summary["sources_truncated"],
            "intervals":summary["intervals"] if evaluated else [],
        }
    intervals = result.pop("intervals", [])
    result["highlight_html"] = ""
    if result.get("percent") is not None:
        review = await run_in_threadpool(citations.assess, original, language, intervals, **(options or {}))
        result["highlight_html"] = highlight(original, review.pop("segments"))
        result["citation"] = review
        result["percent_all"] = review["percent_all"]
        result["percent"] = review["percent_counted"]
        result["matched_tokens_counted"] = review["matched_words_counted"]
    result.update({"document":clean_filename(submission.filename, "Document"),
                   "date":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "quotations_excluded":False, "bibliography_excluded":False,
                   "sha256":hashlib.sha256(original.encode()).hexdigest()})
    return result


MARK_CLASS = {"quotation_cited": "qc", "quotation_uncited": "qu", "bibliography": "bib", "other": "m"}


def highlight(text, segments):
    """Escaped manuscript with matched words wrapped in <mark>; class names only, no user markup."""
    out, pos = [], 0
    for start, end, where, reason in segments:
        if start < pos:
            continue
        out.append(html.escape(text[pos:start]))
        cls = MARK_CLASS[where] + (" x" if reason else "")
        out.append(f'<mark class="{cls}">' + html.escape(text[start:end]) + "</mark>")
        pos = end
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def finish(request, result):
    identifier = uuid.uuid4().hex
    while len(results) >= MAX_REPORTS:
        results.pop(next(iter(results)))
    owner = request.cookies.get("plagiq_session") or secrets.token_urlsafe(32)
    response = JSONResponse({"report_id":identifier, **result})
    # Do not retain manuscript highlights or raw text in the report cache.
    results[identifier] = {"owner": owner, "expires": time.monotonic() + REPORT_TTL,
                           "result": {k:v for k,v in result.items() if k != "highlight_html"}}
    response.set_cookie("plagiq_session", owner, max_age=REPORT_TTL,
                        httponly=True, secure=request.url.scheme == "https" or bool(os.getenv("PUBLIC_ORIGIN", "").startswith("https://")), samesite="strict")
    return response


def owned(identifier, request):
    purge_reports()
    entry = results.get(identifier)
    if entry is None or not secrets.compare_digest(entry['owner'].encode(), request.cookies.get('plagiq_session', '').encode()):
        raise ApiError(404, "report_unavailable", "Report unavailable or expired")
    return entry


@app.get("/api/report/{identifier}.pdf")
def report(identifier:str, request:Request, lang:str="en"):
    entry = owned(identifier, request)
    remaining = max(0, int((entry["expires"] - time.monotonic()) // 60))
    content = build_pdf(entry["result"], identifier, lang if lang in ("uz", "ru", "en") else "en", remaining)
    return Response(content, media_type="application/pdf",
        headers={"Content-Disposition":'attachment; filename="PlagIQ-scope-report.pdf"'})


@app.delete("/api/report/{identifier}")
def delete_report(identifier: str, request: Request):
    owned(identifier, request)
    del results[identifier]
    return {"deleted": True}
