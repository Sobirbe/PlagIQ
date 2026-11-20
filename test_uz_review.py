"""tools/uz_review.py: a native speaker's corrections go back into the code only with the same placeholders, and are
found whether the source writes the text plainly or escaped."""
from tools import uz_review


def test_placeholders_and_source_forms():
    find = uz_review.PLACEHOLDER.findall
    assert find("{n} so‘z, {{EMAIL}} va __KEEP_UZ__") == ["{n}", "{{EMAIL}}", "__KEEP_UZ__"]
    assert 'Tugmani \\"bosing\\"' in uz_review._literal_forms('Tugmani "bosing"', "index.html")
    assert "A &amp; B" in uz_review._literal_forms("A & B", "privacy.html")
    assert not uz_review._wanted("https://x.uz") and uz_review._wanted("Hujjat")


def test_formulas_are_not_written_into_the_code(tmp_path, monkeypatch):
    """The editor left "=E31" (a formula pointing at the row's own text) in rows that needed no change; it must never
    end up as page text."""
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "Matnlar"
    sheet.append(["#", "Where", "File", "Key", "Text", "English", "Correction", "Comment"])
    sheet.append([1, "Website page", "index.html", "L.result", "Tekshiruv natijalari", "Results", "=E2", ""])
    path = tmp_path / "r.xlsx"
    book.save(path)
    before = (uz_review.ROOT / "index.html").read_text("utf-8")
    done, problems = uz_review.apply(path)
    assert done == 0 and "a formula" in problems[0]
    assert (uz_review.ROOT / "index.html").read_text("utf-8") == before


def test_only_whole_texts_are_replaced(tmp_path, monkeypatch):
    """The label "PlagIQ ilmiy manbalar bazasi" was corrected; the same words inside a longer sentence (the
    academic option's description) were replaced too and read "… va Ilmiy ishlar bazasi" mid-sentence."""
    from openpyxl import Workbook
    page = tmp_path / "index.html"
    page.write_text('L={uz:{corpus:"Baza nomi",sub:"Ochiq matnlar va Baza nomi"}}; <p>Baza nomi</p>', "utf-8")
    monkeypatch.setattr(uz_review, "ROOT", tmp_path)
    book = Workbook()
    sheet = book.active
    sheet.title = "Matnlar"
    sheet.append(["#", "Where", "File", "Key", "Text", "English", "Correction", "Comment"])
    sheet.append([1, "Website page", "index.html", "L.corpus", "Baza nomi", "Name", "Yangi nom", ""])
    book.save(tmp_path / "r.xlsx")
    assert uz_review.apply(tmp_path / "r.xlsx") == (1, [])
    assert page.read_text("utf-8") == 'L={uz:{corpus:"Yangi nom",sub:"Ochiq matnlar va Baza nomi"}}; <p>Yangi nom</p>'
