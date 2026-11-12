"""Owner database tools: library statistics, source diagnostic, import history, Uzbek works labelled
Russian by OpenAlex, and chapter-based academic retrieval with de-duplication."""
import http.server
import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import admin
import app as web
import auth
import corpus
import discovery
import engine_service
import import_job
from corpus import connect, ingest, inventory
from test_archive import engine  # noqa: F401  (fixture: engine on a temporary corpus, website wired to it)
from test_auth_ai import TOKEN, sign_in
from test_oa_import import RU, UZ, pdf

KEY = "k" * 32
UZ_LONG = ("Oliy ta'lim muassasalarida ta'lim sifatini oshirish dolzarb masala hisoblanadi va o'qituvchilarning "
           "kasbiy kompetensiyasini rivojlantirish talab etiladi. ") * 25
RU_TEXT = "Научный текст о качестве образования в региональных университетах сегодня рассматривается подробно. " * 4


def meta(**extra):
    base = dict(title="Ta'lim sifati", url="https://doi.org/10.5555/uz.2024.1", language="uz", authors="A. Karimov",
                year=2024, rights_basis="open_license",
                permission_reference="CC BY licence stated by OpenAlex best_oa_location for https://openalex.org/W1")
    return {**base, **extra}


@pytest.fixture()
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    with connect() as db:
        ingest(db, meta(), UZ_LONG)
        ingest(db, meta(title="Wikipedia: Toshkent", url="https://uz.wikipedia.org/wiki/Toshkent", source_type="wikipedia",
                        permission_reference="CC BY-SA, Wikipedia dump"), "Toshkent shahri O'zbekiston poytaxti va eng katta shahri hisoblanadi.")
        ingest(db, meta(title="Talaba ishi", url="", rights_basis="author_deposit", owner="user:" + "a" * 24,
                        permission_reference="author consent"), "Talabaning yozgan maxfiy matni shu yerda turadi va boshqa joyda yo'q. " * 10)
        ingest(db, meta(title="Качество образования", url="https://cyberleninka.ru/article/n/x", language="ru",
                        permission_reference="Licence CC BY shown on https://cyberleninka.ru/article/n/x"), RU_TEXT)
    return tmp_path


# ------------------------------------------------------------- statistics

def test_stats_by_language_provider_rights_freshness_coverage(library):
    with connect() as db:
        s = corpus.stats(db)
    assert s["total"] == 4 and s["by_language"] == {"uz": 3, "ru": 1}
    assert s["by_provider"] == {"openalex": 1, "wikipedia": 1, "author_deposit": 1, "cyberleninka": 1}
    assert s["by_rights"] == {"open_license": 3, "author_deposit": 1}
    assert s["category_language"]["wikipedia"] == {"uz": 1}
    assert s["coverage"]["uz"]["full_texts"] == 1 and s["coverage"]["uz"]["short_texts"] == 2
    assert all(v["last_7_days"] >= 1 and v["newest"] for v in s["freshness"].values())
    assert s["windows"] > 0


def test_engine_inventory_needs_key_and_returns_no_text(library, monkeypatch):
    monkeypatch.setenv("ENGINE_API_KEY", KEY)
    client = TestClient(engine_service.app)
    assert client.get("/v1/inventory").status_code == 401
    assert client.post("/v1/diagnostic", json={"doi": "10.5555/uz.2024.1"}).status_code == 401
    data = client.get("/v1/inventory", headers={"Authorization": "Bearer " + KEY}).json()
    assert data["total"] == 4 and data["limits"]["corpus_max_sources"] >= 1
    dumped = json.dumps(data, ensure_ascii=False)
    assert "maxfiy matni" not in dumped and "author consent" not in dumped
    empty = client.post("/v1/diagnostic", json={"title": "abc"}, headers={"Authorization": "Bearer " + KEY})
    assert empty.status_code == 422


# ------------------------------------------------------------- diagnostic

