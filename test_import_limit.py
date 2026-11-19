"""Imports do not download when the library is at CORPUS_MAX_SOURCES. They used to download a full batch, fail to
add it ("Index limit"), and leave the files on the engine volume until it was almost full (owner alert: 388 MB
free of 4,468 MB); then the leftovers were deleted and the next run downloaded them again."""
import json

import corpus
import engine_service
import import_job
import staging
from test_crosslang import body, meta


def library(tmp_path, monkeypatch, n, limit):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("CORPUS_MAX_SOURCES", str(limit))
    with corpus.connect() as db:
        for i in range(n):
            corpus.ingest(db, meta(i + 1, "uz", title=f"Text {i}"), body("uz", (0,)) + f" Alohida gap {i}.")


def staged_file(tmp_path, size=3 * 1024 * 1024):
    folder = tmp_path / "staging" / "openalex-uz"
    folder.mkdir(parents=True)
    (folder / "w1.txt").write_bytes(b"x" * size)
    (folder / "manifest.json").write_text(json.dumps([{"text_file": "w1.txt", "id": "w1"}]))
    return folder / "w1.txt"


def test_room_and_full_note(tmp_path, monkeypatch):
    library(tmp_path, monkeypatch, 3, 5)
    assert engine_service.library_room() == 2 and engine_service.library_full_note() is None
    waiting = staged_file(tmp_path)
    monkeypatch.setenv("CORPUS_MAX_SOURCES", "3")
    note = engine_service.library_full_note()
    assert note.startswith("Library full: 3 of 3 texts (CORPUS_MAX_SOURCES)") and "3 MB of downloaded files" in note
    assert not waiting.exists()                                  # the files that could not be added are gone


def test_loop_downloads_nothing_when_full_and_caps_the_batch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("AUTO_IMPORT", "uz")
    monkeypatch.setenv("AUTO_IMPORT_BATCH", "500")
    monkeypatch.setattr(staging, "low_disk", lambda *a: None)
    monkeypatch.setattr(import_job, "run", lambda lang, batch, cost: calls.append(batch) or
                        {"imported": 0, "failed": 0, "corpus_sources": 0, "error": None})
    monkeypatch.setattr(import_job, "zenodo_settings", lambda: ([], 1, 0.0))
    monkeypatch.setattr(import_job, "uz_from_ru_settings", lambda: (False, 1, 0.0))
    monkeypatch.setattr(engine_service.wikipedia_import, "settings", lambda: ([], 1, 1))
    monkeypatch.setattr(engine_service.cyberleninka, "settings", lambda: (False, 1, None, None))
    monkeypatch.setattr(engine_service.uz_journals, "settings", lambda: (False, 1))

    def one_round(*a):
        engine_service.import_stop.set()
    monkeypatch.setattr(engine_service.import_stop, "wait", one_round)
    try:
        library(tmp_path, monkeypatch, 3, 3)
        engine_service.import_loop()
        assert calls == []                                       # full: no download at all
        engine_service.import_stop.clear()
        monkeypatch.setenv("CORPUS_MAX_SOURCES", "10")
        engine_service.import_loop()
        assert calls == [7]                                      # room for 7: at most 7 downloaded, not 500
    finally:
        engine_service.import_stop.clear()
