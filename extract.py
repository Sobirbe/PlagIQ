"""Bounded text extraction shared by the website and the owner-run importer."""
import io
import re
import zipfile

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
    if not name.endswith((".docx", ".pdf")):
        raise ExtractError(400, "unsupported_type", "Only TXT, DOCX and text-based PDF supported")
    try:
        if name.endswith(".docx"):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if (len(archive.infolist()) > MAX_DOCX_MEMBERS or
                        sum(item.file_size for item in archive.infolist()) > MAX_DOCX_EXPANDED):
                    raise ExtractError(413, "docx_expanded", "Expanded DOCX exceeds safe limits")
            doc = Document(io.BytesIO(content))
            return "\n".join([p.text for p in doc.paragraphs] +
                             [cell.text for table in doc.tables for row in table.rows for cell in row.cells])
        with fitz.open(stream=content, filetype="pdf") as pdf:
            if pdf.page_count > max_pages:
                raise ExtractError(400, "pdf_pages", f"PDF exceeds {max_pages} pages")
            return dehyphenate("\n".join(page.get_text() for page in pdf))
    except ExtractError:
        raise
    except Exception:
        raise ExtractError(400, "extract_failed", "Could not read this file. Check that it is a valid DOCX or text-based PDF.")
