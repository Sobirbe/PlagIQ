from urllib.parse import urlencode
import asyncio
from contextlib import asynccontextmanager
import hashlib
import math
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
import uzai
import auth
import institutions
import admin
import alerts
from fastapi.responses import FileResponse, StreamingResponse
import pricing
import payments
import entitlements
import pending_checks
import telegram_bot
import verify
import timefmt
import extract
from discovery import discover
import html
from engine_client import EngineError, configured as corpus_configured, library as engine_library, run_check, summarize
import engine_client
from report_pdf import build_certificate_pdf, build_pdf, build_short_pdf
from security import SecurityMiddleware, MAX_REQUEST
import checkstatus
import io
import csv
import sqlite3
import logging
import progress
import operator_info
import sample_report
import mailer
import reports_store
import sandbox
import uzcyr
import templates
import metrics
import notify

# The whole request is capped by SecurityMiddleware, so keep uploaded parts in
# memory instead of letting Starlette spill files over 1 MB to a temp file on disk.
MultiPartParser.spool_max_size = MAX_REQUEST + 1024 * 1024

VERSION = "0.5"


@asynccontextmanager
async def lifespan(app):
    async def cleanup():
        rounds = 0
        while True:
            await asyncio.sleep(30)
            purge_reports()
            rounds += 1
            try:
                await run_in_threadpool(pending_checks.purge)
                await run_paid_checks()                  # checks whose payment the provider has confirmed
            except sqlite3.Error as exc:
                logging.getLogger("plagiq.check").error("paid checks not started: %s", type(exc).__name__)
            if rounds % 10 == 0:                   # every 5 minutes: owner alerts (alerts.py)
                await alerts.watch_round()
            if rounds % 10 == 0:                   # every 5 minutes: delete reports older than REPORT_HOURS
                try:
                    await run_in_threadpool(reports_store.purge)
                    await run_in_threadpool(entitlements.purge)      # fingerprints after the 30-day recheck window
                except sqlite3.Error as exc:
                    logging.getLogger("plagiq.check").error("report purge failed: %s", type(exc).__name__)
    await run_in_threadpool(recover_interrupted)
    try:
        await run_in_threadpool(entitlements.release_interrupted)   # free trials / rechecks of cut-off checks
    except sqlite3.Error as exc:
        logging.getLogger("plagiq.check").error("entitlements unavailable at start-up: %s", type(exc).__name__)
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

def recover_interrupted():
    """Checks cut off by a restart are marked failed ("interrupted") and their charge is given back."""
    try:
        for user_id, charge_id, price in reports_store.interrupted():
            payments.refund({"id": user_id}, charge_id, price, "check interrupted by a restart")
        pending_checks.interrupted()           # paid checks cut off by the restart run again (their charge was returned)
        reports_store.purge()
    except sqlite3.Error as exc:
        logging.getLogger("plagiq.check").error("report store unavailable at start-up: %s", type(exc).__name__)


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
_waiting = []                                  # checks waiting for a slot, in arrival order (see _wait_for_slot)


class ApiError(HTTPException):
    """HTTP error with a stable `code` the browser translates (UZ/RU/EN)."""
    def __init__(self, status, code, message):
        super().__init__(status, message)
        self.code = code


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    code = getattr(exc, "code", None) or {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "request_failed")
    status = checkstatus.error_status(code, exc.status_code)
    if status in ("failed_search", "failed_internal"):
        logging.getLogger("plagiq.check").warning("%s %s: %s", request.url.path, status, code)
    return JSONResponse({"detail": exc.detail, "code": code, "status": status}, exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def internal_error(request, exc):
    # Logged with the stack trace for the operator; the manuscript text is never part of the message.
    logging.getLogger("plagiq.check").error("%s failed_internal: %s", request.url.path, type(exc).__name__, exc_info=exc)
    return JSONResponse({"detail": "PlagIQ could not finish this check because of an internal error. Any charge for it was returned; "
                                   "try again.", "code": "internal_error", "status": "failed_internal"}, 500)


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


METHOD = ("Exact seven-word matching after normalization (verified word by word), and edited copies "
          "(words replaced, inserted or deleted; verified by word alignment)")


def _percent(matched, total):
    return round(100 * matched / total, 2) if total else 0


def _source_entry(evidence, **fields):
    evidence = evidence or {}
    return {**fields, "matched_tokens": evidence.get("matched_words", 0), "edited_tokens": evidence.get("edited_words", 0),
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
                   "exact_intervals": (evidence or {}).get("exact_intervals", []),
                   "passages": (evidence or {}).get("passages", []), "evidence": evidence})
    return result


def compare_sources(text, sources, language):
    """Union of verified matches across returned abstracts (metadata-only records are not compared)."""
    q, evidence, covered, intervals = corpus.compare_texts(
        text, [s.get("fulltext") or s.get("abstract", "") for s in sources], language)
    result, intervals = _measure(q, covered, intervals)
    import discovery as finder
    entries = [_source_entry(e, title=s.get("title", "Untitled"), url=s.get("url", ""), year=s.get("year"),
                             off_topic=finder.off_topic(s),
                             coverage="web" if s.get("coverage") == "web" else
                             "fulltext" if s.get("fulltext") else s.get("coverage", "metadata_only"))
               for s, e in zip(sources, evidence)]
    result.update({"language": language, "method": METHOD + " against open-access full texts and abstracts",
                   "total_chars": len(text), "intervals": intervals, "sources": entries,
                   "exact_intervals": corpus.merge([list(i) for e in evidence if e for i in e["exact_intervals"]]),
                   "ai_detection": "Not performed"})
    return result


def extract_text(name, content, info=None):
    """Extract text from bounded bytes (scanned PDF pages by OCR). Runs in a worker thread, not the event loop."""
    try:
        return sandbox.extract_text(name, content, ocr=True, info=info)      # separate, limited process
    except extract.ExtractError as exc:
        raise ApiError(exc.status, exc.code, str(exc))


async def read_file(upload, kind="document"):
    name = (upload.filename or "").lower()
    if not name.endswith((".txt", ".doc", ".docx", ".pdf")):
        raise ApiError(400, "unsupported_type", "Only TXT, DOC, DOCX and text-based PDF supported")
    content = await upload.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ApiError(413, "file_too_large", f"File exceeds {MAX_BYTES // (1024 * 1024)} MB")
    progress.step("read")
    info = {"progress": progress.callback()}
    text = await run_in_threadpool(extract_text, name, content, info)
    info.pop("progress", None)
    upload.ocr_info = info                      # OCR pages for the result; the PDF's stored title for the search
    if len(text) > MAX_CHARS:
        raise ApiError(413, "text_too_long", f"Extracted text exceeds the {MAX_CHARS:,}-character limit")
    if not text.strip():
        raise ApiError(400, "no_text", "No readable text detected." + ("" if extract.ocr_available() else
                       " Scanned or image-only PDFs need OCR, which is not available on this server."))
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


def hourly_limit(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def rate_limit(request, user=None, purpose="check"):
    """Hourly allowance (in memory; NOT a substitute for production perimeter limits).
    Signed-in users are counted per account (CHECKS_PER_HOUR_ACCOUNT, default 30), so students behind one
    university address do not share an allowance; others per address (CHECKS_PER_HOUR, default 12)."""
    if user:
        key, limit = f"{purpose}:user:{user['id']}", hourly_limit("CHECKS_PER_HOUR_ACCOUNT", 30)
    else:
        key, limit = f"{purpose}:ip:{client_key(request)}", hourly_limit("CHECKS_PER_HOUR", RATE_LIMIT)
    now = time.monotonic()
    history = [t for t in RATE.get(key, []) if now-t < 3600]
    if len(history) >= limit:
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
               off="Verification is not set up on this server.", rid="Report ID", date="Checked",
               hash="Document SHA-256 begins with", mode="Comparison", result="Matched portion within scope",
               words="Words in the document", sources="Sources compared", lang="Document language",
               modes={"i": "AI detector only", "r": "Your own reference", "a": "OpenAlex (open-access full texts and abstracts)", "c": "Private authorized corpus"},
               ne="Not evaluated", ai="AI detector estimate (Pangram): AI + AI-assisted", ex="Sources excluded by the report owner",
               note="Compare the first 16 characters of the SHA-256 printed in the report with the value above. This page confirms the "
                    "figures of a PlagIQ evidence report; it is not a certificate of originality and keeps no copy of the document."),
    "ru": dict(title="Проверка отчёта PlagIQ", ok="Действителен: PlagIQ выдал отчёт с этими данными.",
               bad="Недействителен: ссылка изменена или выдана не этим сервером PlagIQ.",
               off="Проверка на этом сервере не настроена.", rid="Номер отчёта", date="Проверено",
               hash="SHA-256 документа начинается с", mode="Сравнение", result="Доля совпадений в охвате",
               words="Слов в документе", sources="Сравнено источников", lang="Язык документа",
               modes={"i": "Только ИИ-детектор", "r": "Ваш источник", "a": "OpenAlex (открытые полные тексты и аннотации)", "c": "Внутренний разрешённый корпус"},
               ne="Не оценено", ai="Оценка ИИ-детектора (Pangram): ИИ + с помощью ИИ", ex="Источников исключено владельцем отчёта",
               note="Сравните первые 16 символов SHA-256 из отчёта со значением выше. Страница подтверждает данные отчёта PlagIQ; "
                    "это не сертификат оригинальности, копия документа не хранится."),
    "uz": dict(title="PlagIQ hisobotini tekshirish", ok="Haqiqiy: PlagIQ shu ma’lumotlar bilan hisobot bergan.",
               bad="Haqiqiy emas: havola o‘zgartirilgan yoki bu PlagIQ serveri tomonidan berilmagan.",
               off="Bu serverda tekshirish sozlanmagan.", rid="Hisobot raqami", date="Tekshirilgan",
               hash="Hujjat SHA-256 boshlanishi", mode="Solishtirish", result="Doira ichidagi moslik ulushi",
               words="Hujjatdagi so‘zlar", sources="Solishtirilgan manbalar", lang="Hujjat tili",
               modes={"i": "Faqat AI detektor", "r": "O‘z manbangiz", "a": "OpenAlex (ochiq to‘liq matnlar va annotatsiyalar)", "c": "Ruxsat berilgan ichki korpus"},
               ne="Baholanmagan", ai="AI detektor bahosi (Pangram): AI + AI yordamida", ex="Hisobot egasi chiqarib tashlagan manbalar",
               note="Hisobotdagi SHA-256 ning birinchi 16 belgisini yuqoridagi qiymat bilan solishtiring. Bu sahifa PlagIQ dalillar "
                    "hisobotidagi ma’lumotlarni tasdiqlaydi; originallik sertifikati emas va hujjat nusxasini saqlamaydi."),
}


uzcyr.add_to(VERIFY_TEXT)
VERIFY_LANGS = {"uz": "UZ", "uzc": "ЎЗ", "ru": "RU", "en": "EN"}


