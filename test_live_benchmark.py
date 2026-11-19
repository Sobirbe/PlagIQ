"""The accuracy test on the live library, run from /admin: the engine runs evaluation/real_eval.py in the background
(each search under the search lock), keeps the result on the volume, and /admin starts it and shows the table."""
import random
import time

from fastapi.testclient import TestClient

import app as web
import corpus
import engine_client
import engine_service
from test_archive import engine  # noqa: F401  (fixture)
from test_auth_ai import TOKEN, sign_in
from test_real_eval import WORDS

KEY = "k" * 32


def library(n=12):
    rng = random.Random(1)
    with corpus.connect() as db:
        for i in range(n):
            text = ". ".join(" ".join(rng.choice(WORDS) for _ in range(12)) for _ in range(40)) + "."
            corpus.ingest(db, {"title": f"T{i}", "authors": "A", "year": 2024, "language": "uz", "url": f"https://j.uz/{i}",
                               "rights_basis": "open_license", "permission_reference": "CC BY"}, text)


def wait_done(client):
    for _ in range(300):
        data = client.get("/v1/evaluation", headers={"Authorization": "Bearer " + KEY}).json()
        if data["state"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("did not finish")


def test_engine_runs_it_in_the_background_and_keeps_the_result(engine):  # noqa: F811
    library()
    client = TestClient(engine_service.app)
    auth = {"Authorization": "Bearer " + KEY}
    assert client.post("/v1/evaluation", json={"per_language": 6}).status_code in (401, 403)      # owner only
    assert client.post("/v1/evaluation", json={"per_language": 6}, headers=auth).json()["state"] == "running"
    data = wait_done(client)
    assert data["state"] == "done" and data["done"] == data["total"] > 0, data
    assert "Run from /admin on the live library" in data["result"] and "| uz | copied unchanged | 6 |" in data["result"]
    assert engine_service.evaluation_path().exists()
    assert client.post("/v1/evaluation", json={"per_language": 999}, headers=auth).status_code == 422


def test_admin_starts_it_and_shows_the_table(engine, monkeypatch, tmp_path):  # noqa: F811
    library()
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.sqlite3"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    client = TestClient(web.app)
    sign_in(client, user_id=42)
    page = client.get("/admin").text
    assert "Accuracy on the live library" in page and "No test has been run on the live library yet" in page
    r = client.post("/admin/evaluation", data={"per_language": "6"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and "Accuracy test started" in r.text
    wait_done(TestClient(engine_service.app))
    page = client.get("/admin").text
    assert "<th>Language</th>" in page and "<td>copied unchanged</td>" in page and "<script" not in page.split("accuracy")[1][:20000]


def test_markdown_is_escaped():
    import admin
    out = admin.markdown_html("# T\n\n| a | <b>x</b> |\n|---|---|\n| 1 | **2** |")
    assert "&lt;b&gt;x&lt;/b&gt;" in out and "<td><b>2</b></td>" in out and out.startswith("<h3>T</h3>")
