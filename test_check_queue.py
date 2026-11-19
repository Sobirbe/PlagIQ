"""A waiting line instead of "server busy" (app._wait_for_slot): checks wait for a free slot in arrival order and
the page shows their place; refused at once only when the line is full (QUEUE_MAX); stop with server_busy after
QUEUE_WAIT_MINUTES. Tried with a real server and a slow engine as well (see the pull request)."""
import asyncio

import pytest

import app as web
import progress


@pytest.fixture()
def slots(monkeypatch):
    monkeypatch.setenv("SCAN_CONCURRENCY", "1")
    monkeypatch.setattr(web, "active_scans", 1)          # one check running
    monkeypatch.setattr(web, "_waiting", [])
    return web


def test_first_come_first_served_with_places(slots):
    async def scenario():
        a, b = object(), object()
        slots._waiting.extend([a, b])
        order, places = [], {}

        async def wait(t, name, token):
            progress.begin(token)
            task = asyncio.current_task()
            await slots._wait_for_slot(t)
            order.append(name)
        ta = asyncio.create_task(wait(a, "a", "a" * 32))
        tb = asyncio.create_task(wait(b, "b", "b" * 32))
        await asyncio.sleep(0.1)
        places = {k: progress.read(k * 32)["steps"][-1] for k in "ab"}
        assert places["a"]["key"] == "queue" and places["a"]["detail"] == 1 and places["b"]["detail"] == 2
        slots.active_scans -= 1                          # the running check ends
        await asyncio.sleep(0.7)
        assert order == ["a"] and slots.active_scans == 1 and slots._waiting == [b]
        assert progress.read("b" * 32)["steps"][-1]["detail"] == 1      # moved up
        slots.active_scans -= 1
        await asyncio.wait_for(asyncio.gather(ta, tb), 3)
        assert order == ["a", "b"] and slots._waiting == []
    asyncio.run(scenario())


def test_gives_up_after_the_waiting_time(slots, monkeypatch):
    monkeypatch.setattr(web, "queue_wait_seconds", lambda: 0)
    t = object()
    slots._waiting.append(t)
    with pytest.raises(web.ApiError) as err:
        asyncio.run(slots._wait_for_slot(t))
    assert err.value.code == "server_busy"


def test_full_line_is_refused_at_once(slots, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("QUEUE_MAX", "2")
    slots._waiting.extend([object(), object()])
    r = TestClient(web.app).post("/api/check", data={"mode": "reference", "language": "en", "reference_text": "x " * 50},
                                 files={"submission": ("a.txt", b"y " * 50)})
    assert r.status_code == 503 and r.json()["code"] == "server_busy" and len(slots._waiting) == 2


def test_estimate_includes_the_wait(slots, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("METRICS_DB", str(tmp_path / "m.db"))
    client = TestClient(web.app)
    assert client.get("/api/estimate?mode=corpus").json() == {"minutes": 6, "checks": 0, "queue": 1}  # 3 running + 3
    slots._waiting.append(object())
    assert client.get("/api/estimate?mode=corpus").json()["minutes"] == 9
    monkeypatch.setattr(web, "active_scans", 0)
    slots._waiting.clear()
    assert client.get("/api/estimate?mode=corpus").json() == {"minutes": 3, "checks": 0}