@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, ui: str = ""):
    accept = request.headers.get("accept-language", "")[:12].lower()
    lang = ui if ui in VERIFY_TEXT else ("uzc" if accept.startswith("uz-cyrl") else
                                         next((l for l in ("uz", "ru") if l in accept[:5]), "en"))
    t = VERIFY_TEXT[lang]
    valid, f = verify.check(request.query_params)
    status = t["off"] if not verify.secret() else (t["ok"] if valid else t["bad"])
    rows = ""
    if valid:
        when = timefmt.show(f["d"], lang)          # the signed time is UTC; shown in Tashkent time
        result = t["ne"] if f["p"] == "NE" else (checkstatus.texts(lang)["at_least"].format(p=f["p"]) + " — " +
                                                  checkstatus.texts(lang)["incomplete_coverage"] if f.get("c") == "i" else f["p"] + "%")
        rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in (
            (t["rid"], f["r"]), (t["date"], when), (t["hash"], f["h"]), (t["mode"], t["modes"][f["m"]]),
            (t["result"], result), (t["words"], f["w"]), (t["sources"], f["n"]), (t["lang"], f["l"].upper()))
            + (((t["ai"], f["x"] + "%"),) if "x" in f else ()) + (((t["ex"], f["e"]),) if "e" in f else ()))
    keep = [(k, v) for k, v in request.query_params.multi_items() if k != "ui"]   # switching language replaces ui
    links = " · ".join(f'<a href="?{html.escape(urlencode(keep + [("ui", l)]))}">{VERIFY_LANGS.get(l, l.upper())}</a>'
                       for l in VERIFY_TEXT)
    colour = "#0b6a64" if valid else "#9b2c16"
    page = (f'<!doctype html><html lang="{"uz-Cyrl" if lang == "uzc" else lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
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
            "ocr":extract.ocr_available(),"fulltext":fulltext.settings()[0],"login":auth.public_config(),"ai_detector":aidetect.settings(),"uz_ai_indicator":uzai.settings(),"archive":corpus_configured() and auth.enabled(),
            "payments":{"enabled":payments.enabled(),"mode":payments.mode() if payments.enabled() else ""}}


def _secure(request):
    return request.url.scheme == "https" or os.getenv("PUBLIC_ORIGIN", "").startswith("https://")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return HTMLResponse(operator_info.privacy_html())


@app.get("/api/sample-report")
def sample_report_json():
    """The labelled sample report (sample_report.py): fictional document and sources, real matching."""
    return JSONResponse(sample_report.build(), headers={"Cache-Control": "no-store"})


@app.get("/api/support")
def support_details():
    """Support and operator details the owner has set (operator_info.py); empty fields are not set yet."""
    return operator_info.support()


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
    current = auth.read(request.cookies.get(auth.COOKIE))
    if data.get("link") is True and current:            # add Telegram to the signed-in account
        try:
            await run_in_threadpool(auth.link_identity, current, user["id"])
        except auth.AccountError as exc:
            raise ApiError(exc.status, exc.code, str(exc))
        return {"user": current, "linked": "telegram"}
    account = await run_in_threadpool(auth.resolve, user["id"])
    if account != user["id"]:                           # Telegram was linked to an e-mail account
        user = {**user, "id": account}
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


@app.post("/auth/email/start")
async def email_start(request: Request):
    """Send a one-time sign-in code (and link) to an address. The answer is the same whether or not the address
    has an account. Signed in, {"link": true} adds the address to the current account instead."""
    data = await _account_form(request)
    current = auth.read(request.cookies.get(auth.COOKIE)) if data.get("link") is True else None
    if data.get("link") is True and not current:
        raise ApiError(401, "login_required", "Sign in first.")
    try:
        sent = await run_in_threadpool(auth.start_email_signin, data.get("email"), _origin(request), client_key(request),
                                       current["id"] if current else None)
    except auth.AccountError as exc:
        raise ApiError(exc.status, exc.code, str(exc))
    if not sent:
        # Every address gets a code the same way, so a sending failure says nothing about accounts: say so, instead
        # of letting the user wait for a code that will not come.
        logging.getLogger("plagiq.auth").error("sign-in e-mail could not be sent (reason in the plagiq.mail line above)")
        raise ApiError(503, "mail_failed", "The e-mail could not be sent right now. Try again later or continue with Telegram.")
    return {"sent": True, "minutes": auth.CODE_MINUTES}


@app.post("/auth/email/verify")
async def email_verify(request: Request):
    data = await _account_form(request)
    try:
        user = await run_in_threadpool(auth.verify_email_code, data.get("email"), data.get("code"), client_key(request))
    except auth.AccountError as exc:
        raise ApiError(exc.status, exc.code, str(exc))
    if user.get("linked"):
        return {"linked": "email"}
    return signed_in_response(request, JSONResponse({"user": user}), user)


@app.get("/auth/email/link", response_class=HTMLResponse)
async def email_link(request: Request, t: str = ""):
    """The one-time link from the sign-in e-mail (the same request as the code)."""
    try:
        user = await run_in_threadpool(auth.use_email_link, t)
    except auth.AccountError as exc:
        return _account_page(request, lambda tx, lang: f'<p>{html.escape(str(exc))}</p>')
    if not user:
        return HTMLResponse('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                            '<p style="font-family:system-ui;padding:20px">This sign-in link has expired or was already used. '
                            'Ask for a new code. · Havola eskirgan yoki ishlatilgan: yangi kod so‘rang. · '
                            'Ссылка устарела или уже использована: запросите новый код.</p>', 410)
    page = HTMLResponse('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/">'
                        '<p style="font-family:system-ui;padding:20px"><a href="/">PlagIQ</a></p>')
    if user.get("linked"):
        return page
    return signed_in_response(request, page, user)


@app.post("/auth/register")
async def email_register(request: Request):
    """Closed: accounts are created by "Continue with Email" (a one-time code) or Telegram, never with a password.
    Kept so older pages get a clear answer."""
    raise ApiError(410, "password_signup_closed", "Use “Continue with Email”: we send a one-time code, no password needed.")


@app.post("/auth/login")
async def email_login(request: Request):
    """Password sign-in for accounts created before e-mail codes (no new password accounts are made)."""
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
            "email": await run_in_threadpool(auth.email_status, user) if user else None,
            "methods": await run_in_threadpool(auth.methods, user) if user else {},
            "history": await run_in_threadpool(auth.history, user) if user else [],
            # Whether this account gets the Uzbek AI-writing indicator (preview: owners and pilot testers).
            "uz_ai_indicator": await run_in_threadpool(uz_ai_for, user) if user else False}


def _origin(request):
    return os.getenv("PUBLIC_ORIGIN", "").strip() or str(request.base_url)


ACCOUNT_T = {
    "en": dict(title="PlagIQ account", verified="Your email address is confirmed. You can close this page.",
               verify_bad="This confirmation link has expired or was already used. Sign in and send a new one.",
               reset_title="Choose a new password", reset_label="New password (at least 8 characters)", reset_button="Save the new password",
               reset_done="Your password was changed and every earlier sign-in was ended. Sign in with the new password.",
               back="Back to PlagIQ"),
    "ru": dict(title="Аккаунт PlagIQ", verified="Адрес электронной почты подтверждён. Эту страницу можно закрыть.",
               verify_bad="Срок действия ссылки истёк или она уже использована. Войдите и отправьте новую.",
               reset_title="Новый пароль", reset_label="Новый пароль (не менее 8 символов)", reset_button="Сохранить пароль",
               reset_done="Пароль изменён, все прежние входы завершены. Войдите с новым паролем.", back="Вернуться в PlagIQ"),
    "uz": dict(title="PlagIQ hisobi", verified="Elektron pochta manzilingiz tasdiqlandi. Bu sahifani yopishingiz mumkin.",
               verify_bad="Bu havolaning muddati tugagan yoki u ishlatilgan. Tizimga kirib, yangisini yuboring.",
               reset_title="Yangi parol", reset_label="Yangi parol (kamida 8 belgi)", reset_button="Yangi parolni saqlash",
               reset_done="Parolingiz o‘zgartirildi va avvalgi barcha kirishlar tugatildi. Yangi parol bilan kiring.",
               back="PlagIQ’ga qaytish"),
}
uzcyr.add_to(ACCOUNT_T)


