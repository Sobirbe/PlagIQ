import io
import re
import html
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
import fitz
from docx import Document
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

app = FastAPI(title="PlagIQ Pilot")
results = {}
CYR = dict(zip("абвгдежзийклмнопрстуфхчшэюяқғўҳ", ["a","b","v","g","d","e","j","z","i","y","k","l","m","n","o","p","r","s","t","u","f","x","ch","sh","e","yu","ya","q","g","o","h"]))

def tokens(value, language):
    value = value.lower().replace("ʻ", "'").replace("‘", "'").replace("’", "'")
    if language == "uz":
        value = "".join({"ъ":"\u0027","ь":"","ё":"yo","ы":"i","ц":"ts","щ":"shch"}.get(ch,CYR.get(ch,ch)) for ch in value)
        value = value.replace("o'", "o").replace("g'", "g")
    return re.findall(r"[^\W_]+", value, re.UNICODE)

def compare(text, source, language):
    a, b = tokens(text,language), tokens(source,language)
    n = 7
    source_windows = {tuple(b[i:i+n]) for i in range(max(0,len(b)-n+1))}
    matched = set()
    for i in range(max(0,len(a)-n+1)):
        if tuple(a[i:i+n]) in source_windows:
            matched.update(range(i,i+n))
    return {"percent":round(100*len(matched)/len(a),2) if a else 0,
      "matched_tokens":len(matched),"total_tokens":len(a),
      "language":language,"method":"Exact normalized seven-token windows",
      "scope":"One user-supplied reference only. No academic databases or internet searched.",
      "ai_detection":"Not performed"}

async def read_file(upload):
    name = (upload.filename or "").lower()
    if not name.endswith((".txt",".docx",".pdf")):
        raise HTTPException(400,"Only TXT, DOCX, and text-based PDF supported")
    content = await upload.read(2*1024*1024+1)
    if len(content)>2*1024*1024: raise HTTPException(413,"File exceeds 2 MB")
    try:
        if name.endswith(".txt"): return content.decode("utf-8-sig")
        if name.endswith(".docx"):
            doc=Document(io.BytesIO(content))
            return "\n".join(p.text for p in doc.paragraphs)
        pdf=fitz.open(stream=content,filetype="pdf")
        try:
            if pdf.page_count > 40: raise HTTPException(400,"PDF exceeds 40 pages")
            return "\n".join(page.get_text() for page in pdf)
        finally: pdf.close()
    except HTTPException: raise
    except Exception: raise HTTPException(400,"Cannot extract readable document text")

@app.get("/health")
def health(): return {"status":"ok"}

@app.get("/",response_class=HTMLResponse)
def home(): return Path(__file__).with_name("index.html").read_text("utf-8")

@app.post("/api/check")
async def check(submission:UploadFile=File(...),reference:UploadFile|None=File(None),
                reference_text:str=Form(""),language:str=Form("uz")):
    if language not in ("uz","ru","en"): raise HTTPException(400,"Invalid language")
    original=await read_file(submission)
    source=await read_file(reference) if reference and reference.filename else reference_text
    if not original.strip() or not source.strip(): raise HTTPException(400,"Manuscript and reference required")
    if len(source)>300000: raise HTTPException(413,"Reference exceeds limit")
    result=compare(original,source,language)
    result.update({"document":submission.filename,"reference":reference.filename if reference and reference.filename else "Pasted reference",
                   "date":datetime.now(timezone.utc).isoformat(),
                   "sha256":hashlib.sha256(original.encode()).hexdigest()})
    identifier=uuid.uuid4().hex
    if len(results)>=50: results.pop(next(iter(results)))
    results[identifier]=result
    return {"report_id":identifier,**result}

@app.get("/api/report/{identifier}.pdf")
def report(identifier:str):
    result=results.get(identifier)
    if result is None: raise HTTPException(404,"Report expired or unavailable")
    out=io.BytesIO()
    doc=SimpleDocTemplate(out)
    styles=getSampleStyleSheet()
    font_path="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if Path(font_path).exists():
        pdfmetrics.registerFont(TTFont("PlagIQUnicode",font_path))
        styles["Normal"].fontName="PlagIQUnicode"
    lines=["PlagIQ — Pilot Similarity Report","Limited source coverage — not an official certificate",
           "Scope of examination"]
    for k,v in result.items(): lines.append(k.replace("_"," ").title()+": "+str(v))
    lines.append("This score measures only seven-token matches against one submitted reference.")
    lines.append("It does not establish originality, plagiarism, AI authorship, or coverage of academic literature.")
    story=[Paragraph(html.escape(line),styles["Normal"]) for line in lines]
    doc.build(story)
    return Response(out.getvalue(),media_type="application/pdf",
      headers={"Content-Disposition":'attachment; filename="PlagIQ-pilot-report.pdf"'})
