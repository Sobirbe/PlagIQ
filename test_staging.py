"""Disk housekeeping: staged files deleted after import, leftovers retried or purged, low-disk pause."""
import json
from collections import namedtuple

import corpus
import staging

TEXT = "Ushbu maqola suv resurslaridan foydalanish samaradorligini oshirish masalalarini tahlil qiladi. " * 10


def stage(folder, n, prefix="w"):
    (folder / "files").mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(n):
        name = f"files/{prefix}{i}.txt"
        (folder / name).write_text(TEXT + f" {prefix}{i}", "utf-8")
        entries.append({"title": f"T{i}", "authors": "A", "year": 2024, "language": "uz", "url": f"https://j.uz/{prefix}{i}",
                        "rights_basis": "open_license", "permission_reference": "CC BY", "text_file": name})
    (folder / "manifest.json").write_text(json.dumps(entries), "utf-8")
    return entries


def test_import_and_clear_deletes_files(tmp_path):
    folder = tmp_path / "s"
    entries = stage(folder, 3)
    entries.append({**entries[0], "text_file": "files/bad.txt", "url": "http://not-https"})
    (folder / "files/bad.txt").write_text(TEXT + " bad", "utf-8")
    result = staging.import_and_clear(folder, entries, str(tmp_path / "c.db"))
    assert result == {"imported": 3, "failed": 1, "stopped": None}
    assert list((folder / "files").iterdir()) == []                     # indexed and rejected files both removed
    assert len(corpus.inventory(corpus.connect(str(tmp_path / "c.db")))) == 3


def test_stops_and_keeps_files_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_MAX_SOURCES", "1")
    folder = tmp_path / "s"
    entries = stage(folder, 3)
    result = staging.import_and_clear(folder, entries, str(tmp_path / "c.db"))
    assert result["imported"] == 1 and "CORPUS_MAX_SOURCES" in result["stopped"]
    assert len(staging.leftover(folder)) == 2                           # kept for a later run


def test_purge_leftovers_frees_space_and_forgets_state(tmp_path):
    folder = tmp_path / "staging" / "cyberleninka"
    entries = stage(folder, 2)
    (folder / "state.json").write_text(json.dumps({"done": {"https://x/1": entries[0]["text_file"], "https://x/2": "skipped"}}))
    freed = staging.purge_leftovers(tmp_path / "staging")
    assert freed > 0 and staging.leftover(folder) == []
    assert json.loads((folder / "manifest.json").read_text()) == []
    assert json.loads((folder / "state.json").read_text())["done"] == {"https://x/2": "skipped"}   # re-fetched later


def test_low_disk_threshold(monkeypatch, tmp_path):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(staging.shutil, "disk_usage", lambda p: Usage(433 * 2**20, 433 * 2**20, 0))
    assert "Paused: only 0 MB free of 433 MB" in staging.low_disk(str(tmp_path / "c.db"))
    monkeypatch.setattr(staging.shutil, "disk_usage", lambda p: Usage(5 * 2**30, 2**30, 4 * 2**30))
    assert staging.low_disk(str(tmp_path / "c.db")) is None


def test_engine_starts_on_a_full_disk(tmp_path, monkeypatch):
    """The live engine stopped answering on a full volume; startup must purge leftovers and never crash."""
    from collections import namedtuple
    from fastapi.testclient import TestClient
    import engine_service
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("ENGINE_API_KEY", "k" * 32)
    folder = tmp_path / "staging" / "cyberleninka"
    stage(folder, 2)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(staging.shutil, "disk_usage", lambda p: Usage(433 * 2**20, 433 * 2**20, 0))
    real_connect = engine_service.connect
    monkeypatch.setattr(engine_service, "connect", lambda *a: (_ for _ in ()).throw(
        __import__("sqlite3").OperationalError("database or disk is full")))
    with TestClient(engine_service.app) as client:                    # startup runs inside the context
        assert client.get("/health").json() == {"status": "ok"}
    assert staging.leftover(folder) == []                             # leftovers removed at startup
    monkeypatch.setattr(engine_service, "connect", real_connect)