def test_diagnose_by_doi_with_sampled_passage(library):
    with connect() as db:
        found = corpus.find_source(db, doi="https://doi.org/10.5555/UZ.2024.1")
        assert len(found) == 1
        result = corpus.diagnose(db, found[0][0])
    assert result["searchable"] and result["fingerprints"] > 0
    assert result["passage"]["found"] and result["passage"]["rank"] == 1 and result["passage"]["source"].startswith("sampled")
    assert "o'qituvchilarning" not in json.dumps(result)                   # the passage is never returned


def test_diagnose_given_passage_not_in_source(library):
    with connect() as db:
        num = corpus.find_source(db, title="ta'lim sifati")[0][0]
        result = corpus.diagnose(db, num, "Bu jumla kutubxonadagi hech bir matnda uchramaydi va shuning uchun topilmaydi.")
    assert result["passage"]["found"] is False and result["passage"]["source"] == "given by the owner"


def test_diagnose_deposit_never_returns_text(library):
    with connect() as db:
        num = corpus.find_source(db, title="Talaba ishi")[0][0]
        result = corpus.diagnose(db, num)
    assert result["deposit"] and result["passage"]["found"]
    assert "maxfiy" not in json.dumps(result, ensure_ascii=False)


def owner_client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    client = TestClient(web.app)
    sign_in(client, user_id=42)
    return client


def test_admin_diagnostic_owner_only(engine, monkeypatch, tmp_path):     # noqa: F811
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.sqlite3"))
    with connect() as db:
        ingest(db, meta(), UZ_LONG)
    stranger = TestClient(web.app)
    assert stranger.post("/admin/diagnostic", data={"doi": "10.5555/uz.2024.1"}).status_code == 404
    client = owner_client(monkeypatch)
    sign_in(client, user_id=7)
    assert client.post("/admin/diagnostic", data={"doi": "10.5555/uz.2024.1"}).status_code == 404
    client = owner_client(monkeypatch)
    page = client.post("/admin/diagnostic", data={"doi": "10.5555/uz.2024.1"}).text
    assert "✅ found (rank 1" in page and "(searchable)" in page and "o'qituvchilarning" not in page
    missing = client.post("/admin/diagnostic", data={"title": "Hech qachon bo'lmagan sarlavha"}).text
    assert "Not in the library" in missing
    short = client.post("/admin/diagnostic", data={"title": "abc"}).text
    assert "at least 8 characters" in short
    dashboard = client.get("/admin").text
    assert "Searchable full-text coverage" in dashboard and "By provider and rights basis" in dashboard


# ------------------------------------------------------------- admin rendering

def test_admin_details_separate_old_disk_errors_and_explain_caps():
    inv = {"total": 10, "windows": 99, "coverage": {"uz": {"sources": 10, "full_texts": 8, "short_texts": 2, "words": 5000}},
           "category_language": {"wikipedia": {"uz": 8}, "journals": {"uz": 2}}, "by_provider": {"wikipedia": 8},
           "by_rights": {"open_license": 10}, "freshness": {}, "limits": {"corpus_max_sources": 3000, "archive_max_sources": 5000,
                                                                          "wikipedia_max_articles": 5},
           "imports": {"cyberleninka": {"finished": "2026-11-10", "error": "[Errno 28] No space left on device",
                                        "error_at": "2026-11-10", "last_success": "2026-11-01"},
                       "uz": {"finished": "2026-11-12", "imported": 3, "error": None, "last_success": "2026-11-12",
                              "failure_reasons": {"download is not a PDF": 4}}},
           "disk_low_now": None}
    page = admin.details(inv)
    assert "Wikipedia is 80% of the library (ceiling 10%)" in page
    assert "at that time; there is enough free space now" in page and "[error at 2026-11-10]" in page
    assert "8 indexed; cap 5 per language (the cap stops new imports only" in page
    assert "download is not a PDF ×4" in page and "Last success" in page
    inv["disk_low_now"] = "Paused: only 10 MB free"
    assert "there is enough free space now" not in admin.details(inv) and "Now: Paused" in admin.details(inv)


