"""Times are shown in Tashkent time (UTC+5); stored and signed times stay UTC."""
from fastapi.testclient import TestClient

import app as web
import timefmt


def test_utc_to_tashkent_in_three_languages(monkeypatch):
    monkeypatch.delenv("REPORT_UTC_OFFSET_HOURS", raising=False)
    assert timefmt.show("2026-11-12T17:34:06+00:00", "uz") == "2026-11-12 22:34 (Toshkent vaqti, UTC+5)"
    assert timefmt.show("2026-11-12T17:34:06+00:00", "ru", seconds=True) == "2026-11-12 22:34:06 (ташкентское время, UTC+5)"
    assert timefmt.show("2026-11-12T21:10:00Z", "en") == "2026-11-13 02:10 (Tashkent time, UTC+5)"   # crosses midnight
    assert timefmt.show("20261112T1734", "en", with_label=False) == "2026-11-12 22:34"                # signed QR format
    from datetime import datetime, timezone
    ms = int(datetime(2026, 11, 12, 17, 44, tzinfo=timezone.utc).timestamp() * 1000)
    assert timefmt.show(ms, "uz", with_label=False) == "2026-11-12 22:44"                              # epoch milliseconds
    assert timefmt.show("2026-11-12") == "2026-11-12" and timefmt.show("") == "" and timefmt.show("soon") == "soon"


def test_offset_can_be_changed(monkeypatch):
    monkeypatch.setenv("REPORT_UTC_OFFSET_HOURS", "0")
    assert timefmt.show("2026-11-12T17:34:06+00:00", "en") == "2026-11-12 17:34 (UTC)"


def test_report_and_verify_page_show_tashkent_time_but_sign_utc(monkeypatch):
    import pymupdf
    import verify
    monkeypatch.setenv("VERIFY_SECRET", "s" * 40)
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://plagiq.example")
    monkeypatch.delenv("REPORT_UTC_OFFSET_HOURS", raising=False)
    web.RATE.clear(); web.results.clear()
    client = TestClient(web.app, base_url="https://plagiq.example")
    text = "Remittance inflows reshape rural household budgets across the Fergana valley every season. " * 3
    data = client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": text},
                       files={"submission": ("m.txt", text.encode())}).json()
    expected = timefmt.show(data["date"], "uz")
    with pymupdf.open(stream=client.get(f"/api/report/{data['report_id']}.pdf?lang=uz").content, filetype="pdf") as doc:
        pdf = " ".join(" ".join(p.get_text() for p in doc).split())
    assert expected in pdf and " UTC " not in pdf.replace("UTC+5", "")
    cached = web.results[data["report_id"]]["result"]
    link = verify.link(cached, data["report_id"], "https://plagiq.example")
    assert "d=" + data["date"][:16].replace("-", "").replace(":", "") in link        # the signature still covers UTC
    page = client.get(link.replace("https://plagiq.example", "") + "&ui=uz").text
    assert timefmt.show(data["date"], "uz") in page
