"""Bounded text extraction shared by the website and the owner-run importer."""
import io
import struct
import re
import zipfile

import olefile
import pymupdf as fitz
from docx import Document

MAX_DOCX_MEMBERS = 1000
MAX_DOCX_EXPANDED = 20 * 1024 * 1024
MAX_PDF_PAGES = 40

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


def extract_text(name, content, max_pages=MAX_PDF_PAGES):
    """Return text for a .txt/.docx/.pdf payload or raise ExtractError."""
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
        raise ExtractError(400, "unsupported_type", "Only TXT, DOC, DOCX and text-based PDF supported")
    if name.endswith(".doc"):
        if content.startswith(b"PK"):
            name = name + "x"                           # a .docx file renamed to .doc
        else:
            return doc_text(content)
    try:
        if name.endswith(".docx"):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if (len(archive.infolist()) > MAX_DOCX_MEMBERS or
                        sum(item.file_size for item in archive.infolist()) > MAX_DOCX_EXPANDED):
                    raise ExtractError(413, "docx_expanded", "Expanded DOCX exceeds safe limits")
            return docx_text(Document(io.BytesIO(content)))
        with fitz.open(stream=content, filetype="pdf") as pdf:
            if pdf.page_count > max_pages:
                raise ExtractError(400, "pdf_pages", f"PDF exceeds {max_pages} pages")
            return dehyphenate("\n".join(page.get_text() for page in pdf))
    except ExtractError:
        raise
    except Exception:
        raise ExtractError(400, "extract_failed", "Could not read this file. Check that it is a valid DOCX or text-based PDF.")
