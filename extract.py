"""Bounded text extraction shared by the website and the owner-run importer."""
import io
import threading
import os
import struct
import time
import re
import zipfile

import olefile
import pymupdf as fitz
from docx import Document

MAX_DOCX_MEMBERS = 5000
MAX_DOCX_EXPANDED = 300 * 1024 * 1024      # everything, images included
MAX_DOCX_XML = 80 * 1024 * 1024            # the parts that are parsed as text
MAX_PDF_PAGES = 500                        # a PhD/DSc dissertation with appendices

# A word split across lines by a hyphen in PDF layout ("приме-\nнение").
# Only joined when the next line starts with a lower-case letter, so a
# capitalised continuation or a number is left alone.
# The preceding character may be an Uzbek apostrophe letter part (xo‘-/jalik).
_LINE_HYPHEN = re.compile("(?:(?<=[^\\W\\d_])|(?<=['\u02bb\u02bc\u2018\u2019`]))[-\u2010\u00ad][ \t]*\r?\n[ \t]*(?=[^\\W\\d_])")


class ExtractError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def dehyphenate(text):
    return _LINE_HYPHEN.sub(lambda m: "" if text[m.end()].islower() else m.group(), text)


def expand_ligatures(text):
    """'Inﬂation' -> 'Inflation': PDF text often keeps typographic ligatures as single characters."""
    from textfold import LIGATURES
    return text.translate(str.maketrans(LIGATURES)) if any(c in text for c in LIGATURES) else text


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def docx_text(doc):
    """Body text in reading order: paragraphs and tables interleaved, text boxes
    included (Word's duplicate compatibility copy skipped), tabs and line breaks kept."""
    out = []

    def walk(element):
        for child in element:
            tag = child.tag
            if tag == MC_FALLBACK:
                continue                                  # same text as the mc:Choice copy
            if tag == W + "t":
                out.append(child.text or "")
            elif tag in (W + "tab",):
                out.append("\t")
            elif tag in (W + "br", W + "cr"):
                out.append("\n")
            elif tag == W + "p":
                walk(child)
                out.append("\n")
            elif tag == W + "tc":
                walk(child)
                out.append("\t")
            elif tag == W + "tr":
                walk(child)
                out.append("\n")
            else:
                walk(child)

    walk(doc.element.body)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


DOC_REPLACE = {"\r": "\n", "\x0b": "\n", "\x0c": "\n", "\x0e": "\n", "\x07": "\t", "\x1e": "-", "\x1f": ""}


