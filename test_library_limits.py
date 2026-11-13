"""Library limits: archive deposits have their own room, Wikipedia has a per-language cap, sources are grouped."""
import bz2

import pytest

import corpus
import wikipedia_import


@pytest.fixture(autouse=True)
def _no_share_ceiling(monkeypatch):
    monkeypatch.setenv("WIKIPEDIA_MAX_SHARE", "100")      # these tests start from an empty library


TEXT = ("Bu matn kutubxona chegaralarini sinash uchun yozilgan bo‘lib, har bir manba alohida va yetarlicha uzun. " * 12)


def meta(n, **extra):
    return {"title": f"Source {n}", "authors": "A", "year": 2024, "language": "uz", "url": f"https://journal.uz/{n}",
            "rights_basis": "open_license", "permission_reference": "CC BY", **extra}


def test_imports_and_deposits_have_separate_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_MAX_SOURCES", "2")
    monkeypatch.setenv("ARCHIVE_MAX_SOURCES", "1")
    db = corpus.connect(str(tmp_path / "c.db"))
    corpus.ingest(db, meta(1), TEXT + "1")
    corpus.ingest(db, meta(2), TEXT + "2")
    with pytest.raises(ValueError, match="CORPUS_MAX_SOURCES"):
        corpus.ingest(db, meta(3), TEXT + "3")
    deposit = meta(4, rights_basis="author_deposit", url="", owner="user:" + "a" * 24)
    corpus.ingest(db, deposit, TEXT + "4")                                   # full library does not block a deposit
    with pytest.raises(ValueError, match="ARCHIVE_MAX_SOURCES"):
        corpus.ingest(db, {**deposit, "title": "Second"}, TEXT + "5")


def test_categories():
    assert corpus.category({"url": "https://uz.wikipedia.org/wiki/X", "source_type": "wikipedia"}) == "wikipedia"
    assert corpus.category({"url": "https://cyberleninka.ru/article/n/x"}) == "cyberleninka"
    assert corpus.category({"url": "https://inscience.uz/index.php/socinov/article/view/4"}) == "journals"
    assert corpus.category({"rights_basis": "author_deposit"}) == "archive"
    assert corpus.category({"rights_basis": "institution_deposit"}) == "university"
    assert corpus.category({"url": "https://doi.org/10.1/x"}) == "other"


def dump(n):
    pages = "".join(
        f"<page><title>Maqola {i}</title><ns>0</ns><id>{i}</id><revision><id>{100 + i}</id>"
        f"<timestamp>2025-01-01T00:00:00Z</timestamp><text>{TEXT} raqam{i}</text></revision></page>"
        for i in range(1, n + 1))
    return bz2.compress(f"<mediawiki>{pages}</mediawiki>".encode())


def test_wikipedia_stops_at_its_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIPEDIA_MAX_ARTICLES", "3")
    source = tmp_path / "uzwiki.xml.bz2"
    source.write_bytes(dump(6))
    status = wikipedia_import.run("uz", batch=100, min_words=20, source=str(source), db_path=str(tmp_path / "c.db"),
                                  root=tmp_path)
    assert status["imported"] == 3 and status["error"] is None and "WIKIPEDIA_MAX_ARTICLES" in status["note"]
    again = wikipedia_import.run("uz", batch=100, min_words=20, source=str(source), db_path=str(tmp_path / "c.db"),
                                 root=tmp_path)
    assert again["imported"] == 0                                             # still capped
    assert corpus.count_category(corpus.connect(str(tmp_path / "c.db")), "wikipedia", "uz") == 3


def test_library_summary_by_source(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from fastapi.testclient import TestClient
    import engine_client
    import engine_service
    monkeypatch.setenv("ENGINE_API_KEY", "k" * 32)
    monkeypatch.setenv("ENGINE_URL", "http://engine")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "index.db"))
    db = corpus.connect(str(tmp_path / "index.db"))
    corpus.ingest(db, meta(1), TEXT + "1")
    corpus.ingest(db, meta(2, url="https://cyberleninka.ru/article/n/x"), TEXT + "2")
    corpus.ingest(db, meta(3, rights_basis="author_deposit", url="", owner="user:" + "b" * 24), TEXT + "3")
    data = TestClient(engine_service.app, headers={"Authorization": "Bearer " + "k" * 32}).get("/v1/library").json()
    assert data["by_source"] == {"journals": 1, "cyberleninka": 1, "archive": 1}
    real = httpx.AsyncClient
    monkeypatch.setattr(engine_client.httpx, "AsyncClient",
                        lambda **kw: real(**kw, transport=httpx.ASGITransport(app=engine_service.app)))
    assert asyncio.run(engine_client.library())["by_source"] == {"journals": 1, "cyberleninka": 1, "archive": 1}
