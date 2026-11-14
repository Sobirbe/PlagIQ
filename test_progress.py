"""Progress steps come from the code that does the work: only stages that really run are shown, in order."""
import io
import threading
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app as web
import extract
import fulltext
import progress

TEXT = ("Remittance inflows have reshaped rural household budgets across the Fergana valley, and this study examines "
        "how families allocate these funds between consumption, education and small business investment. ") * 3
TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def client():
    web.RATE.clear(); web.results.clear(); progress._runs.clear()
    return TestClient(web.app)


def keys(state):
    return [s["key"] for s in state["steps"]]


def test_reference_check_steps(client):
    response = client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT, "progress": TOKEN},
                           files={"submission": ("m.txt", io.BytesIO(TEXT.encode()))})
    assert response.status_code == 200
    state = client.get(f"/api/progress/{TOKEN}").json()
    assert state["done"] and keys(state) == ["read", "compare", "report"]
    assert all(s["state"] == "done" for s in state["steps"])
    assert "Remittance" not in str(state)


def test_academic_steps_follow_the_work(client, monkeypatch):
    record = {"title": "W", "url": "https://doi.org/10.1/w", "year": 2024, "coverage": "abstract", "abstract": TEXT,
              "access": {"pdf_urls": ["https://e.org/a.pdf"]}}
    monkeypatch.setattr(web, "discover", lambda text, *a: {"status": "searched", "sources": [dict(record)], "queries": 3, "message": ""})
    monkeypatch.setattr(fulltext, "fetch_fulltexts", lambda *a, **k: {"attempted": 1, "compared": 0, "failed": 1, "timed_out": 0, "reasons": {}})
    client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true", "progress": TOKEN},
                files={"submission": ("m.txt", io.BytesIO(TEXT.encode()))})
    state = client.get(f"/api/progress/{TOKEN}").json()
    assert keys(state) == ["read", "discover", "fulltext", "compare", "report"]      # no web, no library: not configured
    assert state["steps"][2]["detail"] == 1


def test_ocr_pages_are_counted(client, monkeypatch):
    if not extract.ocr_available():
        pytest.skip("Tesseract is not installed")
    monkeypatch.setenv("OCR_DPI", "150")
    out = pymupdf.open()
    for _ in range(2):
        src = pymupdf.open(); page = src.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 535, 790), TEXT, fontsize=12)
        img = out.new_page(width=page.rect.width, height=page.rect.height)
        img.insert_image(img.rect, stream=page.get_pixmap(dpi=200).tobytes("png"))
    client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT, "progress": TOKEN},
                files={"submission": ("scan.pdf", io.BytesIO(out.tobytes()))})
    state = client.get(f"/api/progress/{TOKEN}").json()
    assert keys(state)[:2] == ["read", "ocr"] and state["steps"][1]["detail"] == "2/2"


def test_steps_are_visible_while_the_check_runs(client, monkeypatch):
    gate = threading.Event()

    def slow_discover(text, *a):
        gate.wait(5)
        return {"status": "searched", "sources": [], "queries": 3, "message": ""}
    monkeypatch.setattr(web, "discover", slow_discover)
    worker = threading.Thread(target=lambda: client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true",
                                                                             "progress": TOKEN},
                                                        files={"submission": ("m.txt", io.BytesIO(TEXT.encode()))}))
    worker.start()
    for _ in range(50):
        state = progress.read(TOKEN)
        if state and keys(state) == ["read", "discover"]:
            break
        time.sleep(0.05)
    assert not state["done"] and state["steps"][-1]["state"] == "active" and state["steps"][0]["state"] == "done"
    gate.set(); worker.join(10)
    assert progress.read(TOKEN)["done"]


def test_failed_check_and_unknown_tokens(client):
    blank = pymupdf.open(); blank.new_page()
    client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT, "progress": TOKEN},
                files={"submission": ("scan.pdf", io.BytesIO(blank.tobytes()))})
    state = client.get(f"/api/progress/{TOKEN}").json()
    assert state["done"] and state["failed"] and keys(state) == ["read"] and state["steps"][0]["state"] == "failed"
    assert client.get("/api/progress/" + "f" * 32).status_code == 404
    assert client.get("/api/progress/not-a-token").status_code == 404
    client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": TEXT, "progress": "bad"},
                files={"submission": ("m.txt", io.BytesIO(TEXT.encode()))})
    assert list(progress._runs) == [TOKEN]                                           # invalid token: nothing tracked
