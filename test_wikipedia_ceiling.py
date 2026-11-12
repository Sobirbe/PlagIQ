"""Wikipedia as a small supplementary collection: the share ceiling on new imports, and owner-controlled
pruning that keeps the longest articles and never touches academic texts or deposits."""
import json

import pytest
from fastapi.testclient import TestClient

import admin
import app as web
import corpus
import engine_service
import wikipedia_import
from test_archive import engine  # noqa: F401  (fixture: engine on a temporary corpus, website wired to it)
from test_auth_ai import TOKEN, sign_in
from test_library_limits import TEXT, dump, meta

KEY = "k" * 32


def academic(db, n):
    for i in range(n):
        corpus.ingest(db, meta(100 + i, url=f"https://journal.example.uz/{i}",
                               permission_reference="CC BY via OAI feed"), TEXT + f" maqola {i}")


def wiki(db, lang, n, words=lambda i: 40 + i):
    for i in range(n):
        body = " ".join((f"so'z{i}x{j}" if lang == "uz" else f"слово{i}к{j}") for j in range(words(i)))
        corpus.ingest(db, meta(500 + i, language=lang, url=f"https://{lang}.wikipedia.org/wiki/A{i}", source_type="wikipedia",
                               title=f"Wiki {lang} {i}"),
                      ("Bu ensiklopediya maqolasi " if lang == "uz" else "Это статья энциклопедии ") + body)


def test_share_ceiling_stops_new_wikipedia_imports(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIPEDIA_MAX_SHARE", "10")
    monkeypatch.delenv("WIKIPEDIA_MAX_ARTICLES", raising=False)
    db_path = str(tmp_path / "c.db")
    with corpus.connect(db_path) as db:
        academic(db, 9)
    source = tmp_path / "uzwiki.xml.bz2"
    source.write_bytes(dump(6))
    status = wikipedia_import.run("uz", batch=100, min_words=20, source=str(source), db_path=db_path, root=tmp_path)
    assert status["imported"] == 1 and "WIKIPEDIA_MAX_SHARE=10%" in status["note"] and status["error"] is None
    with corpus.connect(db_path) as db:
        assert corpus.count_category(db, "wikipedia") == 1 and corpus.count_category(db, "journals") == 9
    import import_job
    assert import_job.read_status(tmp_path)["wikipedia-uz"]["last_success"]    # written through import_job.write_status


def test_empty_library_gets_no_wikipedia_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIPEDIA_MAX_SHARE", raising=False)
    source = tmp_path / "uzwiki.xml.bz2"
    source.write_bytes(dump(3))
    status = wikipedia_import.run("uz", batch=100, min_words=20, source=str(source), db_path=str(tmp_path / "c.db"), root=tmp_path)
    assert status["imported"] == 0 and "share limit" in status["note"]
    monkeypatch.setenv("WIKIPEDIA_MAX_SHARE", "100")
    assert wikipedia_import.max_share() == 1.0


def test_prune_keeps_longest_and_never_touches_academic_or_deposits(tmp_path):
    db = corpus.connect(str(tmp_path / "c.db"))
    academic(db, 3)
    corpus.ingest(db, meta(900, rights_basis="author_deposit", url="", owner="user:" + "a" * 24), TEXT + " talaba")
    wiki(db, "uz", 5)
    wiki(db, "ru", 3)
    plan = corpus.prune_collection(db, "wikipedia", 2, dry_run=True)
    assert plan["remove"] == {"uz": 3, "ru": 1} and plan["keep"] == {"uz": 2, "ru": 2}
    assert corpus.count_category(db, "wikipedia") == 8                       # preview changed nothing
    removed_titles = {r["title"] for r in plan["removed"]}
    assert removed_titles == {"Wiki uz 0", "Wiki uz 1", "Wiki uz 2", "Wiki ru 0"}  # the shortest go
    corpus.prune_collection(db, "wikipedia", 2, dry_run=False, chunk=2)
    assert corpus.count_category(db, "wikipedia") == 4 and corpus.count_category(db, "journals") == 3
    assert corpus.count_category(db, "archive") == 1
    gone = " ".join(f"so'z0x{j}" for j in range(20))
    assert not corpus.search(db, "Bu ensiklopediya maqolasi " + gone, "uz")["sources"]   # fingerprints removed too
    with pytest.raises(ValueError):
        corpus.prune_collection(db, "journals", 0)


def test_engine_prune_preview_confirm_and_record(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_API_KEY", KEY)
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    with corpus.connect() as db:
        academic(db, 2)
        wiki(db, "uz", 4)
    client = TestClient(engine_service.app)
    auth_header = {"Authorization": "Bearer " + KEY}
    assert client.post("/v1/prune", json={"keep_per_language": 1}).status_code == 401
    assert client.post("/v1/prune", json={"collection": "journals", "keep_per_language": 0}, headers=auth_header).status_code == 422
    preview = client.post("/v1/prune", json={"keep_per_language": 1}, headers=auth_header).json()
    assert preview["remove"] == {"uz": 3} and preview["confirmed"] is False and preview["collection_after"] == 1
    done = client.post("/v1/prune", json={"keep_per_language": 1, "confirm": True}, headers=auth_header).json()
    assert done["confirmed"] and done["total_after"] == 3 and done["collection_after"] == 1
    record = json.loads((tmp_path / "staging" / done["record"]).read_text("utf-8"))
    assert len(record) == 3 and {"title", "url", "language", "words"} == set(record[0])


def test_admin_prune_owner_only_and_needs_typed_confirmation(engine, monkeypatch, tmp_path):     # noqa: F811
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.sqlite3"))
    with corpus.connect() as db:
        academic(db, 2)
        wiki(db, "uz", 4)
    assert TestClient(web.app).post("/admin/prune", data={"keep": "1"}).status_code == 404
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    client = TestClient(web.app)
    sign_in(client, user_id=42)
    page = client.post("/admin/prune", data={"keep": "1", "confirm_text": "delete"}).text
    assert "did not match" in page and "Preview only: nothing was removed" in page
    with corpus.connect() as db:
        assert corpus.count_category(db, "wikipedia") == 4
    page = client.post("/admin/prune", data={"keep": "1", "confirm_text": "DELETE WIKIPEDIA"}).text
    assert "Removed. List saved as pruned-wikipedia-" in page and "Wikipedia 1 of 3 texts (33%)" in page
    with corpus.connect() as db:
        assert corpus.count_category(db, "wikipedia") == 1 and corpus.count_category(db, "journals") == 2
    dashboard = client.get("/admin").text
    assert "Wikipedia: 1 of 3 texts (33%); ceiling 10%" in dashboard


def test_prune_form_shows_share_against_ceiling():
    inv = {"total": 2288, "category_language": {"wikipedia": {"uz": 700, "ru": 1124}}, "limits": {"wikipedia_max_share": 0.1}}
    html = admin.prune_form(inv)
    assert "Wikipedia: 1,824 of 2,288 texts (80%); ceiling 10%" in html and 'class="warn"' in html
