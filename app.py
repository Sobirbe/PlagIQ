import asyncio
from contextlib import asynccontextmanager
import hashlib
import html
import io
import os
import re
import time
import uuid
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import fitz
from docx import Document
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

from discovery import discover
from engine_client import EngineError, configured as corpus_configured, run_check, summarize, highlight_html
from textfold import fold
from security import SecurityMiddleware

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

app = FastAPI(title="PlagIQ Pilot", version="0.4", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
results = {}
REPORT_TTL = 900

def purge_reports():
    now = time.monotonic()
    for key in list(results):
        if results[key]["expires"] <= now:
            del results[key]

RATE = {}
MAX_BYTES = 2 * 1024 * 1024
CYR = dict(zip("абвгдежзийклмнопрстуфхчшэюяқғўҳ",
               ["a","b","v","g","d","e","j","z","i","y","k","l","m","n","o","p","r","s","t","u","f","x","ch","sh","e","yu","ya","q","g","o","h"]))

def tokens(value, language):
    value = value.lower().replace("ʻ", "'").replace("‘", "'").replace("’", "'")
    if language == "uz":
        value = fold(value)
    return re.findall(r"(?:[^\W_]|[\ue000\ue001])+", value, re.UNICODE)

def compare(text, source, language):
    a, b = tokens(text, language), tokens(source, language)
    n = 7
    windows = {tuple(b[i:i+n]) for i in range(max(0, len(b)-n+1))}
    matched = set()
    for i in range(max(0, len(a)-n+1)):
        if tuple(a[i:i+n]) in windows:
            matched.update(range(i, i+n))
    return {"percent":round(100 * len(matched)/len(a), 2) if a else 0,
            "matched_tokens":len(matched), "total_tokens":len(a),
            "language":language, "method":"Exact normalized seven-token windows",
            "ai_detection":"Not performed"}

def compare_sources(text, sources, language):
    """Union of matched document-token positions across readable abstracts."""
    manuscript = tokens(text, language)
    n = 7
    covered = set()
    entries = []
    for source in sources:
        abstract = source.get("abstract", "")
        lexical = tokens(abstract, language)
        windows = {tuple(lexical[i:i+n]) for i in range(max(0,len(lexical)-n+1))}
        positions = set()
        if windows:
            for i in range(max(0,len(manuscript)-n+1)):
                if tuple(manuscript[i:i+n]) in windows:
                    positions.update(range(i, i+n))
        covered.update(positions)
        entries.append({"title":source.get("title", "Untitled"),"url":source.get("url", ""),
                        "year":source.get("year"),"coverage":source.get("coverage", "metadata_only"),
                        "matched_tokens":len(positions)})
    return {"percent":round(100 * len(covered)/len(manuscript),2) if manuscript else 0,
            "matched_tokens":len(covered),"total_tokens":len(manuscript),
            "language":language, "method":"Exact normalized seven-token windows against available abstracts",
            "sources":entries,"ai_detection":"Not performed"}

async def read_file(upload):
    name = (upload.filename or "").lower()
    if not name.endswith((".txt", ".docx", ".pdf")):
        raise HTTPException(400, "Only TXT, DOCX and text-based PDF supported")
    content = await upload.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "File exceeds 2 MB")
    try:
        if name.endswith(".txt"):
            return content.decode("utf-8-sig")
        if name.endswith(".docx"):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if (len(archive.infolist()) > 1000 or
                        sum(item.file_size for item in archive.infolist()) > 20 * 1024 * 1024):
                    raise HTTPException(413, "Expanded DOCX exceeds safe limits")
            doc = Document(io.BytesIO(content))
            return "\n".join([p.text for p in doc.paragraphs] + 
                             [cell.text for table in doc.tables for row in table.rows for cell in row.cells])
        pdf = fitz.open(stream=content, filetype="pdf")
        try:
            if pdf.page_count > 40:
                raise HTTPException(400, "PDF exceeds 40 pages")
            return "\n".join(page.get_text() for page in pdf)
        finally:
            pdf.close()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Could not extract text. Image-only PDFs need OCR.")

def rate_limit(request):
    # Temporary in-memory safeguard; NOT a substitute for production perimeter limits.
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    history = [t for t in RATE.get(key, []) if now-t < 3600]
    if len(history) >= 12:
        raise HTTPException(429, "Hourly pilot scan allowance reached.")
    history.append(now)
    RATE[key] = history
    if len(RATE) > 3000:
        RATE.clear()

@app.get("/health")
def health():
    return {"status":"ok","version":"0.4","corpus_engine_configured":corpus_configured()}

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(Path(__file__).with_name("index.html").read_text("utf-8"), headers={"Cache-Control":"no-store, max-age=0"})

