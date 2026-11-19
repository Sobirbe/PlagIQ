"""tools/uz_review.py: a native speaker's corrections go back into the code only with the same placeholders, and are
found whether the source writes the text plainly or escaped."""
from tools import uz_review


def test_placeholders_and_source_forms():
    find = uz_review.PLACEHOLDER.findall
    assert find("{n} so‘z, {{EMAIL}} va __KEEP_UZ__") == ["{n}", "{{EMAIL}}", "__KEEP_UZ__"]
    assert 'Tugmani \\"bosing\\"' in uz_review._literal_forms('Tugmani "bosing"', "index.html")
    assert "A &amp; B" in uz_review._literal_forms("A & B", "privacy.html")
    assert not uz_review._wanted("https://x.uz") and uz_review._wanted("Hujjat")