def _account_page(request, body, ui=""):
    accept = request.headers.get("accept-language", "")[:12].lower()
    lang = ui if ui in ACCOUNT_T else ("uzc" if accept.startswith("uz-cyrl") else next((l for l in ("uz", "ru") if l in accept[:5]), "en"))
    t = ACCOUNT_T[lang]
    page = (f'<!doctype html><html lang="{"uz-Cyrl" if lang == "uzc" else lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(t["title"])}</title>'
            '<style>body{font-family:system-ui,sans-serif;margin:0;background:#f7f9f7;color:#122a32}main{max-width:520px;margin:40px auto;'
            'background:#fff;border:1px solid #dce6e5;border-radius:18px;padding:28px}h1{font-size:22px}label{display:block;margin:14px 0 6px}'
            'input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cfe0dd;border-radius:10px;font:inherit}'
            'button{margin-top:14px;padding:10px 18px;border:0;border-radius:10px;background:#0f6b5b;color:#fff;font:inherit;font-weight:700}'
            '.err{color:#9b2c16}@media(max-width:600px){main{margin:0;border-radius:0}}</style></head><body><main>'
            f'<h1>{html.escape(t["title"])}</h1>{body(t, lang)}<p><a href="/">{html.escape(t["back"])}</a></p></main></body></html>')
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.post("/auth/verify/resend")
async def resend_verification(request: Request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user or user["id"] >= 0:
        raise ApiError(401, "login_required", "Sign in with your email account first.")
    if not mailer.configured():
        raise ApiError(503, "mail_not_configured", "Sending email is not set up on this server.")
    try:
        sent = await run_in_threadpool(auth.request_verification, user, _origin(request), client_key(request))
    except auth.AccountError as exc:
        raise ApiError(exc.status, exc.code, str(exc))
    return {"sent": bool(sent)}


@app.get("/auth/verify-email", response_class=HTMLResponse)
async def verify_email_page(request: Request, t: str = "", ui: str = ""):
    ok = await run_in_threadpool(auth.confirm_email, t)
    return _account_page(request, lambda tx, lang: f'<p class="{"" if ok else "err"}">{html.escape(tx["verified"] if ok else tx["verify_bad"])}</p>', ui)


@app.post("/auth/forgot")
async def forgot_password(request: Request):
    """Always the same answer, whether or not the address has an account."""
    data = await _account_form(request)
    if not auth.email_enabled() or not mailer.configured():
        raise ApiError(503, "recovery_not_configured", "Password recovery by email is not set up on this server yet.")
    try:
        await run_in_threadpool(auth.request_reset, data.get("email"), _origin(request), client_key(request))
    except auth.AccountError as exc:
        raise ApiError(exc.status, exc.code, str(exc))
    return {"sent": True}


@app.get("/auth/reset", response_class=HTMLResponse)
def reset_page(request: Request, t: str = "", ui: str = ""):
    # Sent with fetch: a plain form post carries "Origin: null" under the site's no-referrer policy and would be
    # refused by the cross-site check (security.py), which stays strict.
    form = lambda tx, lang: (f'<form id="reset" method="post" action="/auth/reset?ui={lang}"><input type="hidden" name="t" value="{html.escape(t)}">'
                             f'<label for="password">{html.escape(tx["reset_label"])}</label>'
                             '<input id="password" name="password" type="password" minlength="8" maxlength="200" autocomplete="new-password" required>'
                             f'<button type="submit">{html.escape(tx["reset_button"])}</button><p id="reset-out" role="status"></p></form>'
                             '<script>document.getElementById("reset").addEventListener("submit",async e=>{e.preventDefault();'
                             'let f=e.target,out=document.getElementById("reset-out");out.className="";'
                             'try{let r=await fetch(f.action,{method:"POST",body:new FormData(f),headers:{Accept:"application/json"}}),d=await r.json();'
                             'out.textContent=d.message;if(r.ok){f.querySelector("input[type=password]").disabled=true;f.querySelector("button").hidden=true}else out.className="err"}'
                             'catch(_){out.className="err";out.textContent="Network error"}})</script>')
    return _account_page(request, form, ui)


@app.post("/auth/reset", response_class=HTMLResponse)
async def reset_submit(request: Request, t: str = Form("", max_length=100), password: str = Form("", max_length=200), ui: str = ""):
    wants_json = "application/json" in request.headers.get("accept", "")
    try:
        await run_in_threadpool(auth.reset_password, t, password)
    except auth.AccountError as exc:
        message = str(exc)
        if wants_json:
            return JSONResponse({"code": exc.code, "message": message}, 400)
        return _account_page(request, lambda tx, lang: f'<p class="err">{html.escape(message)}</p>'
                             + (f'<p><a href="/auth/reset?t={html.escape(t)}&amp;ui={lang}">↺</a></p>' if exc.code == "weak_password" else ""), ui)
    if wants_json:
        lang = ui if ui in ACCOUNT_T else "en"
        response = JSONResponse({"code": "password_changed", "message": ACCOUNT_T[lang]["reset_done"]})
    else:
        response = _account_page(request, lambda tx, lang: f'<p>{html.escape(tx["reset_done"])}</p>', ui)
    response.delete_cookie(auth.COOKIE)
    return response


@app.delete("/api/me")
async def delete_me(request: Request):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user:
        raise ApiError(401, "login_required", "Sign in with Telegram first.")
    if await run_in_threadpool(payments.in_progress, user):
        raise ApiError(409, "payment_in_progress", "A payment is still in progress; the account was kept. Try again in an hour.")
    if corpus_configured():                    # the right to erasure covers archived works too
        try:
            for item in await engine_client.list_deposits(auth.owner_id(user)):
                await engine_client.remove_deposit(item["id"], auth.owner_id(user))
        except EngineError as exc:
            raise ApiError(503, exc.code, "Archived works could not be removed; the account was kept. Try again later.")
    if await run_in_threadpool(payments.has_records, user):      # erasure covers payment records and the balance
        try:
            await run_in_threadpool(payments.forget, user)
        except payments.PaymentError as exc:
            raise ApiError(409, exc.code, str(exc))
    identities = await run_in_threadpool(auth.identities, user)
    await run_in_threadpool(entitlements.forget, user, identities)   # a used free trial stays used (hashed marker)
    await run_in_threadpool(auth.forget, user)
    await run_in_threadpool(reports_store.forget_user, user["id"])
    await run_in_threadpool(pending_checks.forget_user, user["id"])
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


@app.get("/api/templates")
def template_institutions():
    """Universities with approved template text, for the check form (names and counts only)."""
    return {"institutions": templates.institutions_with_templates(list(institutions.configured()))}


@app.get("/api/institution/templates")
def institution_templates(request: Request, institution: str):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not institutions.allowed(user, institution):
        raise ApiError(403, "institution_forbidden", "This account may not manage this institution's templates.")
    return {"items": templates.listing(institution), "max": templates.MAX_TEMPLATES}


@app.post("/api/institution/templates")
async def institution_template_add(request: Request, institution: str = Form(..., max_length=80),
                                   file: UploadFile | None = File(None), text: str = Form("", max_length=400_000),
                                   title: str = Form("", max_length=templates.MAX_TITLE)):
    """A university admin adds wording its students must use (title page, approval sheet, declaration)."""
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not institutions.allowed(user, institution):
        raise ApiError(403, "institution_forbidden", "This account may not manage this institution's templates.")
    content = await read_file(file) if file and file.filename else text
    if not content.strip():
        raise ApiError(400, "template_missing", "Upload a file or paste the template text.")
    name = " ".join(title.split()) or (clean_filename(file.filename, "Template").rsplit(".", 1)[0] if file and file.filename else "Template")
    try:
        identifier = await run_in_threadpool(templates.add, institution, name, content, user["id"])
    except templates.TemplateError as exc:
        raise ApiError(400, exc.code, str(exc))
    return {"id": identifier, "title": name, "words": len(content.split())}


@app.delete("/api/institution/templates/{template_id}")
def institution_template_remove(template_id: int, request: Request, institution: str):
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not institutions.allowed(user, institution):
        raise ApiError(403, "institution_forbidden", "This account may not manage this institution's templates.")
    if not templates.remove(institution, template_id):
        raise ApiError(404, "report_unavailable", "Not found")
    return {"deleted": True}


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
    inventory = await engine_client.inventory() if data is not None else None
    evaluation = await engine_client.evaluation() if data is not None else None
    return HTMLResponse(admin.page({"available": data is not None, **(data or {})}, reason, inventory,
                                   evaluation=evaluation), headers={"Cache-Control": "no-store"})


@app.post("/admin/evaluation", response_class=HTMLResponse)
async def admin_evaluation(request: Request, per_language: int = Form(30, ge=5, le=60)):
    """Owner-only: start the accuracy test on the live library (engine /v1/evaluation)."""
    owner(request)
    started = await engine_client.evaluation(start=True, per_language=per_language)
    data = await engine_library() if corpus_configured() else None
    inventory = await engine_client.inventory() if data is not None else None
    notice = started.get("notice") or ("Accuracy test started; reload /admin to follow it." if started.get("state") == "running"
                                        else started.get("error") or "")
    return HTMLResponse(admin.page({"available": data is not None, **(data or {})}, "", inventory, notice=notice,
                                   evaluation=started), headers={"Cache-Control": "no-store"})


async def _admin_page_with(pilot, notice=""):
    data = await engine_library() if corpus_configured() else None
    inventory = await engine_client.inventory() if data is not None else None
    return HTMLResponse(admin.page({"available": data is not None, **(data or {})}, "", inventory, pilot=pilot,
                                   notice=notice), headers={"Cache-Control": "no-store"})


@app.post("/admin/grants", response_class=HTMLResponse)
async def admin_grant(request: Request, user_id: int = Form(...), checks: int = Form(3), max_words: int = Form(40000),
                      days: int = Form(14), note: str = Form("", max_length=200)):
    """Owner-only: a limited number of pilot test checks for one account."""
    owner(request)
    try:
        gid = await run_in_threadpool(entitlements.grant, auth.resolve(user_id), checks, max_words, days, note)
        message = f"Granted: allowance #{gid}, {checks} check(s) up to {max_words:,} words for {days} day(s)."
    except entitlements.EntitlementError as exc:
        message = str(exc)
    return await _admin_page_with(admin.pilot_section(message), message)


@app.post("/admin/grants/revoke", response_class=HTMLResponse)
async def admin_revoke(request: Request, grant_id: int = Form(...)):
    owner(request)
    done = await run_in_threadpool(entitlements.revoke, grant_id)
    return await _admin_page_with(admin.pilot_section(f"Allowance #{grant_id} revoked." if done else "Nothing changed."))


def mail_route():
    return {"resend": "Resend", "brevo": "Brevo", "dev": "the development backend (server log only)"}.get(
        mailer.backend(), os.environ.get("SMTP_HOST", "SMTP"))


def mail_hint(detail):
    """Plain-language next step for a failed test e-mail."""
    if detail.startswith("Resend HTTP 401") or detail.startswith("Resend HTTP 403") and "key" in detail.lower():
        return "Resend refused the API key: create a sending key in Resend (API Keys) and set RESEND_API_KEY."
    if detail.startswith("Resend HTTP 403") or detail.startswith("Resend HTTP 422"):
        return ("Resend refused the sender: SMTP_FROM must be an address on a domain verified in Resend (Domains). "
                "Until a domain is verified, Resend delivers only to your own Resend account address.")
    if detail.startswith("Brevo HTTP 401") or "unauthorized" in detail.lower():
        return "Brevo refused the API key: create a new key in Brevo (SMTP & API → API keys) and set BREVO_API_KEY."
    if detail.startswith("Brevo HTTP 400") or detail.startswith("Brevo HTTP 403"):
        return ("Brevo refused the message: check that the address in SMTP_FROM is a verified sender in Brevo (Senders) "
                "and that the account is allowed to send transactional e-mail.")
    if "Authentication" in detail or "535" in detail or "534" in detail:
        return ("The mail server refused the login: check SMTP_USER and SMTP_PASSWORD (for Gmail: a 16-character "
                "app password without spaces, and SMTP_USER = the same Gmail address).")
    if any(word in detail for word in ("Timeout", "timed out", "ConnectionRefused", "Network is unreachable", "OSError",
                                       "ConnectionReset", "gaierror")):
        return ("The server could not reach the mail server at all. The hosting may block outgoing mail ports "
                "(587/465); check the hosting plan, or use an e-mail service with a web API.")
    return "Check the SMTP settings (DEPLOYMENT.md, Part 3v)."


@app.post("/admin/test-email", response_class=HTMLResponse)
async def admin_test_email(request: Request, email: str = Form(..., max_length=254)):
    """Owner-only: check SMTP delivery before e-mail sign-in codes are switched on."""
    owner(request)
    if not (mailer.configured() or mailer.dev()):
        message = "E-mail is not set up (RESEND_API_KEY, BREVO_API_KEY or SMTP_HOST, plus SMTP_FROM), so nothing was sent."
    else:
        try:
            await run_in_threadpool(mailer.send, email.strip(), "PlagIQ test e-mail",
                                    "This is a test message from the PlagIQ owner dashboard. If it arrived, e-mail delivery works.")
            message = (f"Test e-mail: sent to {email.strip()} through {mail_route()}. "
                       "If it does not arrive within a few minutes (check spam too), tell the developer.")
        except mailer.MailError as exc:
            message = f"Test e-mail: sending FAILED — {exc}. " + mail_hint(str(exc))
    return await _admin_page_with(admin.pilot_section(message), message)


@app.post("/admin/test-alert", response_class=HTMLResponse)
async def admin_test_alert(request: Request):
    """Owner-only: send a test owner alert by the configured route (alerts.py)."""
    owner(request)
    ok = await run_in_threadpool(alerts._deliver, "🔔 PlagIQ: test alert from the owner dashboard. Alerts reach you here.")
    message = ("Test alert sent." if ok else
               "Test alert NOT delivered: press Start in the PlagIQ Telegram bot from the owner's Telegram account, or "
               "set ALERT_EMAIL, then try again.")
    return await _admin_page_with(admin.pilot_section(message), message)


@app.post("/admin/compact", response_class=HTMLResponse)
async def admin_compact(request: Request, confirm: int = Form(0), confirm_text: str = Form("", max_length=40)):
    """Owner-only: show the library file's sizes, or (with the typed confirmation) compact it."""
    owner(request)
    do = bool(confirm) and confirm_text.strip().upper() == admin.COMPACT_WORDS
    result, error = None, ""
    if confirm and not do:
        error = f"Type {admin.COMPACT_WORDS} to compact. Nothing was changed; the sizes are shown below."
    try:
        result = await engine_client.compact(do)
    except EngineError as exc:
        error = str(exc)
    data = await engine_library() if corpus_configured() else None
    inventory = await engine_client.inventory() if data is not None else None
    notice = ("Library compacted." if result and result.get("compacted") else error)
    return HTMLResponse(admin.page({"available": data is not None, **(data or {})}, "", inventory, notice=notice,
                                   compacting=admin.compact_form(result, error)), headers={"Cache-Control": "no-store"})


@app.post("/admin/prune", response_class=HTMLResponse)
async def admin_prune(request: Request, keep: int = Form(100, ge=0, le=100000), confirm_text: str = Form("", max_length=40)):
    """Owner-only: preview, or with the typed confirmation remove, Wikipedia articles beyond `keep` per language."""
    owner(request)
    confirm = confirm_text.strip().upper() == admin.CONFIRM_WORDS
    result, error = None, ""
    if confirm_text.strip() and not confirm:
        error = f"The confirmation text did not match: type {admin.CONFIRM_WORDS} exactly. Nothing was removed."
    try:
        result = await engine_client.prune(keep, confirm)
    except EngineError as exc:
        error = str(exc)
    data = await engine_library() if corpus_configured() else None
    inventory = await engine_client.inventory() if data is not None else None
    LIBRARY_CACHE["at"] = 0                                     # public counts refresh at once
    return HTMLResponse(admin.page({"available": data is not None, **(data or {})}, "", inventory, "",
                                   admin.prune_form(inventory, result, error, keep)),
                        headers={"Cache-Control": "no-store"})


@app.post("/admin/diagnostic", response_class=HTMLResponse)
async def admin_diagnostic(request: Request, doi: str = Form("", max_length=300), title: str = Form("", max_length=500),
                           passage: str = Form("", max_length=5000)):
    """Owner-only: is a known licensed source indexed and is a passage from it found? Facts only, no source text."""
    owner(request)
    result, error = None, ""
    try:
        result = await engine_client.diagnostic(doi.strip(), title.strip(), passage)
    except EngineError as exc:
        error = str(exc)
    data = await engine_library() if corpus_configured() else None
    inventory = await engine_client.inventory() if data is not None else None
    form = admin.diagnostic_form(result, error, doi.strip(), title.strip())
    return HTMLResponse(admin.page({"available": data is not None, **(data or {})}, "", inventory, form),
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
    config["trial_exports"] = False
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
    when = timefmt.show(row["paid"], "uz")
    seller = os.environ.get("SELLER_NAME", "PlagIQ")
    tin = os.environ.get("SELLER_TIN", "")
    test = row["mode"] == "test"
    lines = [("Order / Buyurtma / Заказ", row["id"]), ("Date / Sana / Дата", when),
             ("Customer / Mijoz / Клиент", user["name"]),
             ("Paid with / To‘lov / Оплата", {"payme": "Payme", "click": "Click", "uzum": "Uzum Bank"}.get(row["provider"], row["provider"])),
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
            f'<table>{table}</table><p><small>{"Payment for one PlagIQ check" if row.get("purpose") == "check" else "Balance top-up for PlagIQ checks"}. This page is a payment confirmation, '
            f'not a fiscal receipt; the fiscal receipt, when issued, comes from the payment provider.</small></p>'
            f'<button onclick="print()">Print / Chop etish / Печать</button></main></body></html>')


@app.post("/payments/payme")
async def payme_endpoint(request: Request):
    # Payme expects HTTP 200 with a JSON-RPC body in every case, errors included.
    body = await request.body()
    reply = await run_in_threadpool(payments.payme, body, request.headers.get("authorization", ""))
    return JSONResponse(reply, 200)


@app.post("/payments/uzum/{method}")
async def uzum_endpoint(method: str, request: Request):
    body = await request.body()
    status, reply = await run_in_threadpool(payments.uzum, method, body, request.headers.get("authorization", ""))
    return JSONResponse(reply, status)


@app.post("/payments/click/{stage}")
async def click_endpoint(stage: str, request: Request):
    if stage not in ("prepare", "complete"):
        raise ApiError(404, "not_found", "Not found")
    form = await request.form()
    reply = await run_in_threadpool(payments.click, {k: v for k, v in form.items() if isinstance(v, str)}, stage)
    return JSONResponse(reply, 200)


@app.post("/api/quote")
async def price_quote(request: Request, submission: UploadFile = File(...), mode: str = Form("academic"),
                      ai_check: bool = Form(False), archive: bool = Form(False)):
    """Word count, the cheapest plan and, for a signed-in user, what this check would be (free trial, recheck,
    paid or pilot) and its total. Prices come from the server's configuration only. Nothing is stored or reserved;
    no check is run."""
    key, now = client_key(request), time.monotonic()
    history = [t for t in QUOTES.get(key, []) if now - t < 3600]
    if len(history) >= QUOTE_LIMIT:
        raise ApiError(429, "rate_limited", "Hourly quote allowance reached.")
    QUOTES[key] = history + [now]
    if len(QUOTES) > 3000:
        QUOTES.clear()
    if mode not in ("reference", "academic", "corpus", "ai"):
        raise ApiError(400, "invalid_mode", "Invalid mode")
    text = await read_file(submission)
    words = pricing.count_words(text)
    out = {**pricing.quote(words), "ai_price_uzs": pricing.ai_price(words), "characters": len(text),
           "currency": "UZS", "payments_enabled": payments.enabled(), "paid": pricing.paid_quote(words),
           "trial_max_words": pricing.trial_max_words(), "trial_fits": 0 < words <= pricing.trial_max_words()}
    user = auth.read(request.cookies.get(auth.COOKIE)) if auth.required() else None
    if user:
        decision, _ = await check_decision(user, text, mode, ai_check or mode == "ai", archive)
        if decision.get("recheck"):
            decision["recheck"] = {k: decision["recheck"][k] for k in ("original_tier", "original_words", "days_left",
                                                                       "remaining")}
        if decision.get("recheck_upgrade"):
            decision["recheck_upgrade"] = {k: decision["recheck_upgrade"][k] for k in ("original_tier", "new_tier",
                                                                                       "days_left", "remaining")}
        decision.pop("grant", None)
        out["decision"] = decision
    return out


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(reports_store.fill(Path(__file__).with_name("index.html").read_text("utf-8")),
                        headers={"Cache-Control":"no-store, max-age=0"})


def validate_check(user, language, mode, consent, exclude_quotes, exclude_bibliography, min_words, ai_check, ai_consent,
                   archive, archive_consent, archive_title, archive_author, institution):
    """The check's options from the form, or ApiError. Shared by /api/check and /api/check/pay."""
    if language not in ("uz","ru","en"):
        raise ApiError(400, "invalid_language", "Invalid language")
    if mode not in ("reference","academic","corpus","ai"):
        raise ApiError(400, "invalid_mode", "Invalid mode")
    if min_words not in citations.MIN_WORD_OPTIONS:
        raise ApiError(400, "invalid_option", "Unsupported minimum match length")
    options = dict(exclude_quotes=exclude_quotes, exclude_bibliography=exclude_bibliography, min_words=min_words)
    institution = " ".join(institution.split())
    if institution:                                  # the student's university: its approved template text is excluded
        if institution not in institutions.configured():
            raise ApiError(400, "institution_unknown", "This university is not set up in PlagIQ.")
        options["template"] = institution
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
    return options, ai_check, deposit


DOC_TYPES = ("article", "dissertation", "master", "bachelor", "course", "conference", "monograph", "other")


def document_meta(author, title, kind):
    """Optional author, title and type the user typed for the certificate: plain text, no control characters."""
    clean = lambda v, n: " ".join("".join(ch for ch in str(v or "") if ch.isprintable()).split())[:n]
    meta = {"author": clean(author, 200), "title": clean(title, 400), "type": kind if kind in DOC_TYPES else ""}
    return {k: v for k, v in meta.items() if v}


@app.post("/api/check")
async def check(request:Request, submission:UploadFile=File(...),
                reference:UploadFile|None=File(None),reference_text:str=Form(""),
                language:str=Form("uz"),mode:str=Form("reference"),consent:bool=Form(False),
                exclude_quotes:bool=Form(False),exclude_bibliography:bool=Form(False),min_words:int=Form(7),
                web_search:bool=Form(False),ai_check:bool=Form(False),ai_consent:bool=Form(False),
                archive:bool=Form(False),archive_consent:bool=Form(False),archive_title:str=Form("",max_length=300),
                archive_author:str=Form("",max_length=300),institution:str=Form("",max_length=80),
                progress_token:str=Form("",alias="progress",max_length=40),
                expected_price:int|None=Form(None,ge=0,le=10_000_000),
                doc_author:str=Form("",max_length=200),doc_title:str=Form("",max_length=400),
                doc_type:str=Form("",max_length=20),ui_lang:str=Form("",max_length=5)):
    global active_scans
    purge_reports()
    user = auth.read(request.cookies.get(auth.COOKIE))
    anonymous = False
    if auth.required() and not user:          # the free trial and paid checks belong to an account
        raise ApiError(401, "login_required", "Sign in to run this check.")
    options, ai_check, deposit = validate_check(user, language, mode, consent, exclude_quotes, exclude_bibliography,
                                                min_words, ai_check, ai_consent, archive, archive_consent, archive_title,
                                                archive_author, institution)
    queued = active_scans >= scan_capacity() or bool(_waiting)
    if queued and len(_waiting) >= queue_max():
        raise ApiError(503, "server_busy", "PlagIQ is processing other documents. Try again in a minute.")
    rate_limit(request, user)
    submission.document_meta = document_meta(doc_author, doc_title, doc_type)
    ticket = None
    if queued:
        ticket = object()                      # joins the line before any await, so the order is the arrival order
        _waiting.append(ticket)
    else:
        active_scans += 1                      # reserve the slot before any await, so overlapping requests cannot all pass
    # The check is recorded before it runs: if the browser is closed, it still finishes and appears in "My checks".
    job = uuid.uuid4().hex
    session = request.cookies.get("plagiq_session") or secrets.token_urlsafe(32)
    started = time.monotonic()
    progress.begin(progress_token)
    try:
        await run_in_threadpool(reports_store.start, job, session, user["id"] if user else None,
                                clean_filename(submission.filename, "Document"), mode)
        if ticket is not None:
            await _wait_for_slot(ticket)
            ticket = None
            started = time.monotonic()         # the check's own time, without the wait (metrics, estimates)
        if user and auth.required():
            response = await _run_entitled_check(request, user, submission, reference, reference_text, language, mode,
                                                 options, web_search, ai_check, deposit, job, session, started, expected_price)
        else:
            response = await _run_check(request, user, anonymous, submission, reference, reference_text, language, mode,
                                        options, web_search, ai_check, deposit, job, session, started)
        await _notify_if_left(request, user, progress_token, submission.filename, True, ui_lang)
        return response
    except ApiError as exc:
        status = checkstatus.error_status(exc.code, exc.status_code)
        if status == "rejected":
            await run_in_threadpool(reports_store.delete, job)   # not a check (sign-in, balance, limits)
        else:
            await run_in_threadpool(reports_store.fail, job, exc.code)
            await _notify_if_left(request, user, progress_token, submission.filename, False, ui_lang)
            await run_in_threadpool(metrics.record_failure, mode, status, exc.code, time.monotonic() - started)
            progress.finish(failed=True)
            if not request.cookies.get("plagiq_session"):        # so the failed check shows under "My checks"
                cookie = Response()
                cookie.set_cookie("plagiq_session", session, max_age=reports_store.ttl(), httponly=True,
                                  secure=_secure(request), samesite="strict")
                exc.headers = {**(exc.headers or {}), "set-cookie": cookie.headers["set-cookie"]}
        raise
    except BaseException:
        await run_in_threadpool(reports_store.fail, job, "internal_error")
        await run_in_threadpool(metrics.record_failure, mode, "failed_internal", "internal_error", time.monotonic() - started)
        progress.finish(failed=True)
        raise
    finally:
        if ticket is not None:                 # gave up while waiting: it never held a slot
            if ticket in _waiting:
                _waiting.remove(ticket)
        else:
            active_scans -= 1
        progress.finish()


def queue_max():
    """How many checks may wait for a free slot (QUEUE_MAX, default 10; 0 = refuse at once when busy). Each waiting
    check keeps its uploaded file in memory."""
    try:
        return max(0, min(int(os.getenv("QUEUE_MAX", "10")), 50))
    except ValueError:
        return 10


def queue_wait_seconds():
    try:
        return max(60, min(int(os.getenv("QUEUE_WAIT_MINUTES", "20")), 120)) * 60
    except ValueError:
        return 1200


async def _wait_for_slot(ticket):
    """Wait in line (first come, first served) until a check slot is free; the page shows the place in the line.
    After QUEUE_WAIT_MINUTES the check stops with server_busy (it is listed as failed under "My checks")."""
    global active_scans
    stop = time.monotonic() + queue_wait_seconds()
    while True:
        place = _waiting.index(ticket) + 1
        if place == 1 and active_scans < scan_capacity():
            _waiting.remove(ticket)
            active_scans += 1
            return
        progress.step("queue", place)
        if time.monotonic() > stop:
            raise ApiError(503, "server_busy", "PlagIQ is processing other documents. Try again in a minute.")
        await asyncio.sleep(0.5)


async def _notify_if_left(request, user, token, filename, ok, ui_lang):
    """"Your check is ready" by Telegram or e-mail when the page was closed before the check ended (notify.py)."""
    try:
        disconnected = await request.is_disconnected()
    except Exception:
        disconnected = False
    notify.after_check(user, token, disconnected, clean_filename(filename, "Document"), ok,
                       ui_lang if ui_lang in ("uz", "uzc", "ru", "en") else "uz", _origin(request))


# ------------------------------------------------------------- one document, one payment (TEST mode only)

class _StoredDocument:
    """Stands in for the uploaded file when the server runs a paid check later (its text was read at payment)."""
    def __init__(self, filename, ocr_info=None):
        self.filename, self.ocr_info = filename, ocr_info or {}


PAY_REQUEST = re.compile(r"[0-9a-f]{32}")


@app.post("/api/check/pay")
async def check_pay(request: Request, submission: UploadFile = File(...), reference: UploadFile | None = File(None),
                    reference_text: str = Form(""), language: str = Form("uz"), mode: str = Form("reference"),
                    consent: bool = Form(False), exclude_quotes: bool = Form(False), exclude_bibliography: bool = Form(False),
                    min_words: int = Form(7), web_search: bool = Form(False), ai_check: bool = Form(False),
                    ai_consent: bool = Form(False), archive: bool = Form(False), archive_consent: bool = Form(False),
                    archive_title: str = Form("", max_length=300), archive_author: str = Form("", max_length=300),
                    institution: str = Form("", max_length=80), expected_price: int = Form(..., ge=1, le=10_000_000),
                    provider: str = Form(..., max_length=10), request_id: str = Form(..., max_length=32)):
    """Pay for exactly this check with Payme, Click or Uzum Bank (test mode). The price is worked out here and must
    equal the price the page showed; the check runs on the server once the provider has confirmed the payment
    (pending_checks.py). The same request_id returns the same order."""
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user:
        raise ApiError(401, "login_required", "Sign in to run this check.")
    if not payments.enabled():
        raise ApiError(409, "payments_off", "Payments are not open yet.")
    if not PAY_REQUEST.fullmatch(request_id):
        raise ApiError(400, "invalid_request", "Invalid request.")
    known = await run_in_threadpool(pending_checks.by_request, user["id"], request_id)
    if known:                                        # a double click or a retried request: the same order
        row = await run_in_threadpool(payments.order, user, known["order_id"])
        return {"pending_id": known["id"], "order_id": known["order_id"], "pay_uzs": known["price"],
                "provider": row["provider"] if row else provider,
                "pay_url": payments.checkout_url(row["provider"], row["id"], row["pay_uzs"]) if row and row["state"] == "pending" else None,
                "state": known["state"], "repeated": True}
    options, ai_check, deposit = validate_check(user, language, mode, consent, exclude_quotes, exclude_bibliography,
                                                min_words, ai_check, ai_consent, archive, archive_consent, archive_title,
                                                archive_author, institution)
    rate_limit(request, user, purpose="pay")
    original = await read_file(submission)
    if mode == "reference":
        reference_text = await read_file(reference, "reference") if reference and reference.filename else reference_text
        if not reference_text.strip():
            raise ApiError(400, "reference_missing", "Provide a reference document or text, or choose academic discovery")
    decision, _ = await check_decision(user, original, mode, ai_check, deposit is not None)
    if decision["kind"] not in ("paid", "recheck") or not decision.get("total_uzs"):
        raise ApiError(409, "no_payment_needed", "This check needs no payment. Start it with the Check button.")
    if expected_price != decision["total_uzs"]:
        raise ApiError(409, "price_changed", f"The price of this check is {decision['total_uzs']} so'm. Check the price and confirm again.")
    try:
        order = await run_in_threadpool(payments.create_order, user, provider, decision["total_uzs"], None, "check")
    except payments.PaymentError as exc:
        raise ApiError(400 if exc.code == "invalid_amount" else 503, exc.code, str(exc))
    session = request.cookies.get("plagiq_session") or secrets.token_urlsafe(32)
    params = {"language": language, "mode": mode, "options": options, "web_search": web_search, "ai_check": ai_check,
              "deposit": deposit, "reference_text": reference_text if mode == "reference" else "",
              "ocr_info": {k: v for k, v in (getattr(submission, "ocr_info", None) or {}).items()
                           if isinstance(v, (str, int, float)) and not callable(v)}}
    try:
        ident = await run_in_threadpool(pending_checks.create, user["id"], session, request_id, order["id"],
                                        decision["total_uzs"], clean_filename(submission.filename, "Document"), params, original)
    except sqlite3.IntegrityError:                   # the same request arrived twice at the same moment
        known = await run_in_threadpool(pending_checks.by_request, user["id"], request_id)
        return {"pending_id": known["id"], "order_id": known["order_id"], "pay_uzs": known["price"], "repeated": True,
                "state": known["state"], "provider": provider, "pay_url": None}
    response = JSONResponse({"pending_id": ident, "order_id": order["id"], "pay_uzs": order["pay_uzs"],
                             "provider": order["provider"], "pay_url": order["pay_url"],
                             "service_name": order.get("service_name"), "state": "awaiting",
                             "keep_hours": pending_checks.hours()})
    if not request.cookies.get("plagiq_session"):
        response.set_cookie("plagiq_session", session, max_age=reports_store.ttl(), httponly=True,
                            secure=_secure(request), samesite="strict")
    return response


@app.get("/api/check/pending")
async def check_pending(request: Request, lang: str = ""):
    """The signed-in account's paid checks and their state: awaiting (payment not confirmed by the provider yet),
    running, done (with the report), failed (the payment stays on the balance) or expired. Asking also starts any
    check whose payment has just been confirmed."""
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user:
        raise ApiError(401, "login_required", "Sign in first.")
    notify.page_seen(user["id"], lang)                 # the page is open: no "check is ready" message needed
    await run_paid_checks(user["id"])
    rows = await run_in_threadpool(pending_checks.listing, user["id"])
    out = []
    for row in rows:
        order = await run_in_threadpool(payments.order, user, row["order_id"])
        out.append({"id": row["id"], "state": row["state"], "filename": row["filename"], "price_uzs": row["price"],
                    "order_id": row["order_id"], "order_state": order["state"] if order else None,
                    "report_id": row["job"] if row["state"] == "done" else None, "error": row["error"],
                    "created": datetime.fromtimestamp(row["created"], timezone.utc).isoformat(timespec="seconds"),
                    "expires": datetime.fromtimestamp(row["expires"], timezone.utc).isoformat(timespec="seconds")})
    return JSONResponse({"checks": out, "keep_hours": pending_checks.hours()}, headers={"Cache-Control": "no-store"})


_PAID_RUNNING = set()


async def run_paid_checks(user_id=None, wait=False):
    """Start every awaiting check whose order the provider has confirmed (state 'paid' in the orders table, which
    only the providers' signed server-to-server calls set). Each check is claimed once and runs in the background."""
    if not payments.enabled():
        return 0
    started = []
    for row in await run_in_threadpool(pending_checks.awaiting):
        if user_id is not None and row["user"] != user_id:
            continue
        order = await run_in_threadpool(payments.order, {"id": row["user"]}, row["order_id"])
        if not order or order["state"] != "paid" or row["id"] in _PAID_RUNNING:
            continue
        claimed = await run_in_threadpool(pending_checks.claim, row["id"])
        if claimed:
            _PAID_RUNNING.add(row["id"])
            started.append(asyncio.create_task(_run_paid(claimed)))
    if wait and started:                             # tests and start-up: wait for them
        await asyncio.gather(*started)
    return len(started)


async def _run_paid(row):
    global active_scans
    try:
        if active_scans >= scan_capacity() or _waiting:
            await run_in_threadpool(pending_checks.release, row["id"])     # busy: the next round runs it
            return
        active_scans += 1
        user = await run_in_threadpool(auth.account, row["user"])
        params = row["params"]
        job, started = uuid.uuid4().hex, time.monotonic()
        mode = params.get("mode", "reference")
        try:
            await run_in_threadpool(reports_store.start, job, row["session"], user["id"], row["filename"], mode)
            await _run_entitled_check(None, user, _StoredDocument(row["filename"], params.get("ocr_info")), None,
                                      params.get("reference_text", ""), params.get("language", "uz"), mode,
                                      params.get("options") or {}, params.get("web_search", False),
                                      params.get("ai_check", False), params.get("deposit"), job, row["session"], started,
                                      row["price"], original=row["text"])
            await run_in_threadpool(pending_checks.finish, row["id"], job)
            notify.after_paid_check(user, row["filename"], True, os.getenv("PUBLIC_ORIGIN", ""))
        except ApiError as exc:
            status = checkstatus.error_status(exc.code, exc.status_code)
            await run_in_threadpool(reports_store.fail, job, exc.code)
            await run_in_threadpool(metrics.record_failure, mode, status, exc.code, time.monotonic() - started)
            await run_in_threadpool(pending_checks.finish, row["id"], None, exc.code)   # the payment stays on the balance
            notify.after_paid_check(user, row["filename"], False, os.getenv("PUBLIC_ORIGIN", ""))
        except Exception:
            logging.getLogger("plagiq.check").exception("paid check failed")
            await run_in_threadpool(reports_store.fail, job, "internal_error")
            await run_in_threadpool(pending_checks.finish, row["id"], None, "internal_error")
            notify.after_paid_check(user, row["filename"], False, os.getenv("PUBLIC_ORIGIN", ""))
        finally:
            active_scans -= 1
    finally:
        _PAID_RUNNING.discard(row["id"])


TRIAL_T = {
    "trial_used": "Your free check has already been used. Paid checks open when payments launch; prices are shown on the page.",
    "trial_too_long": "The free check covers documents of up to {n} words. This document needs a paid plan.",
    "trial_no_extras": "The free check is a plagiarism check without the AI detector or the archive.",
    "payments_off": "Payments are not open yet (pilot). This check will be available when payments launch.",
    "too_long_for_plans": "The document is larger than the biggest plan. Contact us.",
}


async def check_decision(user, text, mode, ai_check, archive):
    """What a check of `text` would be for this user and what it costs in total (entitlements.decide + AI price)."""
    words = pricing.count_words(text)
    identities = await run_in_threadpool(auth.identities, user)
    decision = await run_in_threadpool(entitlements.decide, user, words, text, mode == "ai", bool(ai_check or archive),
                                       payments.enabled(), identities)
    if mode == "ai" and decision["kind"] != "blocked":
        price = pricing.ai_price(words)
        decision.update(tier="ai", full_price_uzs=price, recheck=None)
        decision["price_uzs"] = price if decision["kind"] == "paid" else 0
        if decision["kind"] == "pilot":
            decision["future_price_uzs"] = price
    ai_extra = pricing.ai_price(words) if ai_check and mode != "ai" else 0
    decision["ai_price_uzs"] = ai_extra
    decision["total_uzs"] = (decision.get("price_uzs") or 0) + (ai_extra if decision["kind"] in ("paid", "recheck") and
                                                                 decision.get("payments_enabled") else 0)
    if decision.get("future_price_uzs") is not None:
        decision["future_total_uzs"] = decision["future_price_uzs"] + ai_extra
    return decision, identities


def _succeeded(result, mode):
    """A check that produced what was asked for: a percentage (plagiarism) or an AI estimate (AI-only)."""
    if mode == "ai":
        return (result.get("ai") or {}).get("status") == "done"
    return result.get("percent") is not None


async def _run_entitled_check(request, user, submission, reference, reference_text, language, mode, options,
                              web_search, ai_check, deposit, job, session, started, expected_price, original=None):
    """A signed-in check: free trial, paid check, discounted recheck or pilot allowance, decided and reserved on
    the server before the check runs. The reservation is used only when the check produced a result; any failure
    releases it (and gives back a balance charge)."""
    if original is None:
        original = await read_file(submission)
    decision, identities = await check_decision(user, original, mode, ai_check, deposit is not None)
    if decision["kind"] == "blocked":
        reason = decision.get("reason") or "payment_required"
        raise ApiError(413 if reason == "too_long_for_plans" else 402, reason,
                       TRIAL_T.get(reason, "This check is not available.").format(n=num_words(pricing.trial_max_words())))
    total = decision["total_uzs"]
    if decision["kind"] in ("paid", "recheck") and decision.get("payments_enabled") and total and expected_price != total:
        # the user saw a different price (for example the revision grew into a larger tier): never charge silently
        raise ApiError(409, "price_changed", f"The price of this check is {total} so'm. Check the price and confirm again.")
    try:
        await run_in_threadpool(entitlements.reserve, user, job, decision, identities)
    except entitlements.EntitlementError as exc:
        raise ApiError(exc.status, exc.code, str(exc))
    charge_id = None
    try:
        if total and decision.get("payments_enabled"):
            try:
                charge_id = await run_in_threadpool(payments.charge, user, total, f"{decision['kind']} check, "
                                                    f"{decision['words']} words" + (", AI detector" if ai_check else ""))
            except payments.PaymentError as exc:
                raise ApiError(402, exc.code, str(exc))
            if charge_id:
                await run_in_threadpool(reports_store.charged, job, charge_id, total)
        result = await analyse(submission, reference, reference_text, language, mode, options, web_search, ai_check,
                               auth.owner_id(user), deposit, original, uz_ai_for(user))
    except BaseException:
        await run_in_threadpool(entitlements.release, job)
        if charge_id:
            await run_in_threadpool(payments.refund, user, charge_id, total, "check failed")
        raise
    charged = total
    if charge_id:
        unused = unused_ai_price(result, decision["words"], ai_check) if mode != "ai" else 0
        nothing = (total - decision["ai_price_uzs"]) if mode != "ai" and result.get("percent") is None else 0
        if mode == "ai" and not _succeeded(result, mode):
            nothing = total
        back = min(total, unused + nothing)
        if back:
            await run_in_threadpool(payments.refund, user, charge_id, back, "part of the check did not run")
        charged = total - back
    if _succeeded(result, mode):
        decision["charged_uzs"] = charged if charge_id else 0
        await run_in_threadpool(entitlements.consume, job, user, decision, original, identities, mode != "ai")
    else:
        await run_in_threadpool(entitlements.release, job)       # nothing was compared: the trial/recheck stays
    exports = decision["kind"] != "trial"
    result["entitlement"] = {"kind": decision["kind"], "tier": decision["tier"], "exports": exports,
                             "consumed": _succeeded(result, mode), "price_uzs": charged if charge_id else 0,
                             "future_price_uzs": decision.get("future_total_uzs"),
                             "recheck_of": (decision.get("recheck") or {}).get("root")}
    if mode != "ai":
        tier = pricing.tier(decision["tier"]) or {}
        result["plan"] = {"id": decision["tier"], "words": decision["words"], "max_words": tier.get("max_words"),
                          "price_uzs": tier.get("price_uzs"), "fits": True}
    if charge_id:
        result["charged_uzs"] = charged
    response = finish(request, result, job, session)
    await run_in_threadpool(metrics.record_check, result, time.monotonic() - (started or time.monotonic()),
                            result.get("charged_uzs"))
    report_id = response.headers.get("x-report-id", "")
    await run_in_threadpool(auth.log_check, user, result, report_id)
    if charge_id:
        await run_in_threadpool(payments.attach_report, charge_id, report_id)
    return response


def num_words(n):
    return f"{n:,}"


async def _run_check(request, user, anonymous, submission, reference, reference_text, language, mode, options,
                     web_search, ai_check, deposit, job=None, session=None, started=None):
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
        if charge_id and job:
            await run_in_threadpool(reports_store.charged, job, charge_id, price)
    try:
        result = await analyse(submission, reference, reference_text, language, mode, options, web_search, ai_check,
                               auth.owner_id(user), deposit, original, uz_ai_for(user))
    except BaseException:
        if charge_id:
            await run_in_threadpool(payments.refund, user, charge_id, price, "check failed")
        raise
    if charge_id:
        unused = unused_ai_price(result, words, ai_check)
        if unused:
            await run_in_threadpool(payments.refund, user, charge_id, unused, "AI detector did not run")
        compared_nothing = unused_check_price(result, mode, price, words, ai_check)
        if compared_nothing:
            await run_in_threadpool(payments.refund, user, charge_id, compared_nothing, "nothing could be compared")
        result["charged_uzs"] = price - unused - compared_nothing
    response = finish(request, result, job, session)
    await run_in_threadpool(metrics.record_check, result, time.monotonic() - (started or time.monotonic()),
                            result.get("charged_uzs"))
    if user:
        await run_in_threadpool(auth.log_check, user, result, response.headers.get("x-report-id", ""))
    if charge_id:
        await run_in_threadpool(payments.attach_report, charge_id, response.headers.get("x-report-id", ""))
    return response


def unused_check_price(result, mode, price, words, ai_check):
    """The plagiarism part of a price when no percentage could be given (search failed, or no comparable source):
    the user is not charged for a check that compared nothing. An incomplete check that did compare sources
    ("at least X%") is charged."""
    if mode == "ai" or result.get("percent") is not None:
        return 0
    return max(0, price - (pricing.ai_price(words) if ai_check else 0))


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
        progress.step("library")
        summary = summarize(await run_check(original, language, [hashlib.sha256(original.encode()).hexdigest()]))
        if summary["total_chars"] != len(original):
            raise EngineError("Engine returned inconsistent document length.")
    except EngineError as exc:
        return {"searched": False, "error": exc.code}
    for source in summary["sources"]:
        mark_own(source, owner)
    for key in ("translated", "paraphrased"):
        if key in summary:
            result[key] = own_translated(summary[key], owner, original)
    result["sources"] = summary["sources"] + result["sources"]
    intervals = corpus.merge([list(i) for i in result.get("intervals", [])] + [list(i) for i in summary["intervals"]])
    result["exact_intervals"] = corpus.merge([list(i) for i in result.get("exact_intervals", [])]
                                             + [list(i) for i in summary.get("exact_intervals", summary["intervals"])])
    q = corpus.lexical(original, language)
    matched = corpus.words_in_intervals(q, intervals)
    result.update(intervals=intervals, matched_tokens=matched, matched_chars=sum(e - s for s, e in intervals))
    # The check counts as evaluated if either the OpenAlex texts or the library had text in this language.
    if result.get("percent") is not None or summary["language_counts"].get(language, 0):
        result["percent"] = _percent(matched, len(q))
        result.pop("not_evaluated_reason", None)
    return {"searched": True, "size": summary["corpus_size"], "language": language,
            "language_size": summary["language_counts"].get(language, 0),
            "collections": (summary.get("collection_counts") or {}).get(language),
            "matched_sources": sum(1 for s in summary["sources"] if s["matched_tokens"])}


def ai_highlight(text, passages):
    """HTML of the document with AI-written (class ai-a) and AI-assisted (class ai-s) passages marked, from the
    detector's character offsets; passages without valid offsets are left out."""
    spans = sorted((p["start"], p["end"], "ai-a" if p.get("label") == "ai" else "ai-s") for p in passages
                   if type(p.get("start")) is int and type(p.get("end")) is int and 0 <= p["start"] < p["end"] <= len(text))
    out, pos = [], 0
    for start, end, cls in spans:
        start = max(start, pos)
        if start >= end:
            continue
        out.append(html.escape(text[pos:start]))
        out.append(f'<mark class="{cls}">{html.escape(text[start:end])}</mark>')
        pos = end
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def uz_ai_for(user):
    """Whether the Uzbek AI-writing indicator runs for this account (uzai.visible_to: everyone when UZ_AI_DETECT=on;
    in the default preview only site owners and accounts with a pilot allowance)."""
    try:
        return uzai.visible_to(user, lambda u: entitlements.grants_for(u["id"]))
    except Exception:
        return False


def own_translated(items, owner, original):
    """Possible translated reuse for the result: the user's own archived works are labelled, and the manuscript side of
    each sentence pair is added (shown on the page only, like other excerpts)."""
    out = []
    for item in items:
        mine = bool(owner and item.get("owner") and item["owner"] == owner)
        out.append({**{k: v for k, v in item.items() if k != "owner"}, "own": mine,
                    "pairs": [{**p, "query_excerpt": original[p["query_start"]:p["query_end"]][:500]} for p in item["pairs"]]})
    return out


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
                  owner="", archive=None, original=None, uz_ai=False):
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
            raise ApiError(413, "reference_too_long", f"Reference exceeds the {MAX_CHARS:,}-character limit")
        require_tokens(source, language, "reference_too_short",
                       "The reference needs at least 7 words of readable text.")
        progress.step("compare")
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
        pdf_title = (getattr(submission, "ocr_info", None) or {}).get("pdf_title")
        progress.step("discover")
        discovery = await run_in_threadpool(discover, original, 8, language, pdf_title)
        import discovery as finder
        stems = await run_in_threadpool(finder.document_stems, original, language)
        for source in discovery["sources"]:            # topic relevance of each OpenAlex result (title + abstract)
            source["relevance"] = finder.relevance(source, stems, language)
        # Full-text slots go to the most relevant results; clearly off-topic ones are never downloaded.
        ranked = sorted((s for s in discovery["sources"] if not finder.off_topic(s)),
                        key=lambda s: -(s["relevance"] if s["relevance"] is not None else finder.MIN_RELEVANCE))
        enabled, max_works, seconds, paid = fulltext.settings()
        fetched = {"attempted": 0, "compared": 0, "failed": 0, "timed_out": 0}
        if enabled and ranked:
            # Transient: full texts are compared for this check only and discarded with `discovery`.
            progress.step("fulltext", min(len(ranked), max_works))
            fetched = await run_in_threadpool(fulltext.fetch_fulltexts, ranked, max_works, seconds, paid)
        # Why general web pages were (not) searched, stated next to the result so a low percentage is not read as
        # "nothing was copied from the internet" (checkstatus.web_note).
        web = {"enabled": False, "reason": "not_set_up" if not websearch.settings()[0] else "not_chosen"}
        if web_search and websearch.settings()[0]:
            # Separate consent: the same excerpts are sent to the search providers; pages are compared once and discarded.
            from discovery import spread_excerpts
            excerpts = spread_excerpts(original, websearch.excerpt_count(pricing.count_words(original)), pdf_title)
            progress.step("web")
            web_sources, web = await run_in_threadpool(websearch.run, excerpts, language)
            known = {s.get("url") for s in discovery["sources"]}
            discovery["sources"] = discovery["sources"] + [s for s in web_sources if s["url"] not in known]
        progress.step("compare")
        result = await run_in_threadpool(compare_sources, original, discovery["sources"], language)
        evaluated = sum(bool(s.get("abstract") or s.get("fulltext")) for s in discovery["sources"])
        result["fulltext"] = fetched
        result["web"] = web
        if not evaluated:
            result["percent"] = None
            result["not_evaluated_reason"] = "provider_unavailable" if discovery["status"] not in ("searched", "partial") else "no_abstracts"
        result["evaluated_sources"] = evaluated
        # Off-topic results that matched nothing are compared but not listed (a match is always listed).
        # The count of compared abstracts is kept first, so QR, certificate and PDF still include them.
        result["abstract_count"] = sum(1 for e in result["sources"] if e.get("coverage") == "abstract")
        listed = [e for e in result["sources"] if e.pop("off_topic", False) is False or e["matched_tokens"]]
        result["off_topic_hidden"] = len(result["sources"]) - len(listed)
        result["sources"] = listed
        result["library"] = await search_library(result, original, language, owner)
        result["matched_source_count"] = sum(1 for e in result["sources"] if e["matched_tokens"])
        result.update({"mode":"academic","reference":"None","source_count":len(discovery["sources"]),
                       "database_status":discovery["status"],"database_message":discovery["message"],
                       "queries":discovery["queries"],"failed_queries":discovery.get("failed_queries", 0),
                       "search_phrases":discovery.get("query_texts", []),
                       "chapters":discovery.get("chapters", 0),"duplicates_merged":discovery.get("duplicates_merged", 0),
                       "self_comparison":False,
                       "scope":"OpenAlex discovery; exact matching against open-access full texts fetched for this check "
                                "(not stored) where available, otherwise abstracts. "
                                + (f"Web pages found with {' and '.join(web.get('providers') or ['web search'])} for the same excerpts were also compared (not stored)."
                                   if web.get("enabled") else "No web-wide plagiarism check.")})
    else:
        try:
            progress.step("library")
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
            "exact_intervals":summary.get("exact_intervals", summary["intervals"]) if evaluated else [],
        }
        for key in ("translated", "paraphrased"):
            if key in summary:
                result[key] = own_translated(summary[key], owner, original)
    if detected and detected != chosen:
        result["language_note"] = {"selected": chosen, "detected": detected}
    intervals = result.pop("intervals", [])
    exact_intervals = result.pop("exact_intervals", intervals)
    options = dict(options or {})
    template = options.pop("template", None)
    if template and result.get("percent") is not None:
        # Wording the student's university requires (title page, approval sheet ...): excluded as template text.
        options["template_spans"] = await run_in_threadpool(templates.spans, original, template, language)
        options["template_name"] = template
    # Kept with the report (never sent to the browser) so the owner can exclude sources and recalculate later.
    result["_intervals"], result["_exact"], result["_options"] = intervals, exact_intervals, options
    if result.get("percent") is not None:
        # Matched words found only as edited copying (words replaced, inserted or deleted), reported separately.
        lex = corpus.lexical(original, language)
        result["edited_tokens"] = max(0, corpus.words_in_intervals(lex, intervals) - corpus.words_in_intervals(lex, exact_intervals))
    if result.get("translated") or result.get("paraphrased"):   # sentence pairs are rebuilt from the stored text (reopen, PDF)
        result["_text"] = original
    result["highlight_html"] = ""
    if result.get("percent") is not None:
        progress.step("report")
        review = await run_in_threadpool(citations.assess, original, language, intervals, **(options or {}))
        marks = marks_for(original, review.pop("segments"), result.get("sources"))
        result["highlight_html"] = highlight(original, None, marks=marks)
        # For the PDF appendix only: kept with the report in memory (see finish), never sent twice or stored.
        result["_text"], result["_marks"] = original, [list(m) for m in marks]
        result["citation"] = review
        result["percent_all"] = review["percent_all"]
        result["percent"] = review["percent_counted"]
        result["matched_tokens_counted"] = review["matched_words_counted"]
    # Writing-integrity signals (not AI detection). DOIs are looked up only with the academic consent.
    result["integrity"], result["integrity_examples"] = await run_in_threadpool(
        integrity.assess, original, mode == "academic")
    words = pricing.count_words(original)
    if archive:
        progress.step("archive")
        result["archive"] = await deposit_work(original, language, owner, archive, submission.filename)
    if ai_check:
        # Separate consent: the full text goes to Pangram; flagged passages stay out of the cache and PDF.
        progress.step("ai")
        result["ai"], result["ai_passages"] = await run_in_threadpool(aidetect.detect, original, language, words)
        if result["ai"].get("status") == "done":
            # The whole document with Pangram's AI and AI-assisted passages marked, for the page only (like the
            # flagged passages it is never cached, stored or put in the PDF).
            result["ai_highlight_html"] = ai_highlight(original, result["ai_passages"])
    if uz_ai and language == "uz":
        # PlagIQ's own Uzbek AI-writing indicator (experimental; uzai.py). Runs on this server, so no consent or
        # third party is involved. Its counts go into the report (PDF, not the certificate); the highlighted document
        # is shown on the page only, never cached or stored.
        progress.step("ai")
        result["ai_uz"], flagged = await run_in_threadpool(uzai.assess, original)
        if flagged:
            result["ai_uz_html"] = ai_highlight(original, flagged)
    for source in result.get("sources") or []:
        for passage in source.get("passages") or []:
            qs, qe = passage.get("query_start"), passage.get("query_end")
            if type(qs) is int and type(qe) is int and 0 <= qs < qe <= len(original):
                passage["query_excerpt"] = original[qs:qe][:600] + ("…" if qe - qs > 600 else "")
    result["plan"] = pricing.quote(words)
    ocr = getattr(submission, "ocr_info", None) or {}
    if ocr.get("ocr_pages"):
        result["ocr"] = {"pages": ocr["ocr_pages"], "models": ocr.get("ocr_models", ""), "seconds": ocr.get("ocr_seconds")}
    if getattr(submission, "document_meta", None):
        result["document_meta"] = submission.document_meta
    result.update({"document":clean_filename(submission.filename, "Document"),
                   "date":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "quotations_excluded":False, "bibliography_excluded":False,
                   "sha256":hashlib.sha256(original.encode()).hexdigest()})
    return checkstatus.assess(result)


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