@app.post("/api/check")
async def check(request:Request, submission:UploadFile=File(...),
                reference:UploadFile|None=File(None),reference_text:str=Form(""),
                language:str=Form("uz"),mode:str=Form("reference"),consent:bool=Form(False)):
    purge_reports()
    rate_limit(request)
    if language not in ("uz","ru","en"):
        raise HTTPException(400, "Invalid language")
    if mode not in ("reference","academic","corpus"):
        raise HTTPException(400, "Invalid mode")
    original = await read_file(submission)
    if not original.strip() or not tokens(original, language):
        raise HTTPException(400, "No readable text detected")
    if len(original) > 300000:
        raise HTTPException(413, "Extracted text exceeds pilot limit")
    if mode == "reference":
        source = await read_file(reference) if reference and reference.filename else reference_text
        if not source.strip():
            raise HTTPException(400, "Provide a reference document or text, or choose academic discovery")
        if len(source) > 300000:
            raise HTTPException(413, "Reference exceeds pilot limit")
        result = compare(original, source, language)
        same = hashlib.sha256(original.strip().encode()).digest() == hashlib.sha256(source.strip().encode()).digest()
        result.update({"mode":"reference","reference":reference.filename if reference and reference.filename else "Pasted reference",
                       "sources":[],"database_status":"Not searched","source_count":1,
                       "scope":"One user-provided reference; no public academic databases searched.",
                       "self_comparison":same})
    elif mode == "academic":
        if not consent:
            raise HTTPException(400, "Explicit consent is required before sending a short query excerpt to OpenAlex")
        discovery = await run_in_threadpool(discover, original)
        result = compare_sources(original, discovery["sources"], language)
        if not any(s.get("abstract") for s in discovery["sources"]):
            result["percent"] = None
        result["evaluated_sources"] = sum(bool(s.get("abstract")) for s in discovery["sources"])
        result.update({"mode":"academic","reference":"None","source_count":len(discovery["sources"]),
                       "database_status":discovery["status"],"database_message":discovery["message"],
                       "queries":discovery["queries"],"self_comparison":False,
                       "scope":"OpenAlex metadata discovery, and exact token matching against returned abstracts only. NO full-text publisher corpus or web-wide plagiarism check."})
    else:
        if not corpus_configured():
            raise HTTPException(503, "Private PlagIQ corpus engine is not configured.")
        if not consent:
            raise HTTPException(400, "Explicit consent is required for private corpus processing.")
        try:
            engine_report = await run_check(original, language)
            summary = summarize(engine_report)
            if summary['total_chars'] != len(original):
                raise EngineError("Engine returned inconsistent document length.")
        except EngineError as exc:
            raise HTTPException(503, str(exc))
        result = {
            "mode":"corpus", "reference":"PlagIQ licensed internal corpus",
            "percent":summary["percent"] if summary["language_counts"].get(language, 0) else None,"matched_tokens":None,
            "matched_chars":summary["matched_chars"],"total_chars":summary["total_chars"],
            "total_tokens":len(tokens(original,language)),"language":language,
            "method":"Exact normalized seven-token fingerprint matches in indexed corpus",
            "database_status":"searched","database_message":"Matched against ingested corpus only, not the whole web.",
            "source_count":summary["corpus_size"],"sources":summary["sources"],
            "ai_detection":"Not performed","self_comparison":False,
            "scope":"Internal indexed corpus only. Coverage depends on lawfully ingested documents; search may be partial.",
            "corpus_coverage":summary["coverage"],
            "language_counts":summary["language_counts"],
            "sources_truncated":summary["sources_truncated"],
            "highlight_html":highlight_html(original,summary["intervals"]),
        }
    result.update({"document":(submission.filename or "Document")[:180],
                   "date":datetime.now(timezone.utc).isoformat(),
                   "sha256":hashlib.sha256(original.encode()).hexdigest()})
    identifier = uuid.uuid4().hex
    if len(results) >= 50:
        results.pop(next(iter(results)))
    owner = request.cookies.get("plagiq_session") or secrets.token_urlsafe(32)
    # Do not retain manuscript highlights or raw text in the report cache.
    from fastapi.responses import JSONResponse
    response = JSONResponse({"report_id":identifier, **result})
    results[identifier] = {"owner": owner, "expires": time.monotonic() + REPORT_TTL,
                           "result": {k:v for k,v in result.items() if k != "highlight_html"}}
    response.set_cookie("plagiq_session", owner, max_age=REPORT_TTL,
                        httponly=True, secure=request.url.scheme == "https" or bool(os.getenv("PUBLIC_ORIGIN", "").startswith("https://")), samesite="strict")
    return response

