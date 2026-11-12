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
import aidetect
import auth
import institutions
import admin
from fastapi.responses import FileResponse, StreamingResponse
import pricing
import payments
import telegram_bot
import verify
import extract
from discovery import discover
import html
from engine_client import EngineError, configured as corpus_configured, library as engine_library, run_check, summarize
import engine_client
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
    if telegram_bot.enabled():                 # let the bot answer /start (TELEGRAM_BOT_REPLIES=1), in the background
        asyncio.get_running_loop().run_in_executor(None, telegram_bot.register)
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
MAX_BYTES = 20 * 1024 * 1024      # dissertations with figures
MAX_CHARS = 1_200_000             # about 150,000 words
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
            "passage_count": evidence.get("passage_count", 0), "passages": evidence.get("passages", []),
            "spans": [list(i) for i in evidence.get("intervals", [])][:2000]}


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
               modes={"i": "AI detector only", "r": "Your own reference", "a": "OpenAlex (open-access full texts and abstracts)", "c": "Private authorized corpus"},
               ne="Not evaluated", ai="AI detector estimate (Pangram): AI + AI-assisted",
               note="Compare the first 16 characters of the SHA-256 printed in the report with the value above. This page confirms the "
                    "figures of a PlagIQ evidence report; it is not a certificate of originality and keeps no copy of the document."),
    "ru": dict(title="Проверка отчёта PlagIQ", ok="Действителен: PlagIQ выдал отчёт с этими данными.",
               bad="Недействителен: ссылка изменена или выдана не этим сервером PlagIQ.",
               off="Проверка на этом сервере не настроена.", rid="Номер отчёта", date="Проверено (UTC)",
               hash="SHA-256 документа начинается с", mode="Сравнение", result="Доля совпадений в охвате",
               words="Слов в документе", sources="Сравнено источников", lang="Язык документа",
               modes={"i": "Только ИИ-детектор", "r": "Ваш источник", "a": "OpenAlex (открытые полные тексты и аннотации)", "c": "Внутренний разрешённый корпус"},
               ne="Не оценено", ai="Оценка ИИ-детектора (Pangram): ИИ + с помощью ИИ",
               note="Сравните первые 16 символов SHA-256 из отчёта со значением выше. Страница подтверждает данные отчёта PlagIQ; "
                    "это не сертификат оригинальности, копия документа не хранится."),
    "uz": dict(title="PlagIQ hisobotini tekshirish", ok="Haqiqiy: PlagIQ shu ma’lumotlar bilan hisobot bergan.",
               bad="Haqiqiy emas: havola o‘zgartirilgan yoki bu PlagIQ serveri tomonidan berilmagan.",
               off="Bu serverda tekshirish sozlanmagan.", rid="Hisobot raqami", date="Tekshirilgan (UTC)",
               hash="Hujjat SHA-256 boshlanishi", mode="Solishtirish", result="Doira ichidagi moslik ulushi",
               words="Hujjatdagi so‘zlar", sources="Solishtirilgan manbalar", lang="Hujjat tili",
               modes={"i": "Faqat AI detektor", "r": "O‘z manbangiz", "a": "OpenAlex (ochiq to‘liq matnlar va annotatsiyalar)", "c": "Ruxsat berilgan ichki korpus"},
               ne="Baholanmagan", ai="AI detektor bahosi (Pangram): AI + AI yordamida",
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
            (t["result"], result), (t["words"], f["w"]), (t["sources"], f["n"]), (t["lang"], f["l"].upper()))
            + (((t["ai"], f["x"] + "%"),) if "x" in f else ()))
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
            "web_search":websearch.settings()[0],"web_providers":websearch.providers(),"web_max_excerpts":websearch.max_queries(),
            "login":auth.public_config(),"ai_detector":aidetect.settings(),"archive":corpus_configured() and auth.enabled(),
            "payments":{"enabled":payments.enabled(),"mode":payments.mode() if payments.enabled() else ""}}


def _secure(request):
    return request.url.scheme == "https" or os.getenv("PUBLIC_ORIGIN", "").startswith("https://")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return HTMLResponse(Path(__file__).with_name("privacy.html").read_text("utf-8"))