def marks_for(text, segments, sources=None):
    """[(start, end, class, source rank or None)] for each highlighted stretch, in text order:
    the same marks the page shows and the PDF appendix prints."""
    ranked = source_ranks(sources)
    out, pos = [], 0
    for start, end, where, reason in segments:
        if start < pos:
            continue
        cls = MARK_CLASS[where] + (" x" if reason else "")
        rank = next((r for r, spans in ranked if any(s < end and start < e for s, e in spans)), None)
        out.append((start, end, cls, rank))
        pos = end
    return out


def highlight(text, segments, sources=None, marks=None):
    """Escaped manuscript with matched words wrapped in <mark>; class names and a numeric
    data-s (source rank) only, no user markup."""
    out, pos = [], 0
    for start, end, cls, rank in (marks if marks is not None else marks_for(text, segments, sources)):
        out.append(html.escape(text[pos:start]))
        attr = f' data-s="{min(rank, SOURCE_COLOURS)}"' if rank is not None else ""
        out.append(f'<mark class="{cls}"{attr}>' + html.escape(text[start:end]) + "</mark>")
        pos = end
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


# Shown to the user once and never kept with the report. The manuscript itself ("_text", "_marks") is kept with
# the report for REPORT_HOURS (24 h, reports_store.py) for the PDF appendix and for reopening the report from
# "My checks"; it is deleted with the report.
# The Uzbek indicator's counts (ai_uz) are kept with the report for the PDF; its highlighted document stays page-only.
PRIVATE_FIELDS = ("highlight_html", "search_phrases", "integrity_examples", "ai_passages", "ai_highlight_html",
                  "ai_uz_html")


