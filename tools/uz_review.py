"""Native-speaker review of PlagIQ's Uzbek (Latin) texts: export them to a spreadsheet, apply the corrections back.

  python tools/uz_review.py export uz-review.xlsx
  python tools/uz_review.py apply  uz-review.xlsx        # then run the tests and look at `git diff`

The sheet has one row per text: where it is shown, the Uzbek text, the English text (for meaning), and an empty
"Correction" column. The reviewer fills in only the rows that need a change. The Cyrillic interface (uzc) is
generated from the Latin texts, so it follows automatically.

Rules for the reviewer (also on the sheet's first tab): keep {placeholders} such as {n} or {p} and __WORDS__
such as __KEEP_UZ__ exactly as they are; keep the meaning; the apply step refuses a correction whose placeholders
differ, and reports any text it could not find in the code (to be fixed by hand).

Where the texts come from: the page's text tables in index.html (read in a headless browser, since they are
JavaScript), the Python modules' text tables (PDF report, certificate, status texts, verification page, account
pages, the "check is ready" message, the Telegram bot), and the Uzbek part of privacy.html.
"""
import html
import importlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE_TABLES = ("L X E P searchStates SV G A R OT U SI SIE AT PY TP PG WT CR XS EDT HI CST OCRN PYE WN TRN PRN AV "
               "AIX UZX").split()
MODULE_TABLES = {"app": ("VERIFY_TEXT", "ACCOUNT_T"), "checkstatus": ("T",),
                 "report_pdf": ("T", "INTEGRITY_T", "AI_T", "UZAI_T", "TRANSLATED_T", "PARAPHRASED_T", "EXCLUDED_T",
                                "EDITED_T", "VIEW_T"),
                 "certificate": ("C", "LIBRARY_T"), "notify": ("TEXTS",), "telegram_bot": ("TEXTS",)}
WHERE = {"index.html": "Website page", "privacy.html": "Privacy page", "report_pdf.py": "PDF report",
         "certificate.py": "Certificate (PDF)", "checkstatus.py": "Check status texts", "notify.py": "“Check ready” message",
         "telegram_bot.py": "Telegram bot", "app.py": "Verification / account pages"}
PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}|\{[a-z_0-9]*\}|__[A-Z_]+__")


