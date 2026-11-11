"""Owner dashboard, backups and the privacy page."""
import gzip
import hashlib
import hmac
import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app as web
import corpus
import engine_client
import engine_service

TOKEN = "123456789:AAtest-token-for-plagiq-tests_0123456789"
TEXT = ("Bitiruv ishi Samarqand viloyatida turizm xizmatlari sifatini oshirish yo‘llarini o‘rganadi va "
        "mehmonxonalar faoliyatini baholash usullarini taklif qiladi. ") * 8


def signed_in(user_id):
    client = TestClient(web.app)
    data = {"id": user_id, "first_name": "Owner", "auth_date": int(time.time())}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    client.post("/auth/telegram", json=data)
    return client


@pytest.fixture()
def site(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "1, 2")
    monkeypatch.setenv("ENGINE_API_KEY", "k" * 32)
    monkeypatch.setenv("ENGINE_URL", "http://engine")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "index.db"))
    for name in ("PANGRAM_API_KEY", "YANDEX_API_KEY", "BRAVE_API_KEY", "FULLTEXT_OPENALEX_PDF", "VERIFY_SECRET"):
        monkeypatch.delenv(name, raising=False)
    real = httpx.AsyncClient
    monkeypatch.setattr(engine_client.httpx, "AsyncClient",
                        lambda **kw: real(**kw, transport=httpx.ASGITransport(app=engine_service.app)))
    db = corpus.connect(str(tmp_path / "index.db"))
    corpus.ingest(db, {"title": "Turizm", "authors": "Aliyev B.", "year": 2024, "language": "uz", "url": "",
                       "rights_basis": "author_deposit", "permission_reference": "consent",
                       "owner": "user:" + "e" * 24}, TEXT)
    corpus.ingest(db, {"title": "Ochiq maqola", "authors": "X", "year": 2023, "language": "uz", "url": "https://j.uz/1",
                       "rights_basis": "open_license", "permission_reference": "CC BY"}, TEXT + " Qo‘shimcha.")
    web.LIBRARY_CACHE.update(at=-1e9, data=None)
    return tmp_path


def test_only_owners_see_admin(site):
    assert TestClient(web.app).get("/admin").status_code == 404
    assert signed_in(99).get("/admin").status_code == 404
    page = signed_in(1).get("/admin").text
    for expected in ("Setup checklist", "✅</td><th>Telegram sign-in", "❌</td><th>AI detector", "PANGRAM_API_KEY",
                     "✅</td><th>Private engine connected", "Authors&#x27; archive</th><td>1", "Total</th><td><b>2",
                     "Engine disk", "Accounts</th><td>2"):
        assert expected in page, expected
    assert TOKEN not in page and "k" * 32 not in page                           # never shows secrets


def test_public_library_hides_owner_details(site):
    data = TestClient(web.app).get("/api/library").json()
    assert data["available"] and data["by_source"] == {"archive": 1, "journals": 1}
    assert "disk" not in data and "imports" not in data


def test_archive_backup_round_trip(site, tmp_path):
    owner = signed_in(2)
    assert signed_in(99).get("/admin/backup/archive.jsonl.gz").status_code == 404
    response = owner.get("/admin/backup/archive.jsonl.gz")
    assert response.status_code == 200 and "plagiq-archive-" in response.headers["content-disposition"]
    lines = [json.loads(l) for l in gzip.decompress(response.content).decode().splitlines()]
    assert [l["metadata"]["title"] for l in lines] == ["Turizm"] and lines[0]["text"] == TEXT   # deposits only
    (tmp_path / "backup.jsonl.gz").write_bytes(response.content)
    fresh = corpus.connect(str(tmp_path / "fresh.db"))
    assert corpus.restore_deposits(fresh, tmp_path / "backup.jsonl.gz") == 1
    found = corpus.search(fresh, "Mening kirishim. " + TEXT[:300], "uz")["sources"]
    assert found[0]["title"] == "Turizm" and found[0]["rights_basis"] == "author_deposit"


def test_accounts_backup(site, tmp_path):
    response = signed_in(1).get("/admin/backup/accounts.sqlite3")
    assert response.status_code == 200
    (tmp_path / "a.sqlite3").write_bytes(response.content)
    assert sqlite3.connect(tmp_path / "a.sqlite3").execute("SELECT COUNT(*) FROM users").fetchone() == (1,)


def test_privacy_page_public_in_three_languages(monkeypatch):
    monkeypatch.setenv("PRIVATE_PILOT_USER", "pilot")
    monkeypatch.setenv("PRIVATE_PILOT_PASSWORD", "p" * 20)
    page = TestClient(web.app).get("/privacy")
    assert page.status_code == 200
    for expected in ("Maxfiylik siyosati", "Политика конфиденциальности", "Privacy notice", "ZRU-547", "Pangram", "DRAFT"):
        assert expected in page.text