def finish(request, result, identifier=None, session=None):
    identifier = identifier or uuid.uuid4().hex
    while len(results) >= MAX_REPORTS:
        results.pop(next(iter(results)))
    owner = session or (request.cookies.get("plagiq_session") if request is not None else None) or secrets.token_urlsafe(32)
    response = JSONResponse({"report_id": identifier, **{k: v for k, v in result.items() if not k.startswith("_")}},
                            headers={"x-report-id": identifier})
    cached = {k:v for k,v in result.items() if k not in PRIVATE_FIELDS}
    for key in ("translated", "paraphrased"):   # likewise the manuscript side of translated / paraphrased sentence pairs
        if cached.get(key):
            cached[key] = [{**t, "pairs": [{k: v for k, v in p.items() if k != "query_excerpt"} for p in t["pairs"]]}
                           for t in cached[key]]
    if cached.get("sources"):           # manuscript excerpts of the side-by-side view are never cached
        cached["sources"] = [{**s, "passages": [{k: v for k, v in p.items() if k != "query_excerpt"}
                                                for p in s.get("passages") or []]} for s in cached["sources"]]
    results[identifier] = {"owner": owner, "expires": time.monotonic() + REPORT_TTL, "result": cached}
    try:
        reports_store.finish(identifier, cached)          # kept for REPORT_HOURS (24 h), then deleted
    except sqlite3.Error as exc:
        logging.getLogger("plagiq.check").error("report not stored: %s", type(exc).__name__)
    if request is not None:                 # None: run by the server after a payment (pending_checks.py)
        response.set_cookie("plagiq_session", owner, max_age=reports_store.ttl(),
                            httponly=True, secure=_secure(request), samesite="strict")
    return response


