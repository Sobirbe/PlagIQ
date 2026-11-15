"""More chatbot phrases (Uzbek Latin and Cyrillic, Russian, English) and English references without a DOI that
OpenAlex cannot find. Synthetic texts only; a local fake OpenAlex answers the title searches."""
import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

import integrity
import uzcyr
from test_integrity import HUMAN_EN

UZ_HUMAN = ("Ushbu ishda Namangan viloyatidagi kichik korxonalarning moliyaviy holati tahlil qilindi. Tadqiqot natijalari "
            "shuni ko‘rsatadiki, kredit olish imkoniyati korxonalar rivojiga sezilarli ta’sir qiladi. Albatta, bu borada "
            "qo‘shimcha tadqiqotlar zarur. Savollar bo‘yicha xulosalar uchinchi bobda keltirilgan. ") * 4


def phrases(text):
    return integrity.local_signals(text)["chatbot_phrases"][0]


def test_uzbek_assistant_phrases_in_both_scripts_and_any_apostrophe():
    for phrase in ("Albatta, quyida", "Agar qo‘shimcha savollaringiz bo‘lsa", "agar qo'shimcha savollaringiz bo'lsa",
                   "Agar qoshimcha savollaringiz bolsa", "Sizga yana qanday yordam bera olaman", "Men sun’iy intellektman"):
        assert phrases(UZ_HUMAN + " " + phrase + " ...") == 1, phrase
        if "qoshimcha" not in phrase:                       # typed without apostrophes: a Latin-only habit
            assert phrases(uzcyr.to_cyrillic(UZ_HUMAN + " " + phrase + " ...")) == 1, phrase
    for phrase in ("Если у вас есть дополнительные вопросы", "Чем ещё могу вам помочь", "Отличный вопрос",
                   "If you have any further questions", "Great question"):
        assert phrases(HUMAN_EN + " " + phrase + ".") == 1, phrase


def test_ordinary_academic_uzbek_and_russian_text_is_not_flagged():
    assert phrases(UZ_HUMAN) == 0 and phrases(uzcyr.to_cyrillic(UZ_HUMAN)) == 0     # "Albatta, bu borada" is human prose
    ru = ("В работе рассмотрены вопросы кредитования малого бизнеса. Конечно, результаты требуют проверки на более "
          "широкой выборке. Ответы на вопросы анкеты приведены в приложении. ") * 4
    assert phrases(ru) == 0


@pytest.fixture()
def openalex(monkeypatch):
    known = ["Remittance flows and household investment in rural Central Asia"]
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            words = set(query["filter"][0].split(":", 1)[1].split())
            seen.append(sorted(words))
            results = [{"display_name": t} for t in known if words <= set(t.lower().split())]
            body = json.dumps({"results": results}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("INTEGRITY_OPENALEX_SEARCH", f"http://127.0.0.1:{server.server_port}/works")
    monkeypatch.delenv("INTEGRITY_DOI_CHECK", raising=False)
    monkeypatch.delenv("INTEGRITY_TITLE_CHECK", raising=False)
    yield seen
    server.shutdown()


REFS = ("\nReferences\n"
        "1. Smith, J. A., & Brown, K. (2021). Remittance flows and household investment in rural Central Asia. "
        "Journal of Development Economics, 45(2), 112-130.\n"
        "2. Karimova D. Oilaviy byudjet va migratsiya. Iqtisodiyot va ta’lim, 2020, 3-son, 10-15 b.\n"
        "3. Иванов И.И. Миграция и доходы // Вопросы экономики. 2018. № 4. С. 20-31.\n"
        "4. Johnson, P. 2020. Water governance reforms and canal maintenance outcomes in the delta. World Development "
        "130: 104-118.\n"
        "5. Rahimov A. Kichik biznesni kreditlash masalalari. Toshkent: Fan, 2019. 120 b.\n"
        "6. Brown, T. (2022). The role of village microfinance in seasonal poverty reduction. International Journal "
        "of Economics, 8(1), 1-15.\n")


def test_english_references_not_found_are_listed_others_left_alone(openalex):
    summary, _ = integrity.assess(HUMAN_EN * 3 + REFS, check_dois=True)
    refs = summary["references"]
    assert refs["titles"] == 3 and refs["titles_checked"] == 3                     # only the English journal ones
    assert len(refs["titles_not_found"]) == 2 and all("Remittance" not in e for e in refs["titles_not_found"])
    assert {"id": "reference_not_found", "count": 2} in summary["signals"] and summary["status"] == "review"
    assert len(openalex) == 3                                                     # titles only, not the manuscript


def test_single_missing_reference_is_not_reported(openalex):
    one = REFS.split("6. Brown")[0]
    summary, _ = integrity.assess(HUMAN_EN * 3 + one, check_dois=True)
    assert summary["references"]["titles_not_found"] == [] and not any(
        s["id"] == "reference_not_found" for s in summary["signals"])


def test_title_check_needs_consent_and_can_be_switched_off(openalex, monkeypatch):
    assert integrity.assess(HUMAN_EN * 3 + REFS)[0]["references"] is None and openalex == []
    monkeypatch.setenv("INTEGRITY_TITLE_CHECK", "0")
    refs = integrity.assess(HUMAN_EN * 3 + REFS, check_dois=True)[0]["references"]
    assert refs["titles"] == 0 and openalex == []


def test_unreachable_openalex_reports_nothing(monkeypatch):
    monkeypatch.setenv("INTEGRITY_OPENALEX_SEARCH", "http://127.0.0.1:9/works")
    summary, _ = integrity.assess(HUMAN_EN * 3 + REFS, check_dois=True)
    assert summary["references"]["titles_not_found"] == [] and summary["references"]["titles_checked"] == 0
