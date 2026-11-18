"""The "ready in about N min" message while a check runs: /api/estimate gives minutes from the recorded times of
recent checks of the same kind (the time 9 in 10 of them took), with defaults until there are enough, and the
page shows it during the check. That a check keeps running when the browser is closed was tried with a real
server and a slow stand-in engine: the connection was cut after 1 s and the check still finished and was listed."""
import time

from fastapi.testclient import TestClient

import app as web
import metrics


def record(mode, seconds, ai=None, error=None, when=None):
    metrics._insert({"time": when or time.time(), "mode": mode, "seconds": seconds, "ai": ai, "error": error})


def test_defaults_until_enough_checks_then_the_slow_end_of_recent_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("METRICS_DB", str(tmp_path / "m.db"))
    assert metrics.estimate("academic") == {"minutes": 5, "checks": 0}
    assert metrics.estimate("academic", ai=True)["minutes"] == 6
    for s in (40, 50, 60, 70, 80, 90, 100, 110, 130, 400):                  # 9 in 10 took up to 130 s
        record("academic", s)
    record("academic", 5000, error="engine_timeout")                       # failed checks are left out
    record("academic", 5000, when=time.time() - 30 * 86400)                # and old ones
    record("corpus", 1000)
    assert metrics.estimate("academic") == {"minutes": 3, "checks": 10}
    assert metrics.estimate("academic", ai=True)["checks"] == 0            # AI runs are counted on their own
    for s in (200, 230, 250, 260, 290):
        record("academic", s, ai="done")
    assert metrics.estimate("academic", ai=True)["minutes"] == 5
    for _ in range(5):
        record("reference", 7200)
    assert metrics.estimate("reference")["minutes"] == 30                  # capped


def test_endpoint_and_page(tmp_path, monkeypatch):
    monkeypatch.setenv("METRICS_DB", str(tmp_path / "m.db"))
    client = TestClient(web.app)
    assert client.get("/api/estimate?mode=corpus").json()["minutes"] == 3
    assert client.get("/api/estimate?mode=nope").status_code == 400
    page = client.get("/").text
    assert 'id="wait-note"' in page and "daqiqada tayyor bo‘ladi" in page and "Sahifani yopishingiz mumkin" in page