def owned(identifier, request):
    """The report, for the browser session that ran it or the signed-in account it belongs to."""
    purge_reports()
    session = request.cookies.get('plagiq_session', '')
    entry = results.get(identifier)
    if entry is not None and secrets.compare_digest(entry['owner'].encode(), session.encode()):
        return {**entry, "expires": time.monotonic() + _stored_seconds_left(identifier, entry["expires"] - time.monotonic())}
    stored = reports_store.get(identifier) if re.fullmatch(r"[0-9a-f]{32}", identifier or "") else None
    user = auth.read(request.cookies.get(auth.COOKIE))
    mine = stored and (secrets.compare_digest(stored["owner"].encode(), session.encode())
                       or (user is not None and stored["user"] == user["id"]))
    if not mine or stored["state"] != "done" or not stored["result"]:
        raise ApiError(404, "report_unavailable", "Report unavailable or expired")
    return {"owner": stored["owner"], "expires": time.monotonic() + max(0, stored["expires"] - time.time()),
            "result": stored["result"]}


def no_exports(result):
    """The free trial shows its result on screen only: no PDF, no QR certificate, no CSV (checked here, on the
    server, not only by hiding buttons)."""
    if (result.get("entitlement") or {}).get("exports") is False:
        raise ApiError(403, "trial_no_exports", "The free check shows its result on screen only. A paid check includes "
                                                "the PDF report and the certificate.")


