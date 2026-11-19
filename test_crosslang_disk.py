"""Translated / paraphrased-reuse indexing stops before the engine volume fills up (an owner alert reported 398 MB
free of 4,468 MB: imports had paused at 10 %, but the index kept writing into the library file)."""
import admin
import engine_client
import engine_service
import staging

MB = 1024 * 1024


def test_index_pauses_under_5_percent_or_150_mb(monkeypatch):
    def free(n, total=4468 * MB):
        monkeypatch.setattr(staging, "disk", lambda path: (n, total))
    free(398 * MB)
    assert not engine_service.crosslang_low_disk()              # 8.9 % free: imports paused, indexing goes on
    free(200 * MB)
    assert engine_service.crosslang_low_disk()                  # 4.5 %
    free(140 * MB, 2000 * MB)
    assert engine_service.crosslang_low_disk()                  # 7 %, but under 150 MB
    monkeypatch.setenv("CROSSLANG_MIN_FREE_PERCENT", "10")
    free(398 * MB)
    assert engine_service.crosslang_low_disk()
    monkeypatch.setattr(staging, "disk", lambda path: (None, None))
    assert not engine_service.crosslang_low_disk()              # unknown: carry on


def test_admin_says_paused():
    c = engine_client._crosslang({"enabled": True, "state": "paused", "indexed": 1200, "total": 1500})
    assert c["state"] == "paused"
    text = admin.crosslang_detail({"crosslang": c})
    assert text.startswith("paused, engine volume almost full: 1,200 of 1,500")
