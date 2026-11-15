"""When general web pages were not searched, the result, the certificate and the PDF say so, with the reason."""
import pymupdf
from fastapi.testclient import TestClient

import app as web
import checkstatus
from test_auth_ai import EN

DISCOVERED = dict(sources=[{"title": "W", "url": "https://doi.org/10.1/w", "year": 2024, "coverage": "abstract",
                            "abstract": EN}], status="searched", message="", queries=1, query_texts=["q"])


def academic(client, **extra):
    return client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true", **extra},
                       files={"submission": ("m.txt", EN.encode())}).json()


def test_reason_recorded_and_on_certificate(monkeypatch):
    monkeypatch.setattr(web, "discover", lambda text, *a: DISCOVERED)
    monkeypatch.setenv("FULLTEXT_FETCH", "0")
    for k in ("BRAVE_API_KEY", "YANDEX_API_KEY", "YANDEX_FOLDER_ID"):
        monkeypatch.delenv(k, raising=False)
    web.RATE.clear(); web.results.clear()
    client = TestClient(web.app)
    data = academic(client)
    assert data["web"] == {"enabled": False, "reason": "not_set_up"} and checkstatus.web_note(data) == "not_set_up"
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?lang=en")
    text = " ".join("".join(p.get_text() for p in pymupdf.open(stream=pdf.content)).split())
    assert "General web pages Not searched: web search is not set up on this server" in text
    for lang, phrase in (("uz", "bu serverda internet qidiruvi sozlanmagan"), ("uzc", "бу серверда интернет қидируви созланмаган"),
                         ("ru", "поиск в интернете на этом сервере не настроен")):
        pdf = client.get(f"/api/report/{data['report_id']}.pdf?lang={lang}&full=1")
        assert phrase in " ".join("".join(p.get_text() for p in pymupdf.open(stream=pdf.content)).split()), lang
    monkeypatch.setenv("BRAVE_API_KEY", "test-key-not-real")
    web.RATE.clear()
    assert academic(client)["web"]["reason"] == "not_chosen"                      # set up, but not ticked


def test_which_check_types_get_the_note():
    assert checkstatus.web_note({"mode": "reference"}) is None                      # one supplied document
    assert checkstatus.web_note({"mode": "ai"}) is None
    assert checkstatus.web_note({"mode": "corpus"}) == "not_in_mode"
    assert checkstatus.web_note({"mode": "academic", "web": {"enabled": True}}) is None
    assert checkstatus.web_note({"mode": "academic", "web": {"enabled": False}}) == "not_searched"   # older reports
    assert checkstatus.web_note({"mode": "academic", "sample": True}) is None
