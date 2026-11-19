"""Owner alerts (alerts.py): checks failing in a row, the engine not answering, volumes almost full; each said once
per ALERT_REPEAT_HOURS and once more when it is fixed; Telegram to the owners, e-mail to ALERT_EMAIL otherwise;
codes and counts only."""
import time

import pytest
from fastapi.testclient import TestClient

import alerts
import app as web
import auth
import mailer
import metrics
from test_auth_ai import TOKEN, sign_in


@pytest.fixture()
def sent(monkeypatch, tmp_path):
    out = []
    alerts.reset()
    monkeypatch.setattr(alerts, "_start", out.append)
    monkeypatch.setenv("METRICS_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("OWNER_ALERTS", raising=False)
    yield out
    alerts.reset()


def test_three_failures_in_a_row_then_fixed(sent):
    alerts.check_outcome("academic", "failed_search", "engine_unavailable")
    alerts.check_outcome("academic", "failed_extraction", "unreadable_file")   # the user's file: not counted
    alerts.check_outcome("academic", "completed_matches")                      # a success resets the count
    for _ in range(2):
        alerts.check_outcome("corpus", "failed_search", "engine_timeout")
    assert sent == []
    alerts.check_outcome("corpus", "failed_internal", "internal_error")
    assert len(sent) == 1 and "the last 3 checks failed" in sent[0] and "internal_error (corpus)" in sent[0]
    alerts.check_outcome("corpus", "failed_internal", "internal_error")      # still failing: not repeated yet
    assert len(sent) == 1
    alerts.check_outcome("corpus", "completed_no_matches")
    assert sent[-1] == "✅ PlagIQ: checks are finishing normally again."
    alerts.check_outcome("corpus", "completed_no_matches")
    assert len(sent) == 2                                                     # said once


def test_repeated_after_the_repeat_time(sent):
    now = time.time()
    assert alerts.raise_alert("engine", "x", now)
    assert not alerts.raise_alert("engine", "x", now + 3600)
    assert alerts.raise_alert("engine", "x", now + 7 * 3600)


def test_engine_down_twice_and_volumes(sent):
    alerts.engine_result("connection refused")
    assert sent == []                                                         # one miss can be a restart
    alerts.engine_result("connection refused")
    assert "search engine (library) is not answering: connection refused" in sent[-1]
    alerts.engine_result("")
    assert sent[-1].startswith("✅") and "answers again" in sent[-1]
    alerts.disk("Engine", 300 * 2**20, 4 * 2**30)
    assert "Engine volume is almost full: 300 MB free of 4,096 MB" in sent[-1]
    alerts.disk("Engine", 2 * 2**30, 4 * 2**30)
    assert "has room again" in sent[-1]
    alerts.disk("Website", None, None)                                        # unknown: nothing


def test_metrics_feed_the_alerts_and_switch_off(sent, monkeypatch):
    for _ in range(3):
        metrics.record_failure("academic", "failed_search", "engine_unavailable", 1.0)
    assert len(sent) == 1
    metrics.record_check({"mode": "academic", "status": "completed_matches"}, 2.0)
    assert len(sent) == 2
    alerts.reset()
    monkeypatch.setenv("OWNER_ALERTS", "0")
    for _ in range(3):
        metrics.record_failure("academic", "failed_search", "engine_unavailable", 1.0)
    assert len(sent) == 2


def test_delivery_telegram_then_email(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "telegram_enabled", lambda: True)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42, 77")
    import telegram_bot
    monkeypatch.setattr(telegram_bot, "_call", lambda m, p, o=None: calls.append(p["chat_id"]) or {"ok": True})
    assert alerts._deliver("hello") and calls == [42, 77]
    monkeypatch.setattr(telegram_bot, "_call", lambda m, p, o=None: (_ for _ in ()).throw(OSError("403")))
    monkeypatch.setenv("MAIL_BACKEND", "dev")
    monkeypatch.setenv("ALERT_EMAIL", "owner@example.uz")
    mailer.OUTBOX.clear()
    assert alerts._deliver("hello") and mailer.OUTBOX[-1]["to"] == "owner@example.uz"
    monkeypatch.delenv("ALERT_EMAIL")
    assert not alerts._deliver("hello")


def test_admin_shows_the_route_and_sends_a_test(monkeypatch, tmp_path):
    monkeypatch.setenv("USERS_DB", str(tmp_path / "u.sqlite3"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    delivered = []
    monkeypatch.setattr(alerts, "_deliver", lambda text, opener=None: delivered.append(text) or True)
    client = TestClient(web.app)
    sign_in(client, user_id=42)
    page = client.get("/admin").text
    assert "Owner alerts" in page and "Telegram to 1 owner account(s)" in page
    r = client.post("/admin/test-alert", headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and "Test alert sent." in r.text and delivered
    assert TestClient(web.app).post("/admin/test-alert").status_code in (401, 403, 404)


def test_five_minute_round(sent, monkeypatch):
    import asyncio
    import engine_client
    monkeypatch.setattr(engine_client, "configured", lambda: True)
    async def down():
        return "no reply within 10 s"
    monkeypatch.setattr(engine_client, "probe", down)
    monkeypatch.setattr(metrics, "storage", lambda: {"free": 50 * 2**20, "total": 2**30})
    asyncio.run(alerts.watch_round())
    assert any("Website volume is almost full" in t for t in sent) and not any("not answering" in t for t in sent)
    asyncio.run(alerts.watch_round())
    assert any("not answering: no reply within 10 s" in t for t in sent)


def test_library_full_and_room_again(sent, monkeypatch):
    import asyncio
    import engine_client
    note = "Library full: 5,000 of 5,000 texts (CORPUS_MAX_SOURCES). Nothing is downloaded until the limit is raised."
    lib = {"disk": {"free_bytes": 2 * 2**30, "total_bytes": 4 * 2**30}, "imports": {"limit": {"error": note}}}
    monkeypatch.setattr(engine_client, "configured", lambda: True)
    async def ok():
        return ""
    async def library():
        return lib
    monkeypatch.setattr(engine_client, "probe", ok)
    monkeypatch.setattr(engine_client, "library", library)
    monkeypatch.setattr(metrics, "storage", lambda: {"free": None, "total": None})
    asyncio.run(alerts.watch_round())
    assert sent == ["⚠️ PlagIQ: the library is full, so no new texts are imported. " + note]
    lib["imports"]["limit"]["error"] = None
    asyncio.run(alerts.watch_round())
    assert sent[-1] == "✅ PlagIQ: the library has room again; imports continue."
