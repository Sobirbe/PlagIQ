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
import fulltext
import websearch
import integrity
import pricing
import verify
import extract
from discovery import discover
import html
from engine_client import EngineError, configured as corpus_configured, library as engine_library, run_check, summarize
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
    q, evidence, covered, intervals = corpus.compare_texts(
        text, [s.get("fulltext") or s.get("abstract", "") for s in sources], language)
    result, intervals = _measure(q, covered, intervals)
    entries = [_source_entry(e, title=s.get("title", "Untitled"), url=s.get("url", ""), year=s.get("year"),
                             coverage="web" if s.get("coverage") == "web" else
                             "fulltext" if s.get("fulltext") else s.get("coverage", "metadata_only"))
               for s, e in zip(sources, evidence)]
    result.update({"language": language, "method": METHOD + " against open-access full texts and abstracts",
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
    if not name.endswith((".txt", ".doc", ".docx", ".pdf")):
        raise ApiError(400, "unsupported_type", "Only TXT, DOC, DOCX and text-based PDF supported")
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


VERIFY_TEXT = {
    "en": dict(title="PlagIQ report verification", ok="Valid: PlagIQ issued a report with these figures.",
               bad="Not valid: this link was changed or was not issued by this PlagIQ server.",
               off="Verification is not set up on this server.", rid="Report ID", date="Checked (UTC)",
               hash="Document SHA-256 begins with", mode="Comparison", result="Matched portion within scope",
               words="Words in the document", sources="Sources compared", lang="Document language",
               modes={"r": "Your own reference", "a": "OpenAlex (open-access full texts and abstracts)", "c": "Private authorized corpus"},
               ne="Not evaluated",
               note="Compare the first 16 characters of the SHA-256 printed in the report with the value above. This page confirms the "
                    "figures of a PlagIQ evidence report; it is not a certificate of originality and keeps no copy of the document."),
    "ru": dict(title="Проверка отчёта PlagIQ", ok="Действителен: PlagIQ выдал отчёт с этими данными.",
               bad="Недействителен: ссылка изменена или выдана не этим сервером PlagIQ.",
               off="Проверка на этом сервере не настроена.", rid="Номер отчёта", date="Проверено (UTC)",
               hash="SHA-256 документа начинается с", mode="Сравнение", result="Доля совпадений в охвате",
               words="Слов в документе", sources="Сравнено источников", lang="Язык документа",
               modes={"r": "Ваш источник", "a": "OpenAlex (открытые полные тексты и аннотации)", "c": "Внутренний разрешённый корпус"},
               ne="Не оценено",
               note="Сравните первые 16 символов SHA-256 из отчёта со значением выше. Страница подтверждает данные отчёта PlagIQ; "
                    "это не сертификат оригинальности, копия документа не хранится."),
    "uz": dict(title="PlagIQ hisobotini tekshirish", ok="Haqiqiy: PlagIQ shu ma’lumotlar bilan hisobot bergan.",
               bad="Haqiqiy emas: havola o‘zgartirilgan yoki bu PlagIQ serveri tomonidan berilmagan.",
               off="Bu serverda tekshirish sozlanmagan.", rid="Hisobot raqami", date="Tekshirilgan (UTC)",
               hash="Hujjat SHA-256 boshlanishi", mode="Solishtirish", result="Doira ichidagi moslik ulushi",
               words="Hujjatdagi so‘zlar", sources="Solishtirilgan manbalar", lang="Hujjat tili",
               modes={"r": "O‘z manbangiz", "a": "OpenAlex (ochiq to‘liq matnlar va annotatsiyalar)", "c": "Ruxsat berilgan ichki korpus"},
               ne="Baholanmagan",
               note="Hisobotdagi SHA-256 ning birinchi 16 belgisini yuqoridagi qiymat bilan solishtiring. Bu sahifa PlagIQ dalillar "
                    "hisobotidagi ma’lumotlarni tasdiqlaydi; originallik sertifikati emas va hujjat nusxasini saqlamaydi."),
}


@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, ui: str = ""):
    lang = ui if ui in VERIFY_TEXT else next((l for l in ("uz", "ru") if l in request.headers.get("accept-language", "")[:5]), "en")
    t = VERIFY_TEXT[lang]
    valid, f = verify.check(request.query_params)
    status = t["off"] if not verify.secret() else (t["ok"] if valid else t["bad"])
    rows = ""
    if valid:
        d = f["d"]
        when = f"{d[0:4]}-{d[4:6]}-{d[6:8]} {d[9:11]}:{d[11:13]}"
        result = t["ne"] if f["p"] == "NE" else f["p"] + "%"
        rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in (
            (t["rid"], f["r"]), (t["date"], when), (t["hash"], f["h"]), (t["mode"], t["modes"][f["m"]]),
            (t["result"], result), (t["words"], f["w"]), (t["sources"], f["n"]), (t["lang"], f["l"].upper())))
    links = " · ".join(f'<a href="?{html.escape(str(request.query_params))}&amp;ui={l}">{l.upper()}</a>' for l in VERIFY_TEXT)
    colour = "#0b6a64" if valid else "#9b2c16"
    page = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(t["title"])}</title><style>body{{font-family:system-ui,sans-serif;margin:0;background:#f7f9f7;color:#122a32}}'
            f'main{{max-width:640px;margin:40px auto;background:#fff;border:1px solid #dce6e5;border-radius:18px;padding:28px}}'
            f'h1{{font-size:22px;margin:0 0 6px}}.st{{font-weight:750;color:{colour};margin:14px 0}}table{{width:100%;border-collapse:collapse;font-size:14px}}'
            f'th,td{{text-align:left;padding:8px;border-bottom:1px solid #e6eeed;vertical-align:top}}th{{color:#46606a;width:45%}}'
            f'p.n{{font-size:13px;color:#51676a;line-height:1.6}}.l{{font-size:13px}}@media(max-width:600px){{main{{margin:0;border-radius:0}}}}</style></head>'
            f'<body><main><div class="l">{links}</div><h1>{html.escape(t["title"])}</h1><p class="st">{html.escape(status)}</p>'
            f'<table>{rows}</table>' + (f'<p class="n">{html.escape(t["note"])}</p>' if valid else '') + '</main></body></html>')
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {"status":"ok","version":VERSION,"corpus_engine_configured":corpus_configured(),
            "web_search":websearch.settings()[0]}


QUOTES = {}
QUOTE_LIMIT = 60


LIBRARY_CACHE = {"at": -1e9, "data": None}


@app.get("/api/library")
async def library_counts():
    """Public, cached summary of the private library: sources per language only."""
    if time.monotonic() - LIBRARY_CACHE["at"] > 60:
        LIBRARY_CACHE.update(at=time.monotonic(), data=await engine_library())
    data = LIBRARY_CACHE["data"]
    return {"configured": corpus_configured(), "available": data is not None, **(data or {})}


@app.get("/api/pricing")
def pricing_config():
    return pricing.public_config()


@app.post("/api/quote")
async def price_quote(request: Request, submission: UploadFile = File(...)):
    """Word count and planned price for a file. Nothing is stored; no check is run."""
    key, now = client_key(request), time.monotonic()
    history = [t for t in QUOTES.get(key, []) if now - t < 3600]
    if len(history) >= QUOTE_LIMIT:
        raise ApiError(429, "rate_limited", "Hourly quote allowance reached.")
    QUOTES[key] = history + [now]
    if len(QUOTES) > 3000:
        QUOTES.clear()
    text = await read_file(submission)
    return {**pricing.quote(pricing.count_words(text)), "characters": len(text),
            "currency": "UZS", "payments_enabled": False}


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(Path(__file__).with_name("index.html").read_text("utf-8"), headers={"Cache-Control":"no-store, max-age=0"})


@app.post("/api/check")
async def check(request:Request, submission:UploadFile=File(...),
                reference:UploadFile|None=File(None),reference_text:str=Form(""),
                language:str=Form("uz"),mode:str=Form("reference"),consent:bool=Form(False),
                exclude_quotes:bool=Form(False),exclude_bibliography:bool=Form(False),min_words:int=Form(7),
                web_search:bool=Form(False)):
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
        result = await analyse(submission, reference, reference_text, language, mode, options, web_search)
    finally:
        active_scans -= 1
    return finish(request, result)


async def analyse(submission, reference, reference_text, language, mode, options=None, web_search=False):
    original = await read_file(submission)
    # The menu defaults to Uzbek; when the text clearly is another language, use that
    # (search filter and normalization depend on it) and say so in the result.
    chosen = language
    detected = await run_in_threadpool(corpus.detect_language, original)
    if detected and detected != chosen:
        language = detected
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
        enabled, max_works, seconds, paid = fulltext.settings()
        fetched = {"attempted": 0, "compared": 0, "failed": 0, "timed_out": 0}
        if enabled and discovery["sources"]:
            # Transient: full texts are compared for this check only and discarded with `discovery`.
            fetched = await run_in_threadpool(fulltext.fetch_fulltexts, discovery["sources"], max_works, seconds, paid)
        web = {"enabled": False}
        if web_search and websearch.settings()[0]:
            # Separate consent: the same excerpts are sent to Yandex; pages are compared once and discarded.
            web_sources, web = await run_in_threadpool(websearch.run, discovery.get("query_texts") or [], language)
            known = {s.get("url") for s in discovery["sources"]}
            discovery["sources"] = discovery["sources"] + [s for s in web_sources if s["url"] not in known]
        result = await run_in_threadpool(compare_sources, original, discovery["sources"], language)
        evaluated = sum(bool(s.get("abstract") or s.get("fulltext")) for s in discovery["sources"])
        result["fulltext"] = fetched
        result["web"] = web
        if not evaluated:
            result["percent"] = None
            result["not_evaluated_reason"] = "provider_unavailable" if discovery["status"] not in ("searched", "partial") else "no_abstracts"
        result["evaluated_sources"] = evaluated
        result["matched_source_count"] = sum(1 for e in result["sources"] if e["matched_tokens"])
        result.update({"mode":"academic","reference":"None","source_count":len(discovery["sources"]),
                       "database_status":discovery["status"],"database_message":discovery["message"],
                       "queries":discovery["queries"],"search_phrases":discovery.get("query_texts", []),
                       "self_comparison":False,
                       "scope":"OpenAlex discovery; exact matching against open-access full texts fetched for this check "
                                "(not stored) where available, otherwise abstracts. "
                                + ("Web pages found with Yandex for the same excerpts were also compared (not stored)."
                                   if web.get("enabled") else "No web-wide plagiarism check.")})
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
    if detected and detected != chosen:
        result["language_note"] = {"selected": chosen, "detected": detected}
    intervals = result.pop("intervals", [])
    result["highlight_html"] = ""
    if result.get("percent") is not None:
        review = await run_in_threadpool(citations.assess, original, language, intervals, **(options or {}))
        result["highlight_html"] = highlight(original, review.pop("segments"))
        result["citation"] = review
        result["percent_all"] = review["percent_all"]
        result["percent"] = review["percent_counted"]
        result["matched_tokens_counted"] = review["matched_words_counted"]
    # Writing-integrity signals (not AI detection). DOIs are looked up only with the academic consent.
    result["integrity"], result["integrity_examples"] = await run_in_threadpool(
        integrity.assess, original, mode == "academic")
    result["plan"] = pricing.quote(pricing.count_words(original))
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


# Manuscript text shown to the user once, never kept in the report cache or PDF.
PRIVATE_FIELDS = ("highlight_html", "search_phrases", "integrity_examples")


def finish(request, result):
    identifier = uuid.uuid4().hex
    while len(results) >= MAX_REPORTS:
        results.pop(next(iter(results)))
    owner = request.cookies.get("plagiq_session") or secrets.token_urlsafe(32)
    response = JSONResponse({"report_id":identifier, **result})
    # Do not retain manuscript highlights or raw text in the report cache.
    results[identifier] = {"owner": owner, "expires": time.monotonic() + REPORT_TTL,
                           "result": {k:v for k,v in result.items() if k not in PRIVATE_FIELDS}}
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
    base = os.getenv("PUBLIC_ORIGIN", "").strip() or str(request.base_url)
    content = build_pdf(entry["result"], identifier, lang if lang in ("uz", "ru", "en") else "en", remaining,
                        verify.link(entry["result"], identifier, base))
    return Response(content, media_type="application/pdf",
        headers={"Content-Disposition":'attachment; filename="PlagIQ-scope-report.pdf"'})


@app.delete("/api/report/{identifier}")
def delete_report(identifier: str, request: Request):
    owned(identifier, request)
    del results[identifier]
    return {"deleted": True}
