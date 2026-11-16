"""PlagIQ's own Uzbek AI-writing indicator (uzai.py): features, passages, the shipped model, who sees it, and that it
stays out of the cache, stored report and PDF."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as web
import uzai
from test_auth_ai import login, pilot_tester, sign_in      # noqa: F401  (fixture)

DATA = Path(__file__).with_name("evaluation") / "ai_uz"


def chatbot_section():
    """Three ChatGPT-written Uzbek article sections (study data, committed), joined: ~950 words."""
    return "\n\n".join(json.load(open(DATA / "full_chatgpt.json"))["C2"])


def human_passages():
    """Real Uzbek passages (CC BY, quoted in the study's prompt file, blocks D)."""
    t = (DATA / "prompts-full-uz.txt").read_text("utf-8")
    out = []
    for block in re.split(r"(?m)^===== BLOCK (D\d) =====\s*$", t)[2::2]:
        lines = block.split("----- Sources")[0].strip().split("\n")[1:]
        out += [re.sub(r"^\d+\.\s+", "", x).strip() for x in lines if x.strip()]
    return out


def test_features_ignore_script_and_apostrophe_typography():
    a, b = uzai.features("O‘zbekiston ta’lim"), uzai.features("o'zbekiston ta'lim")
    assert list(a[0]) == list(b[0]) and list(a[1]) == list(b[1])
    c = uzai.features("Ўзбекистон")                       # Cyrillic Uzbek is read in Latin
    assert set(c[0]) <= set(uzai.features("o'zbekiston")[0]) | set(uzai.features("ozbekiston")[0])
    assert uzai.features("   ")[0].size == 0


def test_passages_are_sentence_aligned_and_long_enough():
    text = chatbot_section()
    spans = uzai.passages(text)
    assert len(spans) >= 3
    for s, e in spans:
        assert uzai.MIN_WORDS <= len(text[s:e].split()) <= 2 * uzai.MAX_WORDS
    assert spans == sorted(spans) and all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    assert uzai.passages("Qisqa matn.") == [(0, 11)]


def test_shipped_model_flags_chatbot_text_and_not_the_human_passages():
    """Smoke test of the shipped model (texts it was trained on: this checks the export, not accuracy)."""
    assert uzai.MODEL.is_file()
    summary, flagged = uzai.assess(chatbot_section())
    assert summary["status"] == "done" and summary["experimental"] and summary["passages"] >= 3
    assert summary["passages_flagged"] >= 1 and all(f["label"] == "ai" for f in flagged)
    humans = [p for p in human_passages() if len(p.split()) >= 150]
    assert len(humans) >= 8
    assert sum(uzai.score(p) > uzai._model()[3] for p in humans) <= 1
    assert uzai.assess("This is an English text about something else entirely. " * 40)[0]["status"] == "no_uzbek_passages"


def test_who_sees_it(monkeypatch):
    owner, tester, other = {"id": 7}, {"id": 8}, {"id": 9}
    pilots = lambda u: u["id"] == 8
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "7")
    monkeypatch.delenv("UZ_AI_DETECT", raising=False)                      # default: preview
    assert uzai.mode() == "preview"
    assert uzai.visible_to(owner, pilots) and uzai.visible_to(tester, pilots)
    assert not uzai.visible_to(other, pilots) and not uzai.visible_to(None, pilots)
    monkeypatch.setenv("UZ_AI_DETECT", "on")
    assert uzai.visible_to(other, pilots) and uzai.visible_to(None, pilots)
    monkeypatch.setenv("UZ_AI_DETECT", "off")
    assert not uzai.visible_to(owner, pilots)
    monkeypatch.setenv("UZ_AI_DETECT", "nonsense")
    assert uzai.mode() == "preview"


def test_pilot_tester_gets_it_on_the_page_and_its_counts_in_the_report(login, monkeypatch):
    monkeypatch.delenv("UZ_AI_DETECT", raising=False)
    pilot_tester(login)
    text = chatbot_section()
    data = login.post("/api/check", data={"mode": "reference", "language": "uz", "reference_text": "Boshqa matn. " * 20},
                      files={"submission": ("m.txt", text.encode())}).json()
    ai = data["ai_uz"]
    assert ai["status"] == "done" and ai["provider"] == "PlagIQ" and ai["passages_flagged"] >= 1
    assert data["ai_uz_html"].count('<mark class="ai-a">') == ai["passages_flagged"] and "<script" not in data["ai_uz_html"]
    cached = web.results[data["report_id"]]["result"]
    # the counts are kept with the report (for the PDF); the highlighted document is never cached or stored
    assert cached["ai_uz"] == ai and "ai_uz_html" not in cached and "ai-a" not in repr(cached)
    import io
    from pypdf import PdfReader
    for kind in ("", "?full=1"):
        pdf = login.get(f"/api/report/{data['report_id']}.pdf{kind}")
        assert pdf.status_code == 200, pdf.text[:200]
        pages = [p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages]
        text = " ".join(" ".join(pages).split())
        assert "PlagIQ, experimental" in text and str(ai["passages_flagged"]) in text
        assert "experimental" not in " ".join(pages[0].split())                # not on the certificate page


def test_others_do_not_get_it_in_preview(login, monkeypatch):
    monkeypatch.delenv("UZ_AI_DETECT", raising=False)
    sign_in(login, 77)
    data = login.post("/api/check", data={"mode": "reference", "language": "uz", "reference_text": "Boshqa matn. " * 20},
                      files={"submission": ("m.txt", "O‘zbek tilidagi oddiy matn bu yerda. ".encode() * 30)}).json()
    assert "ai_uz" not in data and "ai_uz_html" not in data
    assert web.health()["uz_ai_indicator"]["mode"] == "preview"


def test_owner_is_recognised_through_a_linked_telegram_id(login, monkeypatch):
    """An account first opened by e-mail keeps the e-mail account's id after Telegram is linked to it; the owner's
    Telegram id must still make it the owner's account (for /admin and the indicator)."""
    import auth
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "7777")
    with auth._lock, auth._connect() as db:
        db.execute("INSERT INTO account_links (alias, account, created) VALUES (?, ?, ?)", (7777, -5, 0))
    assert auth.is_owner({"id": -5}) and auth.is_owner({"id": 7777})
    assert not auth.is_owner({"id": -6}) and not auth.is_owner(None)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "")
    assert not auth.is_owner({"id": -5})


def test_api_me_says_whether_the_indicator_is_on(login, monkeypatch):
    monkeypatch.delenv("UZ_AI_DETECT", raising=False)
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    sign_in(login, 42)
    assert login.get("/api/me").json()["uz_ai_indicator"] is True
    sign_in(login, 43)
    assert login.get("/api/me").json()["uz_ai_indicator"] is False