def _stored_seconds_left(identifier, fallback):
    try:
        stored = reports_store.get(identifier)
    except sqlite3.Error:
        stored = None
    return max(0, stored["expires"] - time.time()) if stored else fallback


@app.get("/api/report/{identifier}/certificate.pdf")
def report_certificate(identifier: str, request: Request, lang: str = "en"):
    """The one-page certificate as its own PDF (same figures and QR code as page 1 of the report)."""
    entry = owned(identifier, request)
    no_exports(entry["result"])
    base = os.getenv("PUBLIC_ORIGIN", "").strip() or str(request.base_url)
    lang = lang if lang in ("uz", "uzc", "ru", "en") else "en"
    content = build_certificate_pdf(entry["result"], identifier, lang, verify.link(entry["result"], identifier, base))
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="PlagIQ-certificate.pdf"'})


SAMPLE_ID = "5a4d91e0c0de"                  # shown on the sample only; never a real report number


@app.get("/api/sample-report.pdf")
def sample_report_pdf(lang: str = "uz", full: int = 0):
    """The sample's report as a PDF: fictional document and sources, marked SAMPLE, with no verification link."""
    lang = lang if lang in ("uz", "uzc", "ru", "en") else "uz"
    data = {**sample_report.build(), "sample": True}
    content = (build_pdf(data, SAMPLE_ID, lang, 15, None) if full else build_short_pdf(data, SAMPLE_ID, lang, None))
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="PlagIQ-sample-{"full-" if full else ""}report.pdf"',
        "Cache-Control": "no-store"})


@app.get("/api/sample-certificate.pdf")
def sample_certificate_pdf(lang: str = "uz"):
    lang = lang if lang in ("uz", "uzc", "ru", "en") else "uz"
    content = build_certificate_pdf({**sample_report.build(), "sample": True}, SAMPLE_ID, lang, None)
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="PlagIQ-sample-certificate.pdf"', "Cache-Control": "no-store"})