@app.get("/login", response_class=HTMLResponse)
def login_page():
    """Telegram returns here with #tgAuthResult=...; the page script finishes sign-in."""
    return home()


@app.post("/auth/telegram")
async def telegram_login(request: Request):
    if not auth.enabled():
        raise ApiError(503, "login_not_configured", "Sign-in is not set up on this server.")
    try:
        data = await request.json()
    except ValueError:
        raise ApiError(400, "login_failed", "Telegram sign-in could not be verified.")
    user = auth.check_telegram(data)
    if not user:
        raise ApiError(401, "login_failed", "Telegram sign-in could not be verified.")
    await run_in_threadpool(auth.remember, user)
    return signed_in_response(request, JSONResponse({"user": user}), user)


def signed_in_response(request, response, user):
    response.set_cookie(auth.COOKIE, auth.issue(user), max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        secure=_secure(request), samesite="strict")
    return response


async def _account_form(request):
    try:
        data = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_request", "Invalid request.")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_request", "Invalid request.")
    return data


@app.post("/auth/register")
async def email_register(request: Request):
    """Email + password account. Nothing is sent to the address and it is not verified."""
    data = await _account_form(request)
    rate_limit(request)                        # the hourly pilot limit also caps account creation
    try:
        user = await run_in_threadpool(auth.register_email, data.get("email"), data.get("password"), data.get("name", ""))
    except auth.AccountError as exc:
        raise ApiError(exc.status, exc.code, str(exc))
    return signed_in_response(request, JSONResponse({"user": user}), user)


@app.post("/auth/login")
async def email_login(request: Request):
    data = await _account_form(request)
    try:
        user = await run_in_threadpool(auth.login_email, data.get("email"), data.get("password"), client_key(request))
    except auth.AccountError as exc:
        raise ApiError(exc.status, exc.code, str(exc))
    return signed_in_response(request, JSONResponse({"user": user}), user)


@app.get("/auth/bot", response_class=HTMLResponse)
async def bot_link(request: Request, t: str = ""):
    """One-time link the Telegram bot sends after the user shared their own phone number."""
    user = await run_in_threadpool(auth.use_login_link, t) if auth.telegram_enabled() else None
    if not user:
        return HTMLResponse('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                            '<p style="font-family:system-ui;padding:20px">This sign-in link has expired or was already used. '
                            'Send /start to the bot again. · Havola eskirgan yoki ishlatilgan: botga qayta /start yuboring. · '
                            'Ссылка устарела или уже использована: отправьте боту /start ещё раз.</p>', 410)
    page = HTMLResponse('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/">'
                        '<p style="font-family:system-ui;padding:20px"><a href="/">PlagIQ</a></p>')
    return signed_in_response(request, page, user)


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not telegram_bot.authorised(request.headers.get("x-telegram-bot-api-secret-token", "")):
        raise ApiError(403, "forbidden", "Forbidden")
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}
    await run_in_threadpool(telegram_bot.handle, update)
    return {"ok": True}


@app.post("/auth/logout")
def logout():
    response = JSONResponse({"signed_out": True})
    response.delete_cookie(auth.COOKIE)
    return response