def _flatten(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _flatten(v, f"{path}[{i}]")


def _wanted(text):
    return bool(re.search(r"[A-Za-z]{2}", text)) and not text.startswith(("http", "#", "/"))


def page_texts():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto((ROOT / "index.html").as_uri())
        found = page.evaluate("""names => { const out = {};
            for (const n of names) { try { const t = eval(n); out[n] = {uz: t.uz, en: t.en}; } catch (e) {} }
            return out; }""", PAGE_TABLES)
        browser.close()
    for name in PAGE_TABLES:
        table = found.get(name) or {}
        en = dict(_flatten(table.get("en")))
        for path, text in _flatten(table.get("uz")):
            if _wanted(text):
                yield "index.html", f"{name}.{path}", text, en.get(path, "")


def module_texts():
    for module, names in MODULE_TABLES.items():
        mod = importlib.import_module(module)
        for name in names:
            table = getattr(mod, name, None) or {}
            en = dict(_flatten(table.get("en")))
            for path, text in _flatten(table.get("uz")):
                if _wanted(text):
                    yield f"{module}.py", f"{name}.{path}", text, en.get(path, "")


class _Texts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section, self.parts = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "section":
            self.section = dict(attrs).get("id")

    def handle_data(self, data):
        if self.section == "uz" and data.strip() and _wanted(data):
            self.parts.append(data.strip())


def privacy_texts():
    parser = _Texts()
    parser.feed((ROOT / "privacy.html").read_text("utf-8"))
    for i, text in enumerate(parser.parts):
        yield "privacy.html", f"uz section, part {i + 1}", text, ""


def export(path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    rows, seen = [], set()
    for source in (page_texts(), module_texts(), privacy_texts()):
        for file, key, text, en in source:
            if (file, text) in seen:          # the same text in the same file: one row (all copies are corrected)
                continue
            seen.add((file, text))
            rows.append((len(rows) + 1, WHERE.get(file, file), file, key, text, en))
    book = Workbook()
    guide = book.active
    guide.title = "How to review"
    for line in ("PlagIQ — o‘zbekcha matnlarni tekshirish / Uzbek text review",
                 "",
                 "1. «Matnlar» varag‘ida har bir qatorda saytda ko‘rsatiladigan o‘zbekcha matn bor (E ustun), ma’nosi uchun inglizchasi (F ustun).",
                 "2. Faqat tuzatish kerak bo‘lgan qatorlarda G ustunga («Tuzatish») to‘liq to‘g‘ri matnni yozing. Qolganlarini bo‘sh qoldiring.",
                 "3. {n}, {p}, {{EMAIL}} kabi qavsli belgilar va __KEEP_UZ__ kabi so‘zlarni o‘zgartirmang — ular o‘rniga dastur raqam yoki so‘z qo‘yadi.",
                 "4. Ma’noni o‘zgartirmang; faqat til, imlo va uslub. Kirill yozuvi lotinchadan avtomatik yasaladi.",
                 "5. Izoh bo‘lsa, H ustunga («Izoh») yozing.",
                 "",
                 "1. The «Matnlar» sheet has one Uzbek text per row (column E) with the English for meaning (column F).",
                 "2. Only where a change is needed, write the full corrected text in column G («Tuzatish»). Leave other rows empty.",
                 "3. Keep {n}, {p}, {{EMAIL}}… and __KEEP_UZ__-style words exactly: the program fills them in.",
                 "4. Keep the meaning; correct language, spelling and style only. The Cyrillic version is generated automatically.",
                 "5. Comments go in column H («Izoh»)."):
        guide.append([line])
    guide.column_dimensions["A"].width = 140
    guide["A1"].font = Font(bold=True, size=13)
    sheet = book.create_sheet("Matnlar")
    sheet.append(["#", "Qayerda / Where", "File", "Key", "O‘zbekcha matn", "English (meaning)", "Tuzatish / Correction",
                  "Izoh / Comment"])
    for r in rows:
        sheet.append(list(r) + ["", ""])
    for col, width in zip("ABCDEFGH", (6, 22, 14, 26, 70, 55, 70, 30)):
        sheet.column_dimensions[col].width = width
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    fill = PatternFill("solid", fgColor="FFF7D6")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        row[6].fill = fill
    sheet.freeze_panes = "E2"
    sheet.auto_filter.ref = f"A1:H{len(rows) + 1}"
    book.save(path)
    return len(rows)


def _literal_forms(text, file):
    """How `text` can appear in the file's source: as written, JSON/JS-escaped, Python-escaped, HTML-escaped."""
    forms = [json.dumps(text, ensure_ascii=False)[1:-1], text]
    if file.endswith(".py"):
        forms.append(repr(text)[1:-1])
    if file.endswith(".html"):
        forms.append(html.escape(text, quote=False))
    return list(dict.fromkeys(forms))


def apply(path):
    from openpyxl import load_workbook
    sheet = load_workbook(path)["Matnlar"]
    done, problems, sources = 0, [], {}
    for row in sheet.iter_rows(min_row=2, values_only=True):
        number, _, file, key, original, _, correction = row[:7]
        if not correction or not str(correction).strip() or str(correction).strip() == str(original).strip():
            continue
        correction = str(correction).strip()
        if correction.startswith("="):                 # a spreadsheet formula (e.g. =E31, the row's own text): no text
            problems.append(f"#{number} {key}: a formula ({correction[:20]}), not text — skipped")
            continue
        if sorted(PLACEHOLDER.findall(correction)) != sorted(PLACEHOLDER.findall(original)):
            problems.append(f"#{number} {key}: the {{placeholders}} differ — not applied")
            continue
        text = sources.setdefault(file, (ROOT / file).read_text("utf-8"))
        for old, new in zip(_literal_forms(original, file), _literal_forms(correction, file)):
            # only where the text stands complete (between quotes, or between tags): the same words inside a
            # longer text are a different text that the reviewer did not see in that place
            whole = re.compile(r"(?<=[\"'>])(\s*)" + re.escape(old) + r"(\s*)(?=[\"'<])")
            if whole.search(text):
                sources[file] = text = whole.sub(lambda m: m.group(1) + new + m.group(2), text)
                done += 1
                break
        else:
            problems.append(f"#{number} {file} {key}: text not found as one piece in the code — fix by hand")
    for file, text in sources.items():
        (ROOT / file).write_text(text, "utf-8")
    return done, problems


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("export", "apply"):
        sys.exit(__doc__)
    if sys.argv[1] == "export":
        print(f"{export(sys.argv[2])} texts written to {sys.argv[2]}")
    else:
        n, problems = apply(sys.argv[2])
        print(f"{n} corrections applied")
        for p in problems:
            print("  " + p)
        sys.exit(1 if problems else 0)