@app.get("/api/report/{identifier}.pdf")
def report(identifier:str, request:Request, lang:str="en", full:int=0):
    """Short report by default (certificate + one page of matches); full=1 for all evidence and the
    whole document with matches highlighted."""
    entry = owned(identifier, request)
    no_exports(entry["result"])
    remaining = max(0, int((entry["expires"] - time.monotonic()) // 60))
    if remaining > 3650 * 1440:                 # an account's report, kept until deleted
        remaining = None
    base = os.getenv("PUBLIC_ORIGIN", "").strip() or str(request.base_url)
    lang = lang if lang in ("uz", "uzc", "ru", "en") else "en"
    link = verify.link(entry["result"], identifier, base)
    if full:
        content, name = build_pdf(entry["result"], identifier, lang, remaining, link), "PlagIQ-full-report.pdf"
    else:
        content, name = build_short_pdf(entry["result"], identifier, lang, link), "PlagIQ-report.pdf"
    return Response(content, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/api/report/{identifier}")
def delete_report(identifier: str, request: Request):
    owned(identifier, request)
    results.pop(identifier, None)
    reports_store.delete(identifier)
    return {"deleted": True}


def _subtract(a, b):
    """Character intervals of a that are not in b (both merged lists)."""
    out, k = [], 0
    for start, end in a:
        pos = start
        while k < len(b) and b[k][1] <= pos:
            k += 1
        j = k
        while j < len(b) and b[j][0] < end:
            if b[j][0] > pos:
                out.append([pos, b[j][0]])
            pos = max(pos, b[j][1])
            j += 1
        if pos < end:
            out.append([pos, end])
    return out


def _intersect(a, b):
    return _subtract(a, _subtract(a, b))


def exclude_sources(result, chosen):
    """Recalculate a stored report without the sources at the given indices (the owner's choice; an empty list
    restores the original). Words matched only by excluded sources stop counting; words that another source also
    matched still count; words matched by sources beyond the listed ones always count. The quotation, bibliography
    and short-match options of the check are applied again. The original figures stay in "before_source_exclusion"."""
    text, all_spans = result.get("_text"), result.get("_intervals")
    sources = result.get("sources") or []
    if text is None or all_spans is None:
        raise ApiError(409, "exclusion_unavailable", "This report cannot be recalculated.")
    chosen = sorted({i for i in chosen if type(i) is int and 0 <= i < len(sources)})
    if any(not sources[i].get("spans") or not sources[i].get("matched_tokens") for i in chosen):
        raise ApiError(400, "source_not_excludable", "This source has no complete match positions and cannot be excluded.")
    original = result.get("before_source_exclusion") or {k: result.get(k) for k in (
        "percent", "percent_all", "matched_tokens", "matched_tokens_counted", "matched_chars", "edited_tokens", "citation",
        "status", "coverage_issues")}
    excluded = corpus.merge([list(x) for i in chosen for x in sources[i]["spans"]])
    kept = corpus.merge([list(x) for i, src in enumerate(sources) if i not in chosen for x in src.get("spans") or []])
    remaining = _subtract(corpus.merge([list(x) for x in all_spans]), _subtract(excluded, kept))
    language = result.get("language", "en")
    lex = corpus.lexical(text, language)
    review = citations.assess(text, language, remaining, **(result.get("_options") or {}))
    shown = [{**src, "spans": [], "passages": []} if i in chosen else src for i, src in enumerate(sources)]
    marks = marks_for(text, review.pop("segments"), shown)
    exact = _intersect(remaining, corpus.merge([list(x) for x in result.get("_exact") or all_spans]))
    out = dict(result)
    out.update(matched_tokens=corpus.words_in_intervals(lex, remaining), matched_chars=sum(e - s for s, e in remaining),
               citation=review, percent_all=review["percent_all"], percent=review["percent_counted"],
               matched_tokens_counted=review["matched_words_counted"],
               edited_tokens=max(0, corpus.words_in_intervals(lex, remaining) - corpus.words_in_intervals(lex, exact)),
               _marks=[list(m) for m in marks],
               sources=[{**src, "excluded": i in chosen} for i, src in enumerate(sources)])
    if chosen:
        out["before_source_exclusion"] = original
        out["excluded_sources"] = [{"index": i, "title": sources[i].get("title", ""), "words": sources[i].get("matched_tokens", 0)}
                                   for i in chosen]
    else:                                              # restore the check exactly as it was
        for key in ("percent", "percent_all", "matched_tokens", "matched_tokens_counted", "matched_chars", "edited_tokens",
                    "citation", "status", "coverage_issues"):
            out[key] = original.get(key)
        out.pop("before_source_exclusion", None)
        out.pop("excluded_sources", None)
        return out
    checkstatus.assess(out)
    return out


@app.post("/api/report/{identifier}/exclude")
async def exclude_report_sources(identifier: str, request: Request):
    """The report's owner excludes sources (for example their own earlier work or an approved template) and gets
    the recalculated report; {"sources": []} restores the original. PDFs and the QR code follow the choice."""
    entry = owned(identifier, request)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("sources"), list) or len(body["sources"]) > 200:
        raise ApiError(400, "invalid_request", "Send the list of source numbers to exclude.")
    updated = await run_in_threadpool(exclude_sources, entry["result"], body["sources"])
    if identifier in results:
        results[identifier]["result"] = updated
    await run_in_threadpool(reports_store.finish, identifier, updated)
    return report_json(identifier, request)


@app.get("/api/report/{identifier}/sources.csv")
def report_sources_csv(identifier: str, request: Request):
    """The source list as a spreadsheet (UTF-8 with BOM, so Excel shows Cyrillic and Uzbek letters)."""
    result = owned(identifier, request)["result"]
    no_exports(result)
    words = result.get("total_tokens") or 0
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Rank", "Title", "Link or DOI", "Year", "Coverage", "Matched words", "Share of document %",
                     "Edited-copy words", "Passages", "Excluded by you"])
    order = sorted(range(len(result.get("sources") or [])), key=lambda i: -(result["sources"][i].get("matched_tokens") or 0))
    safe = lambda v: ("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v   # no spreadsheet formulas
    for rank, i in enumerate(order, 1):
        src = result["sources"][i]
        writer.writerow([rank, safe(src.get("title") or ""), safe(src.get("url") or ""), src.get("year") or "",
                         src.get("coverage") or "", src.get("matched_tokens") or 0,
                         f"{100 * (src.get('matched_tokens') or 0) / words:.2f}" if words else "",
                         src.get("edited_tokens") or 0, src.get("passage_count") or 0, "yes" if src.get("excluded") else ""])
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="PlagIQ-sources.csv"', "Cache-Control": "no-store"})


@app.get("/api/progress/{token}")
def check_progress(token: str):
    """The steps a running check has passed through (see progress.py). Only the browser that chose the random
    token knows it; no document text is included."""
    state = progress.read(token) if progress.TOKEN.fullmatch(token or "") else None
    if state is None:
        raise ApiError(404, "report_unavailable", "Not found")
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


@app.get("/api/my-checks")
async def my_checks(request: Request):
    """The signed-in account's checks: type, words, date, result, which reports can still be opened or downloaded,
    recheck eligibility and expiry, rechecks left and payment status; plus the free-trial state and retention rules.
    Only the account's own records (by its ID on the server)."""
    user = auth.read(request.cookies.get(auth.COOKIE))
    if not user:
        raise ApiError(401, "login_required", "Sign in first.")
    identities = await run_in_threadpool(auth.identities, user)
    summary = await run_in_threadpool(entitlements.summary, user, identities)
    log = await run_in_threadpool(auth.history, user, 100)
    stored = {r["id"]: r for r in await run_in_threadpool(reports_store.listing, "", user["id"])}
    paid = {c["job"]: c for c in summary["paid_checks"]}
    now = time.time()
    rows = []
    for entry in log:
        job = entry.get("job") or ""
        report = stored.get(job)
        open_report = bool(report and report["state"] == "done")
        kind = entry.get("kind") or "legacy"
        pc = paid.get(job) or {}
        root = paid.get(entry.get("recheck_of") or "") or {}
        rows.append({**{k: entry.get(k) for k in ("date", "mode", "words", "percent", "status", "tier", "recheck_of")},
                     "kind": kind, "report_id": job if open_report else None,
                     "reports": ({"screen": True, "pdf": kind != "trial", "certificate": kind != "trial",
                                  "csv": kind != "trial"} if open_report else {}),
                     "report_expires": datetime.fromtimestamp(report["expires"], timezone.utc).isoformat(timespec="seconds")
                     if open_report else None,
                     "recheck_until": datetime.fromtimestamp(pc["recheck_until"], timezone.utc).isoformat(timespec="seconds")
                     if pc.get("recheck_until") and pc["recheck_until"] > now else None,
                     "rechecks_left": pc.get("rechecks_left") if pc.get("recheck_until") else None,
                     "recheck_root_until": datetime.fromtimestamp(root["recheck_until"], timezone.utc).isoformat(timespec="seconds")
                     if root.get("recheck_until") else None,
                     "payment": {"trial": "free", "pilot": "pilot", "paid": "paid", "recheck": "paid" if entry.get("amount")
                                 else "pilot"}.get(kind, "none"), "amount_uzs": entry.get("amount")})
    return JSONResponse({"trial": summary["trial"], "trial_max_words": summary["trial_max_words"],
                         "pilot_checks_left": sum(g["remaining"] for g in summary["grants"]),
                         "payments_enabled": payments.enabled(), "checks": rows,
                         "retention": {"report_hours": reports_store.hours(), "account_days": reports_store.account_days(), "recheck_days": pricing.recheck_days(),
                                       "recheck_limit": pricing.recheck_limit()}},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/estimate")
async def check_estimate(mode: str = "academic", ai: bool = False):
    """Roughly how many minutes a check of this kind takes (metrics.estimate): shown while it runs."""
    if mode not in ("reference", "academic", "corpus", "ai"):
        raise ApiError(400, "invalid_mode", "Unknown check type.")
    found = await run_in_threadpool(metrics.estimate, mode, ai)
    if active_scans >= scan_capacity():        # a new check waits in line: add the rounds ahead of it
        rounds = math.ceil((len(_waiting) + 1) / scan_capacity())
        found = {**found, "minutes": min(120, found["minutes"] * (1 + rounds)), "queue": len(_waiting) + 1}
    return found


@app.get("/api/reports")
def my_reports(request: Request):
    """Checks of the last REPORT_HOURS for this browser session and, when signed in, this account."""
    user = auth.read(request.cookies.get(auth.COOKIE))
    rows = reports_store.listing(request.cookies.get("plagiq_session", ""), user["id"] if user else None)
    return {"hours": reports_store.hours(), "reports": [
        {**r, "created": datetime.fromtimestamp(r["created"], timezone.utc).isoformat(timespec="seconds"),
         "expires": datetime.fromtimestamp(r["expires"], timezone.utc).isoformat(timespec="seconds")} for r in rows]}


@app.get("/api/report/{identifier}")
def report_json(identifier: str, request: Request):
    """Reopen a stored report: the result as first shown, rebuilt from the stored text and marks (the AI detector's
    flagged passages and integrity examples are shown once only and are not part of it)."""
    entry = owned(identifier, request)
    result = dict(entry["result"])
    text, marks = result.get("_text"), result.get("_marks")
    if text is not None and marks is not None:
        result["highlight_html"] = highlight(text, None, marks=[tuple(m) for m in marks])
        result["sources"] = [{**src, "passages": [
            {**p, "query_excerpt": text[p["query_start"]:p["query_end"]][:600] + ("…" if p["query_end"] - p["query_start"] > 600 else "")}
            if type(p.get("query_start")) is int and type(p.get("query_end")) is int and 0 <= p["query_start"] < p["query_end"] <= len(text)
            else p for p in src.get("passages") or []]} for src in result.get("sources") or []]
    for key in ("translated", "paraphrased"):
        if text is not None and result.get(key):
            result[key] = [{**t, "pairs": [
                {**p, "query_excerpt": text[p["query_start"]:p["query_end"]][:500]}
                if type(p.get("query_start")) is int and type(p.get("query_end")) is int and 0 <= p["query_start"] < p["query_end"] <= len(text)
                else p for p in t["pairs"]]} for t in result[key]]
    return {"report_id": identifier, "reopened": True, **{k: v for k, v in result.items() if not k.startswith("_")}}
