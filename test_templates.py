"""Institution-approved template text: added by the university's admins, chosen by the student at check time,
excluded like a quotation (shown, not counted), never shown to students, kept through source exclusion."""
import io
import json

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app as web
import citations
import corpus
import templates
from test_auth_ai import login, sign_in      # noqa: F401  (fixture)

TEMPLATE = ("O‘ZBEKISTON RESPUBLIKASI OLIY TA’LIM, FAN VA INNOVATSIYALAR VAZIRLIGI TOSHKENT DAVLAT IQTISODIYOT "
            "UNIVERSITETI. Men ushbu bitiruv malakaviy ishini mustaqil bajarganimni va unda boshqa mualliflarning "
            "ishlaridan iqtibos keltirilganda manbalar ko‘rsatilganini tasdiqlayman.")
COPIED = ("Raqamli iqtisodiyot sharoitida tijorat banklarining kredit siyosati mijozlarning to‘lov qobiliyatini "
          "baholashning yangi usullarini talab qiladi va bu borada xorijiy tajriba muhim ahamiyatga ega.")
OWN = "Ushbu ishda men Namangan viloyatidagi kichik korxonalarning moliyaviy holatini o‘zim to‘plagan ma’lumotlar asosida tahlil qildim."


@pytest.fixture
def setup(login, monkeypatch):
    monkeypatch.setenv("INSTITUTIONS", json.dumps([{"name": "TDIU", "agreement": "No. 12/2026", "admins": [42]}]))
    admin = login
    sign_in(admin, user_id=42)
    assert admin.post("/api/institution/templates", data={"institution": "TDIU", "title": "Title page and declaration",
                                                          "text": TEMPLATE}).status_code == 200
    return admin


def check(client, text, institution=""):
    return client.post("/api/check", data={"mode": "reference", "language": "uz", "institution": institution,
                                           "reference_text": TEMPLATE + " " + COPIED},
                       files={"submission": ("bmi.txt", io.BytesIO(text.encode()))}).json()


def test_template_wording_is_excluded_but_copying_is_not(setup):
    student = TestClient(web.app)
    doc = TEMPLATE + " " + OWN + " " + COPIED
    plain = check(student, doc)
    with_template = check(student, doc, "TDIU")
    template_words = len(corpus.lexical(TEMPLATE, "uz"))
    assert plain["percent"] == with_template["percent_all"]                       # same matches found ...
    assert with_template["citation"]["excluded"]["template"] == template_words   # ... template words not counted
    assert with_template["citation"]["exclusions"]["template"] == "TDIU"
    copied_words = len(corpus.lexical(COPIED, "uz"))
    assert with_template["matched_tokens_counted"] == copied_words                # the copied passage still counts
    assert "template" not in plain["citation"]["excluded"]
    pdf = student.get(f"/api/report/{with_template['report_id']}.pdf?full=1&lang=en")
    text = " ".join(p.get_text() for p in pymupdf.open(stream=pdf.content, filetype="pdf")).replace("\n", " ")
    assert f"template text of TDIU ({template_words} words)" in text and "template text of TDIU" in text


def test_cyrillic_template_matches_a_latin_document(setup):
    import evaluation.real_eval as real_eval  # noqa: F401
    from evaluation.real_eval import uz_latin_to_cyrillic
    setup.post("/api/institution/templates", data={"institution": "TDIU", "title": "Kirill", "text": uz_latin_to_cyrillic(OWN)})
    data = check(TestClient(web.app), TEMPLATE + " " + OWN + " " + COPIED, "TDIU")
    assert data["citation"]["excluded"]["template"] >= len(corpus.lexical(TEMPLATE, "uz"))


def test_students_see_titles_only_and_only_admins_manage(setup):
    student = TestClient(web.app)
    listed = student.get("/api/templates").json()
    assert listed == {"institutions": [{"name": "TDIU", "templates": 1}]}
    assert student.get("/api/institution/templates?institution=TDIU").status_code == 403
    assert student.post("/api/institution/templates", data={"institution": "TDIU", "text": TEMPLATE}).status_code == 403
    items = setup.get("/api/institution/templates?institution=TDIU").json()["items"]
    assert [i["title"] for i in items] == ["Title page and declaration"] and "text" not in items[0]
    data = check(student, TEMPLATE + " " + COPIED, "TDIU")
    assert "bajarganimni" not in json.dumps({k: v for k, v in data.items() if k != "highlight_html" and k != "sources"})
    assert student.delete(f"/api/institution/templates/{items[0]['id']}?institution=TDIU").status_code == 403
    assert setup.delete(f"/api/institution/templates/{items[0]['id']}?institution=TDIU").json() == {"deleted": True}
    assert student.get("/api/templates").json() == {"institutions": []}


def test_unknown_university_and_limits(setup):
    response = TestClient(web.app).post("/api/check", data={"mode": "reference", "language": "uz", "institution": "Nowhere",
                                                            "reference_text": COPIED},
                                        files={"submission": ("a.txt", io.BytesIO(COPIED.encode()))})
    assert response.status_code == 400 and response.json()["code"] == "institution_unknown"
    assert setup.post("/api/institution/templates", data={"institution": "TDIU", "text": "too short"}).json()["code"] == "template_too_short"
    for k in range(templates.MAX_TEMPLATES - 1):
        templates.add("TDIU", f"T{k}", TEMPLATE, 42)
    assert setup.post("/api/institution/templates", data={"institution": "TDIU", "text": TEMPLATE}).json()["code"] == "template_limit"


def test_source_exclusion_keeps_the_template_rule(setup):
    student = TestClient(web.app)
    data = check(student, TEMPLATE + " " + OWN + " " + COPIED, "TDIU")
    same = student.post(f"/api/report/{data['report_id']}/exclude", json={"sources": []}).json()
    assert same["citation"]["excluded"]["template"] == data["citation"]["excluded"]["template"]


def test_assess_marks_template_words():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    review = citations.assess(text, "en", [[0, len(text)]], template_spans=[[0, 35]], template_name="X")
    assert review["excluded"]["template"] == 6 and review["matched_words_counted"] == 6   # "alpha" … "zeta"
    assert any(reason == "template" for _, _, _, reason in review["segments"])