@app.get("/api/me")
async def me(request: Request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    return {"auth": auth.public_config(), "user": user,
            "history": await run_in_threadpool(auth.history, user) if user else []}


@app.delete("/api/me")
async def delete_me(request: Request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user:
        raise ApiError(401, "login_required", "Sign in with Telegram first.")
    if corpus_configured():                    # the right to erasure covers archived works too
        try:
            for item in await engine_client.list_deposits(auth.owner_id(user)):
                await engine_client.remove_deposit(item["id"], auth.owner_id(user))
        except EngineError as exc:
            raise ApiError(503, exc.code, "Archived works could not be removed; the account was kept. Try again later.")
    await run_in_threadpool(auth.forget, user)
    response = JSONResponse({"deleted": True})
    response.delete_cookie(auth.COOKIE)
    return response


@app.get("/api/archive")
async def my_archive(request: Request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user:
        raise ApiError(401, "login_required", "Sign in with Telegram first.")
    if not corpus_configured():
        return {"enabled": False, "items": []}
    try:
        return {"enabled": True, "items": await engine_client.list_deposits(auth.owner_id(user))}
    except EngineError as exc:
        raise ApiError(503, exc.code, str(exc))


@app.delete("/api/archive/{identifier}")
async def remove_from_archive(identifier: str, request: Request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user:
        raise ApiError(401, "login_required", "Sign in with Telegram first.")
    if not re.fullmatch(r"[0-9a-f]{64}", identifier) or not corpus_configured():
        raise ApiError(404, "report_unavailable", "Not found")
    try:
        removed = await engine_client.remove_deposit(identifier, auth.owner_id(user))
    except EngineError as exc:
        raise ApiError(503, exc.code, str(exc))
    if not removed:
        raise ApiError(404, "report_unavailable", "Not found")
    return {"deleted": True}


@app.get("/api/institution")
def my_institutions(request: Request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    return {"enabled": corpus_configured(), "institutions": institutions.for_user(user)}


@app.post("/api/institution/deposit")
async def institution_deposit(request: Request, institution: str = Form(..., max_length=80), file: UploadFile = File(...),
                              year: int = Form(...), authors: str = Form("", max_length=300), title: str = Form("", max_length=300)):
    """One work per request (the browser sends files one by one; each request is capped at 45 MB)."""
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not institutions.allowed(user, institution):
        raise ApiError(403, "institution_forbidden", "This account may not deposit works for this institution.")
    if not corpus_configured():
        raise ApiError(503, "engine_not_configured", "Private PlagIQ corpus engine is not configured.")
    if not 1900 <= year <= datetime.now(timezone.utc).year:
        raise ApiError(400, "invalid_option", "Invalid year")
    text = await read_file(file)
    language = await run_in_threadpool(corpus.detect_language, text)
    if not language:
        raise ApiError(400, "language_unknown", "The language of this file could not be detected (Uzbek, Russian or English).")
    require_tokens(text, language, "too_short", "At least 7 words of readable text are needed.")
    name_author, name_title = institutions.from_filename(file.filename)
    item = {**institutions.record(institution, institutions.configured()[institution]["agreement"],
                                  " ".join(title.split()) or name_title, " ".join(authors.split()) or name_author or "—",
                                  year, language), "text": text}
    try:
        identifier = await engine_client.deposit(item)
    except EngineError as exc:
        raise ApiError(422 if exc.code == "archive_refused" else 503, exc.code, str(exc))
    return {"id": identifier, "title": item["title"], "authors": item["authors"], "language": language,
            "words": pricing.count_words(text)}


@app.get("/api/institution/archive")
async def institution_archive(request: Request, institution: str):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not institutions.allowed(user, institution):
        raise ApiError(403, "institution_forbidden", "This account may not manage this institution's works.")
    try:
        return {"items": await engine_client.list_deposits(institutions.owner_id(institution))}
    except EngineError as exc:
        raise ApiError(503, exc.code, str(exc))


@app.delete("/api/institution/archive/{identifier}")
async def institution_remove(identifier: str, request: Request, institution: str):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not institutions.allowed(user, institution):
        raise ApiError(403, "institution_forbidden", "This account may not manage this institution's works.")
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ApiError(404, "report_unavailable", "Not found")
    try:
        removed = await engine_client.remove_deposit(identifier, institutions.owner_id(institution))
    except EngineError as exc:
        raise ApiError(503, exc.code, str(exc))
    if not removed:
        raise ApiError(404, "report_unavailable", "Not found")
    return {"deleted": True}


QUOTES = {}
QUOTE_LIMIT = 60


LIBRARY_CACHE = {"at": -1e9, "data": None}


@app.get("/api/library")
async def library_counts():
    """Public, cached summary of the private library: sources per language only."""
    if time.monotonic() - LIBRARY_CACHE["at"] > 60:
        LIBRARY_CACHE.update(at=time.monotonic(), data=await engine_library())
    data = LIBRARY_CACHE["data"]
    public = {k: v for k, v in (data or {}).items() if k not in ("disk", "imports")}    # owner-only details
    return {"configured": corpus_configured(), "available": data is not None, **public}


def owner(request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not auth.is_owner(user):
        raise ApiError(404, "report_unavailable", "Not found")       # the page does not exist for others
    return user


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    owner(request)
    data = await engine_library() if corpus_configured() else None
    reason = await engine_client.probe() if corpus_configured() and data is None else ""
    return HTMLResponse(admin.page({"available": data is not None, **(data or {})}, reason),
                        headers={"Cache-Control": "no-store"})


@app.get("/admin/backup/archive.jsonl.gz")
async def admin_backup_archive(request: Request):
    owner(request)
    if not corpus_configured():
        raise ApiError(503, "engine_not_configured", "Private PlagIQ corpus engine is not configured.")
    try:
        body = await engine_client.export_archive()
    except EngineError as exc:
        raise ApiError(503, exc.code, str(exc))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(body, media_type="application/gzip",
                             headers={"Content-Disposition": f'attachment; filename="plagiq-archive-{stamp}.jsonl.gz"'})


@app.get("/admin/backup/accounts.sqlite3")
def admin_backup_accounts(request: Request):
    owner(request)
    path = auth.db_file()
    if not os.path.exists(path):
        raise ApiError(404, "report_unavailable", "No accounts database yet")
    import sqlite3, tempfile
    copy = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False).name
    with sqlite3.connect(path) as source, sqlite3.connect(copy) as target:
        source.backup(target)                       # consistent copy while the site is running
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    from starlette.background import BackgroundTask
    return FileResponse(copy, media_type="application/vnd.sqlite3", filename=f"plagiq-accounts-{stamp}.sqlite3",
                        background=BackgroundTask(os.unlink, copy))


@app.get("/api/pricing")
def pricing_config():
    config = pricing.public_config()
    if payments.enabled():
        config.update(payments_enabled=True, status=payments.mode())
    return config


# ------------------------------------------------------------- payments (test mode only)

def signed_in(request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user:
        raise ApiError(401, "login_required", "Sign in with Telegram first.")
    return user


@app.get("/api/payments")
async def payments_overview(request: Request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    config = payments.public_config()
    if user and config["enabled"]:
        config["account"] = await run_in_threadpool(payments.account, user)
    return config


@app.post("/api/payments/order")
async def payments_order(request: Request):
    user = signed_in(request)
    try:
        data = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_request", "Invalid request.")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_request", "Invalid request.")
    try:
        return await run_in_threadpool(payments.create_order, user, str(data.get("provider", "")),
                                       data.get("amount_uzs"), str(data["package"]) if data.get("package") else None)
    except payments.PaymentError as exc:
        raise ApiError(400 if exc.code == "invalid_amount" else 503, exc.code, str(exc))


@app.get("/api/payments/order/{order_id}")
async def payments_order_status(order_id: str, request: Request):
    user = signed_in(request)
    row = await run_in_threadpool(payments.order, user, order_id)
    if not row:
        raise ApiError(404, "not_found", "Order not found.")
    row.pop("provider_ref", None)
    return row


@app.get("/payments/receipt/{order_id}", response_class=HTMLResponse)
async def payments_receipt(order_id: str, request: Request):
    user = signed_in(request)
    row = await run_in_threadpool(payments.order, user, order_id)
    if not row or row["state"] not in ("paid", "refunded"):
        raise ApiError(404, "not_found", "No paid order with this number.")
    return HTMLResponse(payments_receipt_html(row, user))


def payments_receipt_html(row, user):
    esc = html.escape
    when = datetime.fromtimestamp(row["paid"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    seller = os.environ.get("SELLER_NAME", "PlagIQ")
    tin = os.environ.get("SELLER_TIN", "")
    test = row["mode"] == "test"
    lines = [("Order / Buyurtma / Заказ", row["id"]), ("Date / Sana / Дата", when),
             ("Customer / Mijoz / Клиент", user["name"]),
             ("Paid with / To‘lov / Оплата", {"payme": "Payme", "click": "Click"}.get(row["provider"], row["provider"])),
             ("Paid / To‘langan / Оплачено", f"{row['pay_uzs']:,} so'm".replace(",", " ")),
             ("Added to balance / Balansga / На баланс", f"{row['credit_uzs']:,} so'm".replace(",", " ")),
             ("Provider reference", row.get("provider_ref") or "—"),
             ("Status", "refunded / qaytarildi / возвращено" if row["state"] == "refunded" else "paid / to‘langan / оплачено")]
    table = "".join(f"<tr><th>{esc(a)}</th><td>{esc(str(b))}</td></tr>" for a, b in lines)
    banner = ('<p class="test">TEST PAYMENT — no real money was taken. Sinov to‘lovi — haqiqiy pul olinmadi. '
              'Тестовый платёж — реальные деньги не списаны.</p>') if test else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>PlagIQ receipt {esc(row["id"])}</title><style>body{{font-family:system-ui,sans-serif;background:#f7f9f7;color:#122a32;margin:0}}'
            f'main{{max-width:620px;margin:24px auto;background:#fff;border:1px solid #dce6e5;border-radius:14px;padding:22px}}'
            f'table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;padding:8px;border-bottom:1px solid #e6eeed}}'
            f'.test{{background:#fff3cd;color:#7a4a00;padding:10px;border-radius:8px;font-weight:700}}small{{color:#51676a}}'
            f'@media print{{button{{display:none}}}}</style></head><body><main><h1>{esc(seller)} — receipt</h1>'
            + (f"<p>TIN / STIR / ИНН: {esc(tin)}</p>" if tin else "") + banner +
            f'<table>{table}</table><p><small>Balance top-up for PlagIQ checks. This page is a payment confirmation, '
            f'not a fiscal receipt; the fiscal receipt, when issued, comes from the payment provider.</small></p>'
            f'<button onclick="print()">Print / Chop etish / Печать</button></main></body></html>')


@app.post("/payments/payme")
async def payme_endpoint(request: Request):
    # Payme expects HTTP 200 with a JSON-RPC body in every case, errors included.
    body = await request.body()
    reply = await run_in_threadpool(payments.payme, body, request.headers.get("authorization", ""))
    return JSONResponse(reply, 200)


@app.post("/payments/click/{stage}")
async def click_endpoint(stage: str, request: Request):
    if stage not in ("prepare", "complete"):
        raise ApiError(404, "not_found", "Not found")
    form = await request.form()
    reply = await run_in_threadpool(payments.click, {k: v for k, v in form.items() if isinstance(v, str)}, stage)
    return JSONResponse(reply, 200)


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
    words = pricing.count_words(text)
    return {**pricing.quote(words), "ai_price_uzs": pricing.ai_price(words), "characters": len(text),
            "currency": "UZS", "payments_enabled": False}


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(Path(__file__).with_name("index.html").read_text("utf-8"), headers={"Cache-Control":"no-store, max-age=0"})


@app.post("/api/check")
async def check(request:Request, submission:UploadFile=File(...),
                reference:UploadFile|None=File(None),reference_text:str=Form(""),
                language:str=Form("uz"),mode:str=Form("reference"),consent:bool=Form(False),
                exclude_quotes:bool=Form(False),exclude_bibliography:bool=Form(False),min_words:int=Form(7),
                web_search:bool=Form(False),ai_check:bool=Form(False),ai_consent:bool=Form(False),
                archive:bool=Form(False),archive_consent:bool=Form(False),archive_title:str=Form("",max_length=300),
                archive_author:str=Form("",max_length=300)):
    global active_scans
    purge_reports()
    user = auth.read(request.cookies.get(auth.COOKIE))
    anonymous = auth.required() and not user
    if anonymous and (not auth.anon_max_words() or mode == "ai" or ai_check or archive):
        raise ApiError(401, "login_required", "Sign in to run this check.")
    if language not in ("uz","ru","en"):
        raise ApiError(400, "invalid_language", "Invalid language")
    if mode not in ("reference","academic","corpus","ai"):
        raise ApiError(400, "invalid_mode", "Invalid mode")
    if min_words not in citations.MIN_WORD_OPTIONS:
        raise ApiError(400, "invalid_option", "Unsupported minimum match length")
    options = dict(exclude_quotes=exclude_quotes, exclude_bibliography=exclude_bibliography, min_words=min_words)
    ai_check = ai_check or mode == "ai"
    if ai_check and not aidetect.configured():
        raise ApiError(503, "ai_not_configured", "The AI detector is not set up on this server.")
    deposit = None
    if archive:
        if not user:
            raise ApiError(401, "login_required", "Sign in with Telegram to add a work to the archive.")
        if not corpus_configured():
            raise ApiError(503, "engine_not_configured", "Private PlagIQ corpus engine is not configured.")
        if not archive_consent:
            raise ApiError(400, "archive_consent_required", "Consent is required to keep the text in the protected archive.")
        deposit = {"title": archive_title, "authors": archive_author or user["name"]}
    if ai_check and not ai_consent:
        raise ApiError(400, "ai_consent_required", "Consent is required before the full text is sent to the AI detector.")
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
    original, price, charge_id = None, 0, None
    if anonymous:                              # short texts can be checked without an account
        original = await read_file(submission)
        if pricing.count_words(original) > auth.anon_max_words():
            raise ApiError(401, "login_required_long",
                           f"Texts over {auth.anon_max_words()} words need sign-in. Sign in and check again.")
    if payments.enabled():
        # The price is known from the word count before anything is checked; it is taken from the
        # balance first and given back if the check fails.
        if original is None:
            original = await read_file(submission)
        words = pricing.count_words(original)
        price = payments.price_for(mode, words, ai_check)
        if price is None:
            raise ApiError(413, "too_long_for_plans", "The document is larger than the biggest plan. Contact us.")
        if not user and price > 0:             # only free checks without an account
            raise ApiError(401, "login_required_long", "Sign in to pay for this check.")
        try:
            charge_id = await run_in_threadpool(payments.charge, user, price, f"{mode} check, {words} words"
                                                + (", AI detector" if ai_check else ""))
        except payments.PaymentError as exc:
            raise ApiError(402, exc.code, str(exc))
    active_scans += 1
    try:
        result = await analyse(submission, reference, reference_text, language, mode, options, web_search, ai_check,
                               auth.owner_id(user), deposit, original)
    except BaseException:
        if charge_id:
            await run_in_threadpool(payments.refund, user, charge_id, price, "check failed")
        raise
    finally:
        active_scans -= 1
    if charge_id:
        unused = unused_ai_price(result, words, ai_check)
        if unused:
            await run_in_threadpool(payments.refund, user, charge_id, unused, "AI detector did not run")
        result["charged_uzs"] = price - unused
    response = finish(request, result)
    if user:
        await run_in_threadpool(auth.log_check, user, result, response.headers.get("x-report-id", ""))
    if charge_id:
        await run_in_threadpool(payments.attach_report, charge_id, response.headers.get("x-report-id", ""))
    return response


def unused_ai_price(result, words, ai_check):
    """The AI-detector part of a price when the detector gave no estimate (Uzbek, too long, error)."""
    if not ai_check or (result.get("ai") or {}).get("status") == "done":
        return 0
    return pricing.ai_price(words)


async def search_library(result, original, language, owner):
    """Add matches from PlagIQ's private library (imported sources and the protected archive)
    to an academic check. Errors never fail the check; they are reported."""
    if not corpus_configured():
        return {"searched": False}
    try:
        summary = summarize(await run_check(original, language, [hashlib.sha256(original.encode()).hexdigest()]))
        if summary["total_chars"] != len(original):
            raise EngineError("Engine returned inconsistent document length.")
    except EngineError as exc:
        return {"searched": False, "error": exc.code}
    for source in summary["sources"]:
        mark_own(source, owner)
    result["sources"] = summary["sources"] + result["sources"]
    intervals = corpus.merge([list(i) for i in result.get("intervals", [])] + [list(i) for i in summary["intervals"]])
    q = corpus.lexical(original, language)
    matched = corpus.words_in_intervals(q, intervals)
    result.update(intervals=intervals, matched_tokens=matched, matched_chars=sum(e - s for s, e in intervals))
    # The check counts as evaluated if either the OpenAlex texts or the library had text in this language.
    if result.get("percent") is not None or summary["language_counts"].get(language, 0):
        result["percent"] = _percent(matched, len(q))
        result.pop("not_evaluated_reason", None)
    return {"searched": True, "size": summary["corpus_size"],
            "matched_sources": sum(1 for s in summary["sources"] if s["matched_tokens"])}


async def deposit_work(original, language, owner, archive, filename):
    """Put the checked text into the protected archive (author consent given at check time)."""
    title = " ".join((archive.get("title") or clean_filename(filename, "Document").rsplit(".", 1)[0]).split())[:300]
    item = {"text": original, "language": language, "title": title or "Document",
            "authors": " ".join((archive.get("authors") or "").split())[:300] or "—",
            "year": datetime.now(timezone.utc).year, "owner": owner, "rights_basis": "author_deposit",
            "permission_reference": "author consent at check time " + datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "collection": "user"}
    try:
        identifier = await engine_client.deposit(item)
    except EngineError as exc:
        return {"deposited": False, "error": exc.code}
    return {"deposited": True, "id": identifier[:12], "title": item["title"]}


def mark_own(source, owner):
    """A user's own deposited work is labelled as such (self-citation), not hidden."""
    if source.get("coverage") == "archive" and owner and source.pop("owner", "") == owner:
        source["coverage"] = "own_archive"
    source.pop("owner", None)


async def analyse(submission, reference, reference_text, language, mode, options=None, web_search=False, ai_check=False,
                  owner="", archive=None, original=None):
    if original is None:
        original = await read_file(submission)
    # The menu defaults to Uzbek; when the text clearly is another language, use that
    # (search filter and normalization depend on it) and say so in the result.
    chosen = language
    detected = await run_in_threadpool(corpus.detect_language, original)
    if detected and detected != chosen:
        language = detected
    require_tokens(original, language, "too_short",
                   "At least 7 words of readable text are needed for seven-word matching.")
    if mode == "ai":
        result = {"mode": "ai", "percent": None, "not_evaluated_reason": "ai_only", "matched_tokens": 0,
                  "total_tokens": len(corpus.lexical(original, language)), "matched_chars": 0, "total_chars": len(original),
                  "unit": "words", "language": language, "sources": [], "source_count": 0, "matched_source_count": 0,
                  "database_status": "not_searched", "reference": "None", "self_comparison": False,
                  "method": "AI-writing detector (Pangram)", "ai_detection": "Pangram estimate",
                  "scope": "AI-writing detector only; no plagiarism comparison was made."}
    elif mode == "reference":
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
            # Separate consent: the same excerpts are sent to the search providers; pages are compared once and discarded.
            from discovery import spread_excerpts
            excerpts = spread_excerpts(original, websearch.excerpt_count(pricing.count_words(original)))
            web_sources, web = await run_in_threadpool(websearch.run, excerpts, language)
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
        result["library"] = await search_library(result, original, language, owner)
        result["matched_source_count"] = sum(1 for e in result["sources"] if e["matched_tokens"])
        result.update({"mode":"academic","reference":"None","source_count":len(discovery["sources"]),
                       "database_status":discovery["status"],"database_message":discovery["message"],
                       "queries":discovery["queries"],"search_phrases":discovery.get("query_texts", []),
                       "self_comparison":False,
                       "scope":"OpenAlex discovery; exact matching against open-access full texts fetched for this check "
                                "(not stored) where available, otherwise abstracts. "
                                + (f"Web pages found with {' and '.join(web.get('providers') or ['web search'])} for the same excerpts were also compared (not stored)."
                                   if web.get("enabled") else "No web-wide plagiarism check.")})
    else:
        try:
            engine_report = await run_check(original, language, [hashlib.sha256(original.encode()).hexdigest()])
            summary = summarize(engine_report)
            if summary['total_chars'] != len(original):
                raise EngineError("Engine returned inconsistent document length.")
        except EngineError as exc:
            raise ApiError(503, exc.code, str(exc))
        for source in summary["sources"]:
            mark_own(source, owner)
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
        result["highlight_html"] = highlight(original, review.pop("segments"), result.get("sources"))
        result["citation"] = review
        result["percent_all"] = review["percent_all"]
        result["percent"] = review["percent_counted"]
        result["matched_tokens_counted"] = review["matched_words_counted"]
    # Writing-integrity signals (not AI detection). DOIs are looked up only with the academic consent.
    result["integrity"], result["integrity_examples"] = await run_in_threadpool(
        integrity.assess, original, mode == "academic")
    words = pricing.count_words(original)
    if archive:
        result["archive"] = await deposit_work(original, language, owner, archive, submission.filename)
    if ai_check:
        # Separate consent: the full text goes to Pangram; flagged passages stay out of the cache and PDF.
        result["ai"], result["ai_passages"] = await run_in_threadpool(aidetect.detect, original, language, words)
    for source in result.get("sources") or []:
        for passage in source.get("passages") or []:
            qs, qe = passage.get("query_start"), passage.get("query_end")
            if type(qs) is int and type(qe) is int and 0 <= qs < qe <= len(original):
                passage["query_excerpt"] = original[qs:qe][:600] + ("…" if qe - qs > 600 else "")
    result["plan"] = pricing.quote(words)
    result.update({"document":clean_filename(submission.filename, "Document"),
                   "date":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "quotations_excluded":False, "bibliography_excluded":False,
                   "sha256":hashlib.sha256(original.encode()).hexdigest()})
    return result


MARK_CLASS = {"quotation_cited": "qc", "quotation_uncited": "qu", "bibliography": "bib", "other": "m"}


SOURCE_COLOURS = 8          # the top sources get their own colour; the rest share one


def source_ranks(sources):
    """Source list order used on the page (most matched words first) -> [(rank, spans)]."""
    order = sorted(range(len(sources or [])), key=lambda i: -(sources[i].get("matched_tokens") or 0))
    ranked = []
    for rank, i in enumerate(order):
        src = sources[i]
        spans = src.get("spans") or [[p["query_start"], p["query_end"]] for p in src.get("passages") or []]
        if src.get("matched_tokens"):
            ranked.append((rank, spans))
    return ranked


def highlight(text, segments, sources=None):
    """Escaped manuscript with matched words wrapped in <mark>; class names and a numeric
    data-s (source rank) only, no user markup."""
    ranked = source_ranks(sources)
    out, pos = [], 0
    for start, end, where, reason in segments:
        if start < pos:
            continue
        out.append(html.escape(text[pos:start]))
        cls = MARK_CLASS[where] + (" x" if reason else "")
        rank = next((r for r, spans in ranked if any(s < end and start < e for s, e in spans)), None)
        attr = f' data-s="{min(rank, SOURCE_COLOURS)}"' if rank is not None else ""
        out.append(f'<mark class="{cls}"{attr}>' + html.escape(text[start:end]) + "</mark>")
        pos = end
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


# Manuscript text shown to the user once, never kept in the report cache or PDF.
PRIVATE_FIELDS = ("highlight_html", "search_phrases", "integrity_examples", "ai_passages")


def finish(request, result):
    identifier = uuid.uuid4().hex
    while len(results) >= MAX_REPORTS:
        results.pop(next(iter(results)))
    owner = request.cookies.get("plagiq_session") or secrets.token_urlsafe(32)
    response = JSONResponse({"report_id":identifier, **result}, headers={"x-report-id": identifier})
    # Do not retain manuscript highlights or raw text in the report cache.
    cached = {k:v for k,v in result.items() if k not in PRIVATE_FIELDS}
    if cached.get("sources"):           # manuscript excerpts of the side-by-side view are never cached
        cached["sources"] = [{**s, "passages": [{k: v for k, v in p.items() if k != "query_excerpt"}
                                                for p in s.get("passages") or []]} for s in cached["sources"]]
    results[identifier] = {"owner": owner, "expires": time.monotonic() + REPORT_TTL, "result": cached}
    response.set_cookie("plagiq_session", owner, max_age=REPORT_TTL,
                        httponly=True, secure=_secure(request), samesite="strict")
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