def test_status_keeps_last_success_and_error_time(tmp_path):
    import_job.write_status(tmp_path, "uz", {"finished": "2026-11-01T00:00:00+00:00", "error": None})
    import_job.write_status(tmp_path, "uz", {"finished": "2026-11-02T00:00:00+00:00", "error": "disk is full"})
    status = import_job.read_status(tmp_path)["uz"]
    assert status["last_success"] == "2026-11-01T00:00:00+00:00" and status["error_at"] == "2026-11-02T00:00:00+00:00"
    import_job.write_status(tmp_path, "uz", {"finished": "2026-11-03T00:00:00+00:00", "error": None})
    status = import_job.read_status(tmp_path)["uz"]
    assert status["last_success"] == "2026-11-03T00:00:00+00:00" and status["error_at"] is None


# ------------------------------------------------------------- Uzbek works labelled Russian

@pytest.fixture()
def openalex():
    seen = []

    def work(n, title, pdf_path=None, licence="cc-by"):
        return {"id": f"https://openalex.org/W{n}", "doi": f"https://doi.org/10.1/{n}", "display_name": title,
                "publication_year": 2024, "language": "ru", "authorships": [{"author": {"display_name": f"Muallif {n}"}}],
                "best_oa_location": {"license": licence, "pdf_url": base + pdf_path if pdf_path else None,
                                     "landing_page_url": f"https://example.org/{n}"}}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            seen.append(self.path)
            status, body = 404, b"missing"
            if url.path == "/oa/works":
                seen.append(parse_qs(url.query)["filter"][0])
                body = json.dumps({"meta": {"next_cursor": None}, "results": [
                    work(1, "Олий таълим сифати", "/pub/uz.pdf"),                 # Uzbek Cyrillic title and text
                    work(2, "Work two", "/pub/uz.pdf"),                           # neutral title, Uzbek text
                    work(3, "Качество образования в вузы", "/pub/ru3.pdf"),       # Russian title (ы): skipped
                    work(4, "Work four", "/pub/ru.pdf"),                          # Russian text
                    work(5, "Work five", "/pub/uz.pdf", licence="cc-by-nc")]}).encode()   # licence needs review
                status = 200
            elif url.path in ("/pub/uz.pdf", "/pub/ru.pdf", "/pub/ru3.pdf"):
                status, body = 200, pdf(UZ if "uz" in url.path else RU)
            self.send_response(status)
            self.send_header("Content-Type", "application/pdf" if url.path.startswith("/pub/") else "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield base, seen
    server.shutdown()


def local_fetcher():
    from test_oa_import import fetcher
    return fetcher()


def test_uzbek_works_labelled_russian_are_found_and_relabelled(openalex, tmp_path, monkeypatch):
    base, seen = openalex
    monkeypatch.setenv("AUTO_IMPORT", "uz")
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    status = import_job.run_uz_from_ru(batch=10, db_path=tmp_path / "c.db", root=tmp_path / "s", fetcher=local_fetcher(),
                                       api=base + "/oa")
    listing = next(f for f in seen if f.startswith("best_oa_location"))
    assert "language:ru" in listing and "authorships.institutions.country_code:UZ" in listing
    assert "/pub/ru3.pdf" not in seen                                        # Russian title: never downloaded
    with connect(tmp_path / "c.db") as db:
        indexed = {m["title"]: m for m in inventory(db)}
    assert set(indexed) <= {"Олий таълим сифати", "Work two"} and indexed
    assert all(m["language"] == "uz" for m in indexed.values())
    assert status["relabelled"] >= 1 and status["failed"] >= 1               # the Russian text was refused
    assert any("this run imports only uz" in r for r in status["failure_reasons"])
    assert import_job.read_status(tmp_path / "s")["uz-from-ru"]["last_success"]
    review = (tmp_path / "s" / "openalex-uz-from-ru" / "review.csv").read_text("utf-8-sig")
    assert "letter ы" in review and "cc-by-nc" in review                    # skipped title; NC licence kept for review


def test_russian_texts_kept_when_ru_is_imported_too(openalex, tmp_path, monkeypatch):
    base, _ = openalex
    monkeypatch.setenv("AUTO_IMPORT", "uz,ru")
    import_job.run_uz_from_ru(batch=10, db_path=tmp_path / "c.db", root=tmp_path / "s", fetcher=local_fetcher(), api=base + "/oa")
    with connect(tmp_path / "c.db") as db:
        languages = {m["language"] for m in inventory(db)}
    assert languages == {"uz", "ru"}


def test_relabel_pass_settings(monkeypatch):
    monkeypatch.setenv("AUTO_IMPORT", "ru")
    assert import_job.uz_from_ru_settings()[0] is False
    monkeypatch.setenv("AUTO_IMPORT", "uz")
    monkeypatch.delenv("AUTO_IMPORT_UZ_FROM_RU", raising=False)
    assert import_job.uz_from_ru_settings() == (True, 100, 0.05)
    monkeypatch.setenv("AUTO_IMPORT_UZ_FROM_RU", "0")
    assert import_job.uz_from_ru_settings()[0] is False


# ------------------------------------------------------------- chapter-based retrieval and de-duplication

TOPICS = {"Kirish": "mavzuning dolzarbligi maqsad vazifalar ilmiy yangilik amaliy ahamiyat tadqiqot obyekti predmeti",
          "Nazariy asoslar": "raqamli iqtisodiyot tushunchasi tarixiy rivojlanish xorijiy olimlar qarashlari konsepsiyalar tasnifi",
          "Metodologiya": "korrelyatsion regression model ekonometrik baholash panel ma'lumotlar statistik mezonlar tanlanma",
          "Tahlil natijalari": "viloyatlar kesimida investitsiyalar dinamikasi eksport hajmi bandlik ko'rsatkichlari o'sish sur'ati",
          "Xulosa": "takliflar tavsiyalar davlat siyosati qonunchilik takomillashtirish istiqbollar strategik yo'nalishlar"}


def chapter_text(i, name):
    words = TOPICS[name].split()
    return f"\n{i}. {name}\n" + " ".join(
        f"{words[j % len(words)].capitalize()} {' '.join(words[(j + k) % len(words)] for k in range(1, 9))} bo'yicha {j}-holat batafsil yoritiladi."
        for j in range(40))


THESIS = ("Dissertatsiya mavzusi: iqtisodiyotda raqamli transformatsiya\n\nMUNDARIJA\n1. Kirish 3\n2. Nazariy asoslar 10\n3. Tahlil 30\n"
          + "".join(chapter_text(i, name) for i, name in enumerate(TOPICS, 1)))


def test_chapters_found_and_contents_merged():
    parts = discovery.chapters(THESIS)
    assert len(parts) == 5                                   # the table of contents joins the first chapter
    queries, found = discovery.chapter_queries(THESIS, 8)
    assert found == 5 and 6 <= len(queries) <= 8
    assert len({q.lower() for q in queries}) == len(queries)    # no duplicate excerpts
    assert all(len(q.split()) <= discovery.QUERY_WORDS for q in queries)


def test_no_headings_falls_back_to_equal_parts():
    words = " ".join(TOPICS.values()).split()
    text = "Oddiy matn sarlavhasi\n" + " ".join(
        f"{words[i % len(words)].capitalize()} {' '.join(words[(i * 7 + k) % len(words)] for k in range(1, 10))} haqida {i}-jumla." for i in range(900))
    queries, found = discovery.chapter_queries(text, 5)
    assert found == 0 and len(queries) >= 3


def test_results_deduplicated_by_doi_and_title(monkeypatch):
    def fake(query, language, n):
        return [{"id": "https://openalex.org/W1", "display_name": "Pricing-to-market, staggered contracts, and real exchange rate persistence",
                 "doi": "https://doi.org/10.1016/x1", "publication_year": 2001},
                {"id": "https://openalex.org/W2", "display_name": "Pricing-to-Market, Staggered Contracts, and Real Exchange Rate Persistence",
                 "doi": "https://doi.org/10.1142/x2", "publication_year": 2017,
                 "abstract_inverted_index": {"Exporters": [0], "adjust": [1], "prices": [2]}},
                {"id": "https://openalex.org/W3", "display_name": "Another copy", "doi": "https://doi.org/10.1016/X1"}]
    monkeypatch.setattr(discovery, "_search", fake)
    result = discovery.discover("Pricing to market. " * 20, 8, "en")
    assert len(result["sources"]) == 1 and result["sources"][0]["year"] == 2017      # the copy with an abstract is kept
    assert result["duplicates_merged"] >= 2
