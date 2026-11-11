"""Protected archive of deposited works: library rules, engine API, website flow (real engine in-process)."""
import hashlib
import hmac
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import corpus
import engine_client
import engine_service

THESIS = ("Magistrlik dissertatsiyasi Farg‘ona vodiysidagi suv resurslarini boshqarish tizimini o‘rganadi va "
          "fermer xo‘jaliklarining sug‘orish samaradorligini oshirish yo‘llarini tahlil qiladi. ") * 8
COPY = "Kirish qismi boshqacha yozilgan. " + THESIS[:400] + " Xulosa ham mustaqil yozilgan."
TOKEN = "123456789:AAtest-token-for-plagiq-tests_0123456789"


def deposit_meta(owner="user:" + "a" * 24, **extra):
    return {"title": "Suv resurslari", "authors": "Dilnoza Karimova", "year": 2026, "language": "uz", "url": "",
            "rights_basis": "author_deposit", "permission_reference": "consent", "owner": owner, "collection": "user", **extra}


def test_library_rules_for_deposits(tmp_path):
    db = corpus.connect(str(tmp_path / "c.db"))
    with pytest.raises(ValueError, match="owner"):
        corpus.ingest(db, deposit_meta(owner=""), THESIS)
    ident = corpus.ingest(db, deposit_meta(), THESIS)
    report = corpus.search(db, COPY, "uz")
    src = report["sources"][0]
    assert src["url"] == "" and src["owner"] == "user:" + "a" * 24 and src["title"] == "Suv resurslari"
    assert all(p["source_excerpt"] == "" for p in src["passages"]) and src["matched_words"] > 30   # text never shown
    assert corpus.search(db, COPY, "uz", exclude={ident})["sources"] == []                          # same document skipped
    published = corpus.ingest(db, {**deposit_meta(), "rights_basis": "open_license", "url": "https://j.uz/1",
                                   "title": "Published"}, THESIS + " Nashr.")
    found = corpus.search(db, THESIS + " Nashr.", "uz", exclude={published})["sources"]
    assert found[0]["title"] == "Published"                                    # published copies are never skipped
    assert [i for i, _ in corpus.deposits(db, "user:" + "a" * 24)] == [ident]
    assert not corpus.remove_deposit(db, ident, "user:" + "b" * 24)                                  # only the owner
    assert corpus.remove_deposit(db, ident, "user:" + "a" * 24)
    assert [x["title"] for x in corpus.search(db, COPY, "uz")["sources"]] == ["Published"]


@pytest.fixture()
def engine(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_API_KEY", "k" * 32)
    monkeypatch.setenv("ENGINE_URL", "http://engine")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "index.db"))
    real = httpx.AsyncClient
    monkeypatch.setattr(engine_client.httpx, "AsyncClient",
                        lambda **kw: real(**kw, transport=httpx.ASGITransport(app=engine_service.app)))
    return TestClient(engine_service.app, headers={"Authorization": "Bearer " + "k" * 32})


def test_engine_archive_api(engine):
    body = {"text": THESIS, "language": "uz", "title": "Suv", "authors": "D. K.", "year": 2026,
            "owner": "user:" + "c" * 24, "rights_basis": "author_deposit", "permission_reference": "consent"}
    assert TestClient(engine_service.app).post("/v1/archive", json=body).status_code == 401
    assert engine.post("/v1/archive", json={**body, "rights_basis": "open_license"}).status_code == 422
    assert engine.post("/v1/archive", json={**body, "owner": "../etc"}).status_code == 422
    ident = engine.post("/v1/archive", json=body).json()["id"]
    items = engine.get("/v1/archive", params={"owner": "user:" + "c" * 24}).json()["items"]
    assert [i["id"] for i in items] == [ident] and "text" not in items[0]
    assert engine.delete(f"/v1/archive/{ident}", params={"owner": "user:" + "d" * 24}).status_code == 404
    assert engine.delete(f"/v1/archive/{ident}", params={"owner": "user:" + "c" * 24}).json() == {"deleted": True}


def signed_in(client, user_id):
    data = {"id": user_id, "first_name": "User", "last_name": str(user_id), "auth_date": int(time.time())}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    assert client.post("/auth/telegram", json=data).status_code == 200
    return client


def test_deposit_then_later_copy_is_found_in_academic_check(engine, monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("FULLTEXT_FETCH", "0")
    monkeypatch.setattr(web, "discover", lambda text, *a: dict(sources=[], status="searched", message="", queries=1,
                                                               query_texts=["q"]))
    web.RATE.clear(); web.results.clear()
    author = signed_in(TestClient(web.app), 1)
    assert TestClient(web.app).get("/health").json()["archive"] is True
    # deposit needs consent
    form = {"mode": "academic", "language": "uz", "consent": "true", "archive": "true", "archive_title": "Suv resurslari"}
    refused = author.post("/api/check", data=form, files={"submission": ("t.docx.txt", THESIS.encode())})
    assert refused.json()["code"] == "archive_consent_required"
    first = author.post("/api/check", data={**form, "archive_consent": "true"},
                        files={"submission": ("t.txt", THESIS.encode())}).json()
    assert first["archive"]["deposited"] is True and first["archive"]["title"] == "Suv resurslari"
    # the author re-checks the same document: not matched against itself
    web.RATE.clear()
    again = author.post("/api/check", data={"mode": "academic", "language": "uz", "consent": "true"},
                        files={"submission": ("t.txt", THESIS.encode())}).json()
    assert again["percent"] == 0 and again["library"]["searched"] is True
    # another student copies part of it
    web.RATE.clear()
    other = signed_in(TestClient(web.app), 2)
    data = other.post("/api/check", data={"mode": "academic", "language": "uz", "consent": "true"},
                      files={"submission": ("c.txt", COPY.encode())}).json()
    src = data["sources"][0]
    assert data["percent"] > 50 and src["coverage"] == "archive" and src["title"] == "Suv resurslari"
    assert src["authors"] == "User 1" and src["url"] == "" and all(p["source_excerpt"] == "" for p in src["passages"])
    assert "owner" not in src and data["library"]["matched_sources"] == 1
    # the author sees the copy of their own deposit labelled as their own work
    web.RATE.clear()
    own = author.post("/api/check", data={"mode": "academic", "language": "uz", "consent": "true"},
                      files={"submission": ("c.txt", COPY.encode())}).json()
    assert own["sources"][0]["coverage"] == "own_archive"
    # list, then erasure through account deletion
    items = author.get("/api/archive").json()["items"]
    assert len(items) == 1 and items[0]["title"] == "Suv resurslari"
    assert other.delete(f"/api/archive/{'0' * 64}").status_code == 404
    assert author.delete("/api/me").json() == {"deleted": True}
    web.RATE.clear()
    after = other.post("/api/check", data={"mode": "academic", "language": "uz", "consent": "true"},
                       files={"submission": ("c.txt", COPY.encode())}).json()
    assert not any(s.get("coverage") == "archive" for s in after["sources"]) and after["percent"] is None  # library empty again


def test_archive_needs_sign_in(engine, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    web.RATE.clear()
    r = TestClient(web.app).post("/api/check", data={"mode": "reference", "language": "uz", "reference_text": THESIS,
                                                     "archive": "true", "archive_consent": "true"},
                                 files={"submission": ("t.txt", THESIS.encode())})
    assert r.status_code == 401 and r.json()["code"] == "login_required"
