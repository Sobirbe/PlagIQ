"""Fixes from the November 2026 external audit: engine queueing, per-account hourly allowance,
scan-slot admission before any await, and error texts that match the configured limits."""
import threading
import time

from fastapi.testclient import TestClient

import app as web
import engine_service
from test_archive import engine  # noqa: F401  (fixture: engine with a temporary corpus)
from test_auth_ai import EN, login, sign_in  # noqa: F401  (fixture)


def test_engine_waits_for_its_turn(engine, monkeypatch):
    monkeypatch.setenv("ENGINE_QUEUE_SECONDS", "5")
    engine_service.lock.acquire()
    threading.Timer(0.5, engine_service.lock.release).start()        # another search finishes after 0.5 s
    started = time.monotonic()
    r = engine.post("/v1/checks", json={"query_text": EN, "language": "en"})
    assert r.status_code == 202 and time.monotonic() - started >= 0.4


def test_engine_busy_after_queue_time(engine, monkeypatch):
    monkeypatch.setenv("ENGINE_QUEUE_SECONDS", "0.2")
    engine_service.lock.acquire()
    try:
        assert engine.post("/v1/checks", json={"query_text": EN, "language": "en"}).status_code == 429
    finally:
        engine_service.lock.release()


def test_signed_in_users_have_their_own_allowance(login, monkeypatch):
    monkeypatch.setenv("CHECKS_PER_HOUR", "1")
    monkeypatch.setenv("CHECKS_PER_HOUR_ACCOUNT", "2")
    form = {"mode": "reference", "language": "en", "reference_text": EN}
    run = lambda client: client.post("/api/check", data=form, files={"submission": ("m.txt", EN.encode())})
    anonymous = TestClient(web.app)
    assert run(anonymous).status_code == 200
    assert run(anonymous).json()["code"] == "rate_limited"           # same address, no account
    sign_in(login, user_id=42)
    assert run(login).status_code == 200 and run(login).status_code == 200    # per account, not per address
    assert run(login).json()["code"] == "rate_limited"
    other = TestClient(web.app)
    sign_in(other, user_id=43)
    assert run(other).status_code == 200                              # a classmate on the same network


def test_slot_reserved_before_reading(login, monkeypatch):
    monkeypatch.setenv("SCAN_CONCURRENCY", "1")
    seen = []

    async def slow_analyse(*args, **kwargs):
        seen.append(web.active_scans)
        raise web.ApiError(400, "too_short", "x")

    monkeypatch.setattr(web, "analyse", slow_analyse)
    r = login.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": EN},
                   files={"submission": ("m.txt", EN.encode())})
    assert r.status_code == 400 and seen == [1] and web.active_scans == 0       # released after failure


def test_limit_messages_match_settings():
    client = TestClient(web.app)
    big = client.post("/api/quote", files={"submission": ("a.txt", b"x" * (web.MAX_BYTES + 1))}).json()
    assert big["code"] == "file_too_large" and "20 MB" in big["detail"]


def test_admin_shows_proxy_setting(monkeypatch):
    import admin
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    row = next(r for r in admin.checklist(None) if r[0].startswith("Visitors told apart"))
    assert row[1] is False and "share one allowance" in row[2]
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    assert next(r for r in admin.checklist(None) if r[0].startswith("Visitors told apart"))[1] is True