def _doc_clean(raw):
    """Word control characters to plain text; field codes dropped, field results kept."""
    out, stack = [], []                 # stack: True while inside a field's code part
    for ch in raw:
        if ch == "\x13":
            stack.append(True)
            continue
        if ch == "\x14":
            if stack:
                stack[-1] = False
            continue
        if ch == "\x15":
            if stack:
                stack.pop()
            continue
        if any(stack):
            continue
        if ch == "\x07" and out and out[-1] == "\t":
            out[-1] = "\n"                               # cell mark + row mark: end of table row
        elif ch in DOC_REPLACE:
            out.append(DOC_REPLACE[ch])
        elif ch >= " " or ch in "\n\t":
            out.append(ch)
    text = "".join(out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def doc_text(content):
    """Text of a Word 97-2003 .doc file from its piece table ([MS-DOC] 2.4.1)."""
    if content.lstrip()[:5].lower() == b"{\\rtf":
        raise ExtractError(400, "doc_not_word", "This .doc file is really RTF. Open it in Word and save it as DOCX.")
    if content.lstrip()[:15].lower().startswith((b"<html", b"<!doctype", b"mime-version")):
        raise ExtractError(400, "doc_not_word", "This .doc file is really a web page. Open it in Word and save it as DOCX.")
    try:
        ole = olefile.OleFileIO(io.BytesIO(content))
    except (OSError, ValueError, struct.error):
        raise ExtractError(400, "extract_failed", "Could not read this .doc file. Save it as DOCX and try again.")
    try:
        if ole.exists("EncryptedPackage") or not ole.exists("WordDocument"):
            raise ExtractError(400, "doc_protected" if ole.exists("EncryptedPackage") else "extract_failed",
                               "Password-protected Word files cannot be read. Save an unprotected copy."
                               if ole.exists("EncryptedPackage") else "This file has no Word document inside.")
        word = ole.openstream("WordDocument").read()
        ident, nfib, flags = struct.unpack_from("<HH6xH", word, 0)       # wIdent, nFib, ..., flags at 0x0A
        if ident != 0xA5EC or nfib < 101:
            raise ExtractError(400, "extract_failed", "This Word file is older than Word 97. Save it as DOCX.")
        if flags & 0x0100:
            raise ExtractError(400, "doc_protected", "Password-protected Word files cannot be read. Save an unprotected copy.")
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            raise ExtractError(400, "extract_failed", "Could not read this .doc file. Save it as DOCX and try again.")
        table = ole.openstream(table_name).read()
        ccp_text = struct.unpack_from("<i", word, 0x004C)[0]
        fc_clx, lcb_clx = struct.unpack_from("<II", word, 0x01A2)
        clx = table[fc_clx:fc_clx + lcb_clx]
        pos = 0
        while pos < len(clx) and clx[pos] == 0x01:                 # skip Prc blocks
            pos += 3 + struct.unpack_from("<h", clx, pos + 1)[0]
        if pos >= len(clx) or clx[pos] != 0x02:
            raise ValueError("no piece table")
        lcb = struct.unpack_from("<I", clx, pos + 1)[0]
        plc = clx[pos + 5:pos + 5 + lcb]
        pieces = (lcb - 4) // 12
        if pieces <= 0 or pieces > 200000:
            raise ValueError("bad piece table")
        cps = struct.unpack_from(f"<{pieces + 1}I", plc, 0)
        parts, total = [], 0
        for i in range(pieces):
            start, end = cps[i], min(cps[i + 1], ccp_text)
            if end <= start:
                continue
            fc = struct.unpack_from("<I", plc, 4 * (pieces + 1) + 8 * i + 2)[0]
            if fc & 0x40000000:                                       # 8-bit (cp1252) piece
                offset = (fc & 0x3FFFFFFF) // 2
                parts.append(word[offset:offset + (end - start)].decode("cp1252", "replace"))
            else:                                                     # UTF-16 piece
                offset = fc & 0x3FFFFFFF
                parts.append(word[offset:offset + 2 * (end - start)].decode("utf-16-le", "replace"))
            total += end - start
            if total >= ccp_text or total > 2_000_000:
                break
        return _doc_clean("".join(parts))
    except ExtractError:
        raise
    except (OSError, ValueError, struct.error, IndexError):
        raise ExtractError(400, "extract_failed", "Could not read this .doc file. Save it as DOCX and try again.")
    finally:
        ole.close()


# ------------------------------------------------------------- OCR (scanned PDF pages)
# Tesseract with the Uzbek (Latin and Cyrillic), Russian and English models. Only pages without a text
# layer are read. Scanned pages are read with all four models until one has enough text to tell the script, the rest
# with the models that fit (faster and more accurate). Settings: OCR_ENABLED=0 turns it off; OCR_MAX_PAGES (120),
# OCR_MAX_SECONDS (240) and OCR_DPI (300) bound the work for one upload; OCR_QUEUE_SECONDS (60) is how
# long an upload waits while another one is being read.

OCR_ALL = "uzb+uzb_cyrl+rus+eng"
_OCR_SLOT = threading.BoundedSemaphore(1)      # one upload at a time: Tesseract is CPU- and memory-heavy
TESSDATA_DIRS = ("/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata", "/usr/share/tessdata")


def _tessdata():
    for folder in [os.environ.get("TESSDATA_PREFIX", "")] + list(TESSDATA_DIRS):
        if folder and all(os.path.isfile(os.path.join(folder, f"{m}.traineddata")) for m in ("uzb", "uzb_cyrl", "rus", "eng")):
            return folder
    return None


def ocr_available():
    return os.environ.get("OCR_ENABLED", "1") != "0" and _tessdata() is not None


def _bounded(name, default, low, high):
    try:
        return min(max(int(os.environ.get(name, default)), low), high)
    except ValueError:
        return default


def _models_for(sample):
    """The Tesseract models that fit the text read from the first scanned page."""
    from corpus import detect_language, script_shares
    language = detect_language(sample) if len(sample.split()) >= 20 else None
    cyrillic, _ = script_shares(sample) if sample.strip() else (0, 0)
    if language == "ru":
        return "rus+eng"
    if language == "uz":
        return "uzb_cyrl+rus" if cyrillic >= 0.5 else "uzb+eng"
    if language == "en":
        return "eng"
    return OCR_ALL


FUNCTION_WORDS = {"a", "an", "as", "at", "by", "in", "is", "it", "of", "on", "or", "to", "the", "va", "da", "bu",
                  "в", "и", "к", "о", "с", "у", "во", "из", "на", "не", "по", "от", "до", "за", "со", "ва"}
# Scanner-app logos printed on every page ("CamScanner", "Scanned with Adobe Scan").
SCANNER_MARK = re.compile(r"camscanner|scanned with|adobe scan|microsoft lens|genius scan|vflat|tapscanner", re.I)


def _clean_ocr(text):
    """Drop OCR lines that are not text: scanner-app logos, and lines made of stray letters and symbols
    (stamps, signatures, table rules). Lines with real words are kept as read."""
    kept = []
    for line in text.split("\n"):
        tokens = line.split()
        if not tokens:
            kept.append(line)
            continue
        if SCANNER_MARK.search(line) and len(tokens) <= 4:
            continue
        words = [t for t in tokens if sum(c.isalpha() for c in t) >= 3 or t.lower() in FUNCTION_WORDS]
        if not words and not any(t.isdigit() for t in tokens):
            continue                                   # "|", "‘ a", "2 ie" ... (numbers alone are kept: tables)
        kept.append(line)
    return "\n".join(kept)


def _ocr_page(page, models, dpi, tessdata):
    textpage = page.get_textpage_ocr(language=models, dpi=dpi, full=True, tessdata=tessdata)
    return _clean_ocr(page.get_text(textpage=textpage))


def _ocr_pdf(pdf, texts, info):
    """Fill in the text of pages without a text layer. Raises ExtractError when there are too many
    scanned pages or the time budget is used up (nothing is guessed)."""
    scanned = [i for i, text in enumerate(texts) if len(text.strip()) < 25 and pdf[i].get_images()]
    if not scanned:
        return texts
    tessdata = _tessdata()
    if not tessdata or os.environ.get("OCR_ENABLED", "1") == "0":
        return texts
    limit, budget, dpi = _bounded("OCR_MAX_PAGES", 120, 1, 2000), _bounded("OCR_MAX_SECONDS", 240, 10, 3600), _bounded("OCR_DPI", 300, 150, 400)
    if len(scanned) > limit:
        raise ExtractError(413, "ocr_too_many_pages", f"{len(scanned)} scanned pages; at most {limit} can be read by OCR")
    if not _OCR_SLOT.acquire(timeout=_bounded("OCR_QUEUE_SECONDS", 60, 0, 600)):
        raise ExtractError(503, "ocr_busy", "Another scanned document is being read. Try again in a minute.")
    try:
        started, models = time.monotonic(), OCR_ALL
        for n, i in enumerate(scanned):
            if time.monotonic() - started > budget:
                raise ExtractError(413, "ocr_timeout", f"Reading the scanned pages took longer than {budget} seconds")
            texts[i] = _ocr_page(pdf[i], models, dpi, tessdata)
            if models == OCR_ALL:                   # until a page has enough text to tell the script
                models = _models_for(texts[i])
    finally:
        _OCR_SLOT.release()
    info.update(ocr_pages=len(scanned), ocr_models=models, ocr_seconds=round(time.monotonic() - started, 1))
    return texts


def extract_text(name, content, max_pages=MAX_PDF_PAGES, ocr=False, info=None):
    """Return text for a .txt/.docx/.pdf payload or raise ExtractError. With ocr=True, scanned PDF pages
    are read by OCR, and `info` (a dict) receives ocr_pages, ocr_models and ocr_seconds."""
    info = {} if info is None else info
    name = name.lower()
    if not content:
        raise ExtractError(400, "empty_file", "The file is empty.")
    if name.endswith(".txt"):
        try:
            if content.startswith((b"\xff\xfe", b"\xfe\xff")):
                return content.decode("utf-16")
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ExtractError(400, "txt_encoding", "TXT files must be saved as UTF-8 (or UTF-16 with a byte-order mark).")
    if not name.endswith((".docx", ".doc", ".pdf")):
        raise ExtractError(400, "unsupported_type", "Only TXT, DOC, DOCX and PDF supported")
    if name.endswith(".doc"):
        if content.startswith(b"PK"):
            name = name + "x"                           # a .docx file renamed to .doc
        else:
            return doc_text(content)
    try:
        if name.endswith(".docx"):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                items = archive.infolist()
                if (len(items) > MAX_DOCX_MEMBERS or sum(item.file_size for item in items) > MAX_DOCX_EXPANDED or
                        sum(item.file_size for item in items if item.filename.endswith((".xml", ".rels"))) > MAX_DOCX_XML):
                    raise ExtractError(413, "docx_expanded", "Expanded DOCX exceeds safe limits")
            return docx_text(Document(io.BytesIO(content)))
        with fitz.open(stream=content, filetype="pdf") as pdf:
            if pdf.page_count > max_pages:
                raise ExtractError(400, "pdf_pages", f"PDF exceeds {max_pages} pages")
            info["pdf_title"] = (pdf.metadata or {}).get("title") or ""
            texts = [page.get_text() for page in pdf]
            if ocr:
                texts = _ocr_pdf(pdf, texts, info)
            return expand_ligatures(dehyphenate("\n".join(texts)))
    except ExtractError:
        raise
    except Exception:
        raise ExtractError(400, "extract_failed", "Could not read this file. Check that it is a valid DOCX or text-based PDF.")
