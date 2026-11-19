"""Texts stored twice in the library: found (the same text under two records, or an article inside a journal issue),
and only the newer record of a same-text pair removed after the owner types the confirmation; deposits never."""
import random
import time

from fastapi.testclient import TestClient

import app as web
import corpus
import engine_service
from test_archive import engine  # noqa: F401  (fixture)
from test_auth_ai import TOKEN, sign_in
from test_real_eval import WORDS

KEY = "k" * 32


def text(rng, n=40):
    return ". ".join(" ".join(rng.choice(WORDS) for _ in range(12)) for _ in range(n)) + "."


def meta(title, url, rights="open_license"):
    return {"title": title, "authors": "A", "year": 2024, "language": "uz", "url": url, "rights_basis": rights,
            "permission_reference": "CC BY" if rights == "open_license" else "consent"}


def library(db):
    rng = random.Random(7)
    article, other, own = text(rng), text(rng), text(rng)
    corpus.ingest(db, meta("Article A", "https://j.uz/a"), article)
    corpus.ingest(db, meta("Article A (again)", "https://doi.org/10.1/a"), article + " Qo‘shimcha.")   # stored twice
    corpus.ingest(db, meta("Journal issue 3", "https://j.uz/issue3"), text(rng) + " " + other + " " + text(rng))
    corpus.ingest(db, meta("Article B", "https://j.uz/b"), other)                                    # inside the issue
    for i in range(4):
        corpus.ingest(db, meta(f"Unrelated {i}", f"https://j.uz/u{i}"), text(rng))
    corpus.ingest(db, meta("Thesis", "", rights="author_deposit") | {"owner": "user:" + "c" * 24}, own)
    corpus.ingest(db, meta("Thesis copy", "", rights="author_deposit") | {"owner": "user:" + "d" * 24}, own + " Ilova.")


def test_finds_same_text_and_text_inside_another(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    with corpus.connect() as db:
        library(db)
        pairs = corpus.find_duplicates(db)
    kinds = {(p["kind"], p["keep"]["title"], p["extra"]["title"]) for p in pairs}
    assert ("same", "Article A", "Article A (again)") in kinds                 # the older record is kept
    assert ("inside", "Journal issue 3", "Article B") in kinds
    assert not any("Unrelated" in t or "Thesis" in t for k in kinds for t in k[1:])   # deposits are left out
    assert len(pairs) == 2


def test_engine_and_admin_find_and_remove_only_the_extra_copy(engine, monkeypatch, tmp_path):  # noqa: F811
    with corpus.connect() as db:
        library(db)
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.sqlite3"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    client = TestClient(web.app)
    sign_in(client, user_id=42)
    origin = {"Origin": "http://testserver"}
    assert "Texts stored twice" in client.get("/admin").text
    assert "Searching for duplicates" in client.post("/admin/duplicates", data={"action": "find"}, headers=origin).text
    for _ in range(200):
        if engine_service.DUPLICATES["state"] != "running":
            break
        time.sleep(0.05)
    page = client.get("/admin").text
    assert "1 texts stored twice, 1 texts contained in another" in page and "Article A (again)" in page
    r = client.post("/admin/duplicates", data={"action": "remove", "confirm_text": "yes"}, headers=origin)
    assert "Nothing was changed" in r.text                                        # the words must be typed
    r = client.post("/admin/duplicates", data={"action": "remove", "confirm_text": "delete copies"}, headers=origin)
    assert "1 extra copies removed" in r.text
    with corpus.connect() as db:
        titles = {m["title"] for m in map(corpus.json.loads, (r[0] for r in db.execute("SELECT metadata FROM sources")))}
    assert "Article A (again)" not in titles and {"Article A", "Article B", "Journal issue 3", "Thesis", "Thesis copy"} <= titles
    assert TestClient(web.app).post("/admin/duplicates", data={"action": "find"}).status_code in (401, 403, 404)
