"""Writing-integrity indicators (not AI detection): local signals, DOI checks, privacy."""
import http.server
import json
import threading

import pytest
from fastapi.testclient import TestClient

import app as web
import integrity

HUMAN_EN = ("This study examines how remittance inflows affect rural household budgets in the Fergana valley. "
            "We combine survey data from 2019 with interviews and compare seasonal consumption patterns. ") * 4
HUMAN_RU = ("В статье рассматривается влияние денежных переводов на бюджеты сельских домохозяйств. "
            "IT-компании и SMS-сообщения упоминаются как каналы перевода; формула NaCl приведена для примера. ") * 4
HUMAN_UZ = ("Ushbu maqolada pul o‘tkazmalarining qishloq xo‘jaligi oilalari byudjetiga ta’siri o‘rganiladi. "
            "Faylasūf atamasi va Rīnū ismi transliteratsiya qilingan. Тадқиқот натижалари келтирилган. ") * 4


def test_genuine_text_has_no_indicators():
    for text in (HUMAN_EN, HUMAN_RU, HUMAN_UZ):
        summary, examples = integrity.assess(text)
        assert summary == {"status": "none", "signals": [], "references": None} and examples == {}


@pytest.mark.parametrize("text, signal", [
    ("Intro. As an AI language model, I cannot browse the internet. " + HUMAN_EN, "chatbot_phrases"),
    ("Certainly! Here is a revised version of your paragraph. " + HUMAN_EN, "chatbot_phrases"),
    ("Конечно! Вот переработанный вариант текста. " + HUMAN_RU, "chatbot_phrases"),
    ("Albatta! Mana qayta ishlangan variant. " + HUMAN_UZ, "chatbot_phrases"),
    ("Сунъий интеллект сифатида айта оламанки. " + HUMAN_UZ, "chatbot_phrases"),
    (HUMAN_EN + " As shown by [Insert citation here], prices rose.", "placeholders"),
    (HUMAN_EN + " Prices rose (Author, Year).", "placeholders"),
    (HUMAN_EN.replace(" ", " ​", 12), "hidden_characters"),
    (HUMAN_EN.replace("a", "а", 6), "mixed_script"),                     # Cyrillic а in English words
    (HUMAN_RU.replace("о", "o", 12), "mixed_script"),                          # Latin o in Russian words
    ("## Introduction\n**Key point** one.\n## Methods\n```\n" + HUMAN_EN, "markdown"),
])
def test_each_indicator(text, signal):
    summary, examples = integrity.assess(text)
    assert summary["status"] == "review" and signal in [s["id"] for s in summary["signals"]]
    assert examples[signal]


def test_thresholds_tolerate_isolated_slips():
    few = HUMAN_EN.replace(" ", " ​", 3).replace("a", "а", 2)          # a copy-paste or keyboard slip
    assert integrity.assess(few)[0]["status"] == "none"


def test_examples_mark_hidden_and_lookalike_letters():
    _, examples = integrity.assess(HUMAN_EN.replace(" ", " ​", 12).replace("Fergana", "Fеrgana"))
    assert "⟦U+200B⟧" in examples["hidden_characters"][0]
    assert integrity._scripts("Fеrgana") == "F⟨е⟩rgana" and integrity._scripts("Дoм") == "Д⟨o⟩м"
    assert not integrity._disguised("Faylasӯuf") and not integrity._disguised("IT")


def test_overlapping_chatbot_phrases_count_once():
    summary, _ = integrity.assess("Certainly! Here is a revised version of the text. " + HUMAN_EN)
    assert summary["signals"] == [{"id": "chatbot_phrases", "count": 1}]


def test_reference_dois_from_bibliography():
    text = (HUMAN_EN + "\nSee also doi 10.1000/body.1 in the text.\n" + HUMAN_EN +
            "\nReferences\n1. LeCun Y. Deep learning. Nature. https://doi.org/10.1038/NATURE14539.\n"
            "2. Smith J. Remittances. doi:10.5555/x.y(2020)12;\n3. Same again 10.1038/nature14539\n")
    dois = [d for d, _ in integrity.reference_dois(text)]
    assert dois == ["10.1038/nature14539", "10.5555/x.y(2020)12"]                # body DOI and duplicate ignored


def test_title_matching():
    entry = "LeCun Y., Bengio Y., Hinton G. Deep learning in neural network research. Nature, 2015. doi:10.1038/x"
    assert integrity.title_matches("Deep learning in neural network research", entry) is True
    assert integrity.title_matches("Coral reef bleaching in the Pacific ocean", entry) is False
    assert integrity.title_matches("Глубокое обучение нейронных сетей и его применение", entry) is None   # other script
    assert integrity.title_matches("Deep", entry) is None


