"""Uzbek Cyrillic interface ("uzc"): generated from the Uzbek Latin texts; the page (JavaScript) and the server
(uzcyr.py) give identical text; placeholders, names and codes survive; PDFs and the verification page follow."""
import io
import re

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app as web
import certificate
import checkstatus
import report_pdf
import uzcyr

CASES = {
    "V0.5 · ILMIY SINOV": "V0.5 · ИЛМИЙ СИНОВ",
    "muvaffaqiyatsiz": "муваффақиятсиз", "Agar o‘tsa": "Агар ўтса", "ketsangiz": "кетсангиз",
    "tsement": "цемент", "natsional": "национал", "kvalifikatsiya": "квалификация", "dotsentlar": "доцентлар",
    "PLAGIQ — PLAGIAT TEKSHIRUVI XULOSASI": "PLAGIQ — ПЛАГИАТ ТЕКШИРУВИ ХУЛОСАСИ",
    "Oʻzbekiston": "Ўзбекистон", "taʼlim": "таълим", "yoʻl": "йўл", "gʻoya": "ғоя", "Mening tekshiruvlarim": "Менинг текширувларим",
    "esa": "эса", "Elektron pochta": "Электрон почта", "shu yerda": "шу ерда", "qayerda": "қаерда", "ilmiy": "илмий",
    "Yangi": "Янги", "poeziya": "поэзия", "15-sentabrda": "15-сентябрда", "kvitansiyalar": "квитанциялар",
    "dissertatsiya": "диссертация", "Kompyuter": "Компьютер", "Bu PDFda OCR bilan": "Бу PDFда OCR билан",
    "masalan OAKka": "масалан ОАКка", "AQSHda": "АҚШда", "{n} ta so‘z": "{n} та сўз", "Telegram orqali kirish": "Telegram орқали кириш",
    "VERIFY_SECRET yo‘q": "VERIFY_SECRET йўқ", "robots.txt taqiqlagan": "robots.txt тақиқлаган", "emailga": "emailга",
    "<b>Diqqat</b>: sinov": "<b>Диққат</b>: синов", "https://plagiq.uz/verify?r=1": "https://plagiq.uz/verify?r=1",
    "PhD / DSc": "PhD / DSc", "tayyor": "тайёр", "sheʼr": "шеър", "Yevropa": "Европа", "‘Xavfsiz’ rejim": "‘Хавфсиз’ режим",
    "Word’da": "Word’да", "OʻZBEKISTON": "ЎЗБЕКИСТОН", "SENTABR": "СЕНТЯБРЬ",
}
TABLES = ['A', 'CR', 'CST', 'E', 'EDT', 'G', 'HI', 'L', 'OCRN', 'OT', 'P', 'PG', 'PY', 'PYE', 'R', 'SI', 'SIE', 'SV', 'TP', 'U', 'X', 'XS',
          'searchStates']


def test_transliteration_rules():
    for latin, cyrillic in CASES.items():
        assert uzcyr.to_cyrillic(latin) == cyrillic, latin


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


@pytest.fixture(scope="module")
def page_tables():
    pytest.importorskip("playwright", reason="browser tests run in the CI 'browser' job")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("file://" + str(__import__("pathlib").Path(web.__file__).with_name("index.html")))
        page.locator("[data-lang=uzc]").click()
        tables = page.evaluate("names => Object.fromEntries(names.map(n => [n, {uz: eval(n).uz, uzc: eval(n).uzc}]))", TABLES)
        js_cases = page.evaluate("cases => cases.map(uzCyr)", list(CASES))
        state = page.evaluate("() => [document.documentElement.lang, document.querySelector('#submit').textContent]")
        browser.close()
    return tables, js_cases, state, errors


def test_page_and_server_transliterate_identically(page_tables):
    tables, js_cases, state, errors = page_tables
    assert not errors and state[0] == "uz-Cyrl" and state[1].startswith("Текширишни")
    assert js_cases == list(CASES.values())
    for name, t in tables.items():
        assert t["uzc"] == uzcyr.deep(t["uz"]), name


def test_placeholders_and_markup_survive_and_no_latin_word_is_left(page_tables):
    tables, *_ = page_tables
    for name, t in tables.items():
        for latin, cyrillic in zip(strings(t["uz"]), strings(t["uzc"])):
            assert re.findall(r"\{\w+\}", latin) == re.findall(r"\{\w+\}", cyrillic), (name, latin)
            assert re.findall(r"<[^>]+>", latin) == re.findall(r"<[^>]+>", cyrillic), (name, latin)
            leftover = re.sub(uzcyr.KEEP, " ", cyrillic)
            assert not re.search(r"[a-z]{2,}", leftover), (name, cyrillic)


def test_server_texts_and_reports_in_cyrillic():
    for table in (report_pdf.T, report_pdf.VIEW_T, certificate.C, checkstatus.T, web.VERIFY_TEXT):
        assert "uzc" in table and table["uzc"] == uzcyr.deep(table["uz"])
    web.RATE.clear(); web.results.clear()
    client = TestClient(web.app)
    text = "Oʻzbekistonda oliy taʼlim tizimi soʻnggi yillarda tez rivojlanmoqda va yangi universitetlar ochilmoqda. " * 3
    data = client.post("/api/check", data={"mode": "reference", "language": "uz", "reference_text": text},
                       files={"submission": ("ish.txt", io.BytesIO(text.encode()))}).json()
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?lang=uzc")
    body = " ".join(p.get_text() for p in pymupdf.open(stream=pdf.content, filetype="pdf"))
    assert "PLAGIQ — ПЛАГИАТ ТЕКШИРУВИ ХУЛОСАСИ" in body and "Тошкент вақти" in body
    assert "TEKSHIRUV" not in body.upper().replace("ТЕКШИРУВ", "")
    verify_page = client.get("/verify?r=abc&ui=uzc").text
    assert '<html lang="uz-Cyrl">' in verify_page and "ЎЗ</a>" in verify_page
    assert '<html lang="uz-Cyrl">' in client.get("/verify?r=abc", headers={"accept-language": "uz-Cyrl-UZ"}).text


def test_inline_label_lookups_pick_the_language():
    """`(o=>o||...)({en:..,uz:..})` (the key lookup left out) put "[object Object]" in the Privacy link and between
    web-search provider names; every inline lookup must index the object by the language."""
    import re
    page = open("index.html", encoding="utf-8").read()
    lookups = re.findall(r"\(o=>o(\[\w+\])?\|\|", page)
    assert lookups and all(lookups), "an inline label lookup without [code]/[current]"
