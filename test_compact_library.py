"""Compacting the engine's library file: the space left by removed texts goes back to the disk, every remaining text
stays searchable, nothing changes without the typed confirmation, and too little free space is refused."""
import os
from collections import namedtuple

from fastapi.testclient import TestClient

import app as web
import corpus
import engine_service
from test_archive import engine  # noqa: F401  (fixture)
from test_auth_ai import TOKEN, sign_in
from test_library_limits import TEXT
from test_wikipedia_ceiling import KEY, academic, wiki

AUTH = {"Authorization": "Bearer " + KEY}


def library(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_API_KEY", KEY)
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    with corpus.connect() as db:
        academic(db, 3)
        wiki(db, "uz", 60, words=lambda i: 400)
    return TestClient(engine_service.app)


def test_compact_shrinks_file_and_keeps_every_text(tmp_path, monkeypatch):
    client = library(tmp_path, monkeypatch)
    assert client.post("/v1/compact", json={"confirm": True}).status_code == 401
    client.post("/v1/prune", json={"keep_per_language": 0, "confirm": True}, headers=AUTH)
    path = str(tmp_path / "c.db")
    with corpus.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    big = os.path.getsize(path)
    preview = client.post("/v1/compact", json={}, headers=AUTH).json()
    assert preview["compacted"] is False and preview["unused_bytes"] > 0 and preview["possible"]
    assert os.path.getsize(path) == big                                     # preview changes nothing
    done = client.post("/v1/compact", json={"confirm": True}, headers=AUTH).json()
    assert done["compacted"] and done["texts"] == 3 and done["db_bytes_after"] < big / 2
    with corpus.connect() as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert corpus.count_category(db, "journals") == 3 and corpus.count_category(db, "wikipedia") == 0
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not os.path.exists(path + ".compact")
    with corpus.connect() as db:                                             # still searchable
        assert corpus.search(db, TEXT + " maqola 1", "uz")["sources"]


def test_refused_without_enough_free_space(tmp_path, monkeypatch):
    client = library(tmp_path, monkeypatch)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(engine_service.shutil, "disk_usage", lambda p: Usage(10, 10, 0))
    path = str(tmp_path / "c.db")
    before = os.path.getsize(path)
    result = client.post("/v1/compact", json={"confirm": True}, headers=AUTH).json()
    assert result["possible"] is False and result["compacted"] is False and os.path.getsize(path) == before


def test_admin_compact_owner_only_and_typed_confirmation(engine, monkeypatch, tmp_path):     # noqa: F811
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.sqlite3"))
    with corpus.connect() as db:
        academic(db, 2)
        wiki(db, "uz", 30, words=lambda i: 300)
    assert TestClient(web.app).post("/admin/compact", data={"confirm": "1", "confirm_text": "COMPACT"}).status_code == 404
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    client = TestClient(web.app)
    sign_in(client, user_id=42)
    assert "Library file size" in client.get("/admin").text
    client.post("/admin/prune", data={"keep": "0", "confirm_text": "DELETE WIKIPEDIA"})
    page = client.post("/admin/compact", data={"confirm": "1", "confirm_text": "compact please"}).text
    assert "Type COMPACT to compact. Nothing was changed" in page and "can be given back" in page
    page = client.post("/admin/compact", data={"confirm": "1", "confirm_text": "COMPACT"}).text
    assert "Library compacted." in page and "<b>Done.</b>" in page and "2 texts" in page