@pytest.fixture()
def registry(monkeypatch):
    works = {"10.1038/nature14539": "Deep learning",
             "10.1000/real.1": "Remittances and rural household budgets in Central Asia",
             "10.1000/other.2": "Coral reef bleaching in the Pacific ocean"}
    handles = {"10.1000/datacite.3": 1}
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            path = self.path.split("?")[0]
            if path.startswith("/oa/"):
                doi = path[4:]
                if doi == "10.1000/slow.4":
                    self.send_response(503); self.end_headers(); return
                status, body = (200, {"display_name": works[doi]}) if doi in works else (404, {})
            else:
                doi = path[3:]
                code = handles.get(doi, 100)
                status, body = (200 if code == 1 else 404), {"responseCode": code}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("INTEGRITY_OPENALEX_URL", base + "/oa/")
    monkeypatch.setenv("INTEGRITY_HANDLE_URL", base + "/h/")
    monkeypatch.delenv("INTEGRITY_DOI_CHECK", raising=False)
    yield seen
    server.shutdown()


BIBLIOGRAPHY = ("\nReferences\n"
                "1. Karimov A. Remittances and rural household budgets in Central Asia. J. Econ. 2021. doi:10.1000/real.1\n"
                "2. Usmonova D. Labour migration and family consumption in Uzbekistan. 2022. doi:10.1000/other.2\n"
                "3. Petrov I. Invented study of seasonal income. 2020. doi:10.1000/fake.9\n"
                "4. Data set of household surveys in the region. 2023. doi:10.1000/datacite.3\n"
                "5. Unavailable record of migration statistics. 2019. doi:10.1000/slow.4\n")


def test_doi_check_finds_missing_and_mismatched(registry):
    summary, _ = integrity.assess(HUMAN_EN + BIBLIOGRAPHY, check_dois=True)
    refs = summary["references"]
    assert refs == {"dois": 5, "checked": 4, "unchecked": 1, "not_found": ["10.1000/fake.9"],
                    "mismatch": [{"doi": "10.1000/other.2", "title": "Coral reef bleaching in the Pacific ocean"}]}
    assert {"id": "doi_not_found", "count": 1} in summary["signals"] and summary["status"] == "review"
    assert not any("/h/10.1000/real.1" in p for p in registry)                     # doi.org only when OpenAlex lacks it


def test_doi_check_off_without_consent_or_by_setting(registry, monkeypatch):
    assert integrity.assess(HUMAN_EN + BIBLIOGRAPHY)[0]["references"] is None and registry == []
    monkeypatch.setenv("INTEGRITY_DOI_CHECK", "0")
    assert integrity.assess(HUMAN_EN + BIBLIOGRAPHY, check_dois=True)[0]["references"] is None and registry == []


def test_check_endpoint_examples_not_cached_or_in_pdf(registry, monkeypatch):
    monkeypatch.setattr(web, "discover", lambda text, *a: dict(sources=[], status="searched", message="", queries=1,
                                                               query_texts=["q"]))
    monkeypatch.setenv("FULLTEXT_FETCH", "0")
    web.RATE.clear(); web.results.clear()
    text = "As an AI language model, I summarise the secret finding XYZZY. " + HUMAN_EN + BIBLIOGRAPHY
    client = TestClient(web.app)
    data = client.post("/api/check", data={"mode": "academic", "language": "en", "consent": "true"},
                       files={"submission": ("m.txt", text.encode())}).json()
    assert data["integrity"]["status"] == "review" and "XYZZY" in data["integrity_examples"]["chatbot_phrases"][0]
    cached = repr(web.results[data["report_id"]])
    assert "XYZZY" not in cached and "integrity_examples" not in cached and "10.1000/fake.9" in cached
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?lang=en")
    import pymupdf
    pdf_text = " ".join("".join(p.get_text() for p in pymupdf.open(stream=pdf.content)).split())
    assert "Review suggested" in pdf_text and "DOI does not exist 10.1000/fake.9" in pdf_text
    assert "Chatbot phrases left in the text 1" in pdf_text and "XYZZY" not in pdf_text
    web.RATE.clear()
    ref = client.post("/api/check", data={"mode": "reference", "language": "en", "reference_text": HUMAN_EN},
                      files={"submission": ("m.txt", text.encode())}).json()
    assert ref["integrity"]["references"] is None and ref["integrity"]["status"] == "review"