@app.get("/api/report/{identifier}.pdf")
def report(identifier:str, request:Request):
    purge_reports()
    entry = results.get(identifier)
    if entry is None or not secrets.compare_digest(entry['owner'], request.cookies.get('plagiq_session', '')):
        raise HTTPException(404, "Report unavailable or expired")
    result = entry["result"]
    out = io.BytesIO()
    pdf = SimpleDocTemplate(out, pagesize=A4, leftMargin=40, rightMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if Path(font).exists():
        pdfmetrics.registerFont(TTFont("PlagIQUnicode", font))
        for name in ("Normal", "Title", "Heading2", "Heading3"):
            styles[name].fontName = "PlagIQUnicode"
    styles.add(ParagraphStyle(name="SmallDetails", parent=styles["Normal"], fontSize=8, leading=12, spaceAfter=8))
    esc = lambda x: html.escape(str(x))
    story = [
        Paragraph("PlagIQ", styles["Title"]),
        Paragraph("ACADEMIC COMPARISON — EVIDENCE REPORT", styles["Heading2"]),
        Paragraph("Not an accredited originality certificate or determination of plagiarism.", styles["SmallDetails"]),
        Spacer(1, 12), Paragraph("Scope of Examination", styles["Heading3"])
    ]
    summary = [
        ("Document", result["document"]), ("Date (UTC)",result["date"]),
        ("Language mode",result["language"].upper()),("Analysis method",result["method"]),
        ("Coverage",result["scope"]),("Database status",result["database_status"]),
        ("Sources discovered / corpus size",result.get("source_count","N/A")),
        ("Source list", "Showing up to 15 entries; totals may be larger"),
        ("Report expiry", "15 minutes; browser session required"),("Normalized document tokens",result["total_tokens"]),
        ("Matching tokens",result.get("matched_tokens","N/A — character-based corpus matching")),
        ("Matching characters",result.get("matched_chars","N/A")),
        ("Corpus coverage",result.get("corpus_coverage","N/A")),
        ("Matched portion in stated scope",str(result["percent"]) + "%" if result["percent"] is not None else "Not evaluated"),
        ("Self-comparison",str(result["self_comparison"])),
        ("AI writing detector","Not performed"),("SHA-256",result["sha256"])
    ]
    rows = [[Paragraph(esc(k),styles["SmallDetails"]),Paragraph(esc(v),styles["SmallDetails"])] for k,v in summary]
    table = Table(rows, colWidths=[160, 350], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E7F4F2")),
        ("LINEBELOW",(0,0),(-1,-1),0.3,colors.HexColor("#D3DFE3")),
        ("LEFTPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),7)
    ]))
    story += [table, Spacer(1, 16),Paragraph("Sources and Coverage",styles["Heading3"])]
    if not result.get("sources"):
        story.append(Paragraph("No academic sources with usable abstracts were returned, or academic search was not selected.",styles["Normal"]))
    for source in result.get("sources",[])[:15]:
        title = esc(source["title"])
        url = esc(source["url"])
        story.append(Paragraph(title,styles["Normal"]))
        story.append(Paragraph(esc(str(source.get("authors", "")) + " · " + str(source.get("year", ""))), styles["SmallDetails"]))
        story.append(Paragraph(url,styles["SmallDetails"]))
        for passage in source.get('passages', [])[:3]:
            story.append(Paragraph(esc("Query characters " + str(passage.get('query_start')) + "–" + str(passage.get('query_end')) +
                "; source characters " + str(passage.get('source_start')) + "–" + str(passage.get('source_end')) +
                " (zero-based, end exclusive): " + str(passage.get('source_excerpt', ''))), styles['SmallDetails']))
        story.append(Paragraph("Coverage: "+esc(source.get("coverage","unknown"))+"; matching tokens: "+esc(source.get("matched_tokens","N/A"))+"; matching characters: "+esc(source.get("matched_chars","N/A")),styles["SmallDetails"]))
    story += [Spacer(1,12),Paragraph("Important: A metadata-only discovery is not an evaluated full-text source. "
        "A zero match does not demonstrate originality. This report does not evaluate AI-generated writing. "
        "Findings are limited to the stated search coverage.",styles["SmallDetails"])]
    def page_footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#527073"))
        canvas.drawString(40, 24, "PlagIQ | Scope-limited evidence | " + identifier[:12])
        canvas.drawRightString(A4[0]-40, 24, "Page " + str(doc.page))
    pdf.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return Response(out.getvalue(),media_type="application/pdf",
        headers={"Content-Disposition":'attachment; filename="PlagIQ-scope-report.pdf"'})

@app.delete("/api/report/{identifier}")
def delete_report(identifier: str, request: Request):
    purge_reports()
    entry = results.get(identifier)
    if entry is None or not secrets.compare_digest(entry['owner'], request.cookies.get('plagiq_session', '')):
        raise HTTPException(404, "Report unavailable or expired")
    del results[identifier]
    return {"deleted": True}
