"""CyberLeninka import against a simulated site (OAI feed, article pages, robots.txt forbidding PDFs)."""
import http.server
import json
import threading

import pytest

import corpus
import cyberleninka
from harvest import Fetcher

BODY_RU = ("Статья рассматривает подготовку экономистов-статистиков и роль количественных методов в высшем образовании "
           "региона, анализирует учебные планы и предлагает меры по их совершенствованию. ") * 25


def page(title, badge, text, authors=("Иванов И. И.",), year="2019"):
    badge_html = (f'<div class="statitem label-cc" title="Лицензия Creative Commons"><img src="/images/cc.svg" />{badge}</div>'
                  if badge else "")
    metas = "".join(f'<meta name="citation_author" content="{a}">' for a in authors)
    return (f'<html><head><meta name="citation_title" content="{title}">{metas}'
            f'<meta name="citation_publication_date" content="{year}"><meta name="citation_journal_title" content="Вестник">'
            f'<meta name="citation_pdf_url" content="/article/n/x/pdf"></head><body><nav>Меню</nav>{badge_html}'
            f'<div class="ocr" itemprop="articleBody"><div class="ocr-banner">Реклама</div><p>{text}</p></div>'
            f'<div class="full">Похожие статьи</div></body></html>').encode()


def record(slug, title):
    return (f'<record><header><identifier>{{base}}/article/n/{slug}</identifier><datestamp>2020-01-01T00:00:00Z</datestamp>'
            f'</header><metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:creator>Иванов</dc:creator>'
            f'<dc:identifier>{{base}}/article/n/{slug}</dc:identifier></oai_dc:dc></metadata></record>')


@pytest.fixture()
def site(monkeypatch):
    requests = []
    pages = {"/article/n/a1": page("Подготовка экономистов", "CC BY", BODY_RU),
             "/article/n/a2": page("Некоммерческая", "CC BY-NC", BODY_RU),
             "/article/n/a3": page("Без лицензии", "", BODY_RU),
             "/article/n/a4": page("Короткая", "CC BY", "Слишком короткий текст статьи."),
             "/article/n/a5": page("Без авторов", "CC BY-SA", BODY_RU.replace("экономистов", "статистиков"), authors=())}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            requests.append(self.path)
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/robots.txt":
                body, ctype = b"User-Agent: *\nDisallow: /article/n/*/pdf\nDisallow: /api/\n", "text/plain"
            elif self.path.startswith("/oai"):
                first = "resumptionToken" not in self.path
                recs = [record("a1", "Подготовка"), record("a2", "НК"), record("a3", "Без")] if first else \
                    [record("a4", "Короткая"), record("a5", "Без авторов")]
                token = "<resumptionToken>next</resumptionToken>" if first else "<resumptionToken/>"
                body = ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>'
                        + "".join(recs).replace("{base}", base) + token + "</ListRecords></OAI-PMH>").encode()
                ctype = "text/xml"
            elif self.path in pages:
                body, ctype = pages[self.path], "text/html; charset=utf-8"
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("CYBERLENINKA_URL", f"http://127.0.0.1:{server.server_port}")
    yield requests, lambda: Fetcher(delay=0, allow_http_hosts={"127.0.0.1"})
    server.shutdown()


def test_parse_article_extracts_licence_metadata_and_body_only():
    article = cyberleninka.parse_article(page("Заголовок", "CC BY", BODY_RU, authors=("А. А.", "Б. Б.")).decode())
    assert article["licence"] == "CC BY" and article["authors"] == "А. А.; Б. Б." and article["year"] == 2019
    assert "Меню" not in article["text"] and "Реклама" not in article["text"] and "Похожие" not in article["text"]
    assert article["text"].startswith("Статья рассматривает")


def test_batch_applies_licence_rules_and_never_requests_pdfs(site, tmp_path):
    requests, fetcher = site
    added, review = cyberleninka.harvest_batch(tmp_path / "s", 10, fetcher=fetcher())
    status = {r["url"].rsplit("/", 1)[-1]: (r["status"], r["reason"]) for r in review}
    assert status["a1"] == ("staged", "")
    assert status["a2"][0] == "review" and "non-commercial" in status["a2"][1]
    assert status["a3"] == ("skipped", "no licence shown on the article page")
    assert status["a4"][0] == "skipped" and "under 300 words" in status["a4"][1]
    assert status["a5"] == ("skipped", "no authors")
    assert [e["title"] for e in added] == ["Подготовка экономистов"] and added[0]["rights_basis"] == "open_license"
    assert added[0]["language"] == "ru" and "Licence by shown on" in added[0]["permission_reference"]
    assert not any("pdf" in path for path in requests)                       # robots.txt forbids PDFs
    # a second run resumes and does not repeat finished articles
    again, review2 = cyberleninka.harvest_batch(tmp_path / "s", 10, fetcher=fetcher())
    assert again == [] and all(r["url"].rsplit("/", 1)[-1] not in ("a1", "a3", "a5") for r in review2)


def test_run_imports_and_copied_text_is_found(site, tmp_path):
    _, fetcher = site
    status = cyberleninka.run(10, db_path=str(tmp_path / "c.db"), root=tmp_path, fetcher=fetcher())
    assert status["imported"] == 1 and status["error"] is None
    student = "Моё введение. " + " ".join(BODY_RU.split()[30:80]) + " Мой вывод."
    found = corpus.search(corpus.connect(str(tmp_path / "c.db")), student, "ru")["sources"]
    assert found[0]["title"] == "Подготовка экономистов" and found[0]["matched_words"] >= 50
    assert found[0]["url"] == "https://cyberleninka.ru/article/n/a1"
    assert json.loads((tmp_path / "import-status.json").read_text())["cyberleninka"]["imported"] == 1
