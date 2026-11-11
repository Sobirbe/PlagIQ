"""University deposits: configuration, web upload by admins only, bulk CSV import, display."""
import csv
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app as web
import corpus
import engine_client
import engine_service
import institutions

TOKEN = "123456789:AAtest-token-for-plagiq-tests_0123456789"
THESIS = ("Bitiruv malakaviy ishi Toshkent viloyatidagi kichik biznes subyektlarining kredit resurslaridan "
          "foydalanish samaradorligini baholash usullarini tahlil qiladi va takliflar beradi. ") * 8
COPY = "Mening kirish qismim. " + THESIS[:420] + " Mening xulosam."
CONFIG = json.dumps([{"name": "TDIU", "agreement": "No. 12/2026", "admins": [77]},
                     {"name": "Bad"}, {"name": "SamDU", "agreement": "No. 3", "admins": ["x"]}])


def test_configuration_and_filenames(monkeypatch):
    monkeypatch.setenv("INSTITUTIONS", CONFIG)
    assert institutions.configured() == {"TDIU": {"agreement": "No. 12/2026", "admins": {77}},
                                         "SamDU": {"agreement": "No. 3", "admins": set()}}
    assert institutions.for_user({"id": 77}) == [{"name": "TDIU", "agreement": "No. 12/2026"}]
    assert not institutions.allowed({"id": 78}, "TDIU") and not institutions.allowed(None, "TDIU")
    assert institutions.from_filename("Karimova D. - Suv resurslari_2024.docx") == ("Karimova D.", "Suv resurslari 2024")
    assert institutions.from_filename("bmi.pdf") == ("", "bmi")
    monkeypatch.setenv("INSTITUTIONS", "not json")
    assert institutions.configured() == {}


@pytest.fixture()
def site(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_API_KEY", "k" * 32)
    monkeypatch.setenv("ENGINE_URL", "http://engine")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("LOGIN_REQUIRED", "0")
    monkeypatch.setenv("USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("INSTITUTIONS", CONFIG)
    real = httpx.AsyncClient
    monkeypatch.setattr(engine_client.httpx, "AsyncClient",
                        lambda **kw: real(**kw, transport=httpx.ASGITransport(app=engine_service.app)))
    web.RATE.clear(); web.results.clear()


def signed_in(user_id):
    client = TestClient(web.app)
    data = {"id": user_id, "first_name": "Staff", "auth_date": int(time.time())}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    client.post("/auth/telegram", json=data)
    return client


def test_admin_deposits_and_students_copy_is_found(site):
    admin, stranger = signed_in(77), signed_in(78)
    assert admin.get("/api/institution").json() == {"enabled": True, "institutions": [{"name": "TDIU", "agreement": "No. 12/2026"}]}
    assert stranger.get("/api/institution").json()["institutions"] == []
    files = {"file": ("Rahimov A. - Kichik biznes kreditlari.docx.txt", THESIS.encode())}
    assert stranger.post("/api/institution/deposit", data={"institution": "TDIU", "year": "2023"}, files=files).status_code == 403
    done = admin.post("/api/institution/deposit", data={"institution": "TDIU", "year": "2023"},
                      files={"file": ("Rahimov A. - Kichik biznes kreditlari.txt", THESIS.encode())}).json()
    assert done["authors"] == "Rahimov A." and done["title"] == "Kichik biznes kreditlari" and done["language"] == "uz"
    result = TestClient(web.app).post("/api/check", data={"mode": "corpus", "language": "uz", "consent": "true"},
                                      files={"submission": ("c.txt", COPY.encode())}).json()
    src = result["sources"][0]
    assert result["percent"] > 50 and src["coverage"] == "archive" and src["rights_basis"] == "institution_deposit"
    assert src["collection"] == "TDIU" and src["authors"] == "Rahimov A." and src["url"] == ""
    assert all(p["source_excerpt"] == "" for p in src["passages"])
    items = admin.get("/api/institution/archive", params={"institution": "TDIU"}).json()["items"]
    assert [i["title"] for i in items] == ["Kichik biznes kreditlari"]
    assert stranger.get("/api/institution/archive", params={"institution": "TDIU"}).status_code == 403
    assert stranger.delete(f"/api/institution/archive/{items[0]['id']}", params={"institution": "TDIU"}).status_code == 403
    assert admin.delete(f"/api/institution/archive/{items[0]['id']}", params={"institution": "TDIU"}).json() == {"deleted": True}


def test_bulk_import_from_csv(tmp_path, monkeypatch):
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "w1.txt").write_text(THESIS, "utf-8")
    (folder / "w2.txt").write_text(("Выпускная работа анализирует кредитование малого бизнеса в регионе и предлагает меры "
                                    "по повышению доступности финансовых ресурсов для предпринимателей. ") * 6, "utf-8")
    with open(tmp_path / "works.csv", "w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["file", "title", "authors", "year", "language"])
        w.writerow(["w1.txt", "Kichik biznes", "Rahimov A.", "2023", ""])
        w.writerow(["w2.txt", "Кредитование", "Петров И.", "2022", "ru"])
    db = str(tmp_path / "c.db")
    args = ["--institution", "TDIU", "--agreement", "No. 12/2026", "--csv", str(tmp_path / "works.csv"),
            "--folder", str(folder), "--db", db]
    assert institutions.main(args + ["--check"]) == 0 and corpus.inventory(corpus.connect(db)) == []
    assert institutions.main(args) == 0
    rows = corpus.inventory(corpus.connect(db))
    assert sorted((r["language"], r["rights_basis"], r["collection"], r["owner"]) for r in rows) == [
        ("ru", "institution_deposit", "TDIU", institutions.owner_id("TDIU")),
        ("uz", "institution_deposit", "TDIU", institutions.owner_id("TDIU"))]
    assert all(r["permission_reference"] == "TDIU: agreement No. 12/2026" for r in rows)
    assert not (folder / ".plagiq-institution-manifest.json").exists()
    with open(tmp_path / "bad.csv", "w", encoding="utf-8") as handle:
        handle.write("file,title,authors,year\n../secret.txt,x,y,2020\n")
    with pytest.raises(ValueError, match="file not found"):
        institutions.main(["--institution", "TDIU", "--agreement", "x", "--csv", str(tmp_path / "bad.csv"),
                           "--folder", str(folder), "--db", db])
