"""The real-text evaluation script runs end to end and scores what it should (small generated library)."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "evaluation"))
import corpus  # noqa: E402
import real_eval  # noqa: E402

WORDS = ("tadqiqot natija iqtisodiyot jamiyat rivojlanish tahlil usul davlat maktab oila suv yer bozor "
         "korxona daromad xizmat sifat tizim loyiha muammo yechim mahalla viloyat shahar qishloq").split()


def test_runs_and_finds_unchanged_copies(tmp_path):
    rng = random.Random(1)
    db = corpus.connect(str(tmp_path / "c.db"))
    for i in range(12):
        text = ". ".join(" ".join(rng.choice(WORDS) for _ in range(12)) for _ in range(40)) + "."
        corpus.ingest(db, {"title": f"T{i}", "authors": "A", "year": 2024, "language": "uz", "url": f"https://j.uz/{i}",
                           "rights_basis": "open_license", "permission_reference": "CC BY"}, text)
    results, cases, whole, size, held = real_eval.run(str(tmp_path / "c.db"), None, 6, 3, holdout_per_language=4)
    assert held == {"uz": 4} and size["uz"] == 8
    exact = results[("uz", "exact")]
    assert len(exact) == 6 and all(r["recall"] > 0.9 and r["right_source"] for r in exact)
    heavy = results[("uz", "heavy_edit")]          # one word in 4 replaced: found as edited copying, nothing else marked
    assert all(r["recall"] > 0.9 and r["false"] == 0 for r in heavy)
    assert real_eval.uz_cyrillic_to_latin(real_eval.uz_latin_to_cyrillic("O‘zbekiston maktab")) == "O‘zbekiston maktab"
    text = real_eval.markdown(results, cases, whole, size, held, 3, 1.0)
    assert "| uz | copied unchanged | 6 |" in text


def test_template_passages_are_not_used_as_copies(tmp_path):
    """A passage repeated in many library texts (a journal's licence note) is ignored by PlagIQ on purpose; the test
    used to count it as a missed copy. It is now replaced by another passage and reported."""
    rng = random.Random(4)
    db = corpus.connect(str(tmp_path / "c.db"))
    template = ("Ushbu maqola Creative Commons Attribution 4.0 xalqaro litsenziyasi asosida nashr etilgan va "
                "mualliflik huquqi mualliflarda qoladi hamda jurnal tahririyati unga javob bermaydi. ") * 3
    for i in range(30):
        text = ". ".join(" ".join(rng.choice(WORDS) for _ in range(12)) for _ in range(40)) + ". " + template
        corpus.ingest(db, {"title": f"T{i}", "authors": "A", "year": 2024, "language": "uz", "url": f"https://j.uz/{i}",
                           "rights_basis": "open_license", "permission_reference": "CC BY"}, text)
    assert real_eval.template_text(db, template, "uz")
    assert not real_eval.template_text(db, " ".join(rng.choice(WORDS) for _ in range(60)), "uz")
    results, cases, whole, size, held = real_eval.run(str(tmp_path / "c.db"), None, 6, 3, holdout_per_language=4)
    text = real_eval.markdown(results, cases, whole, size, held, 3, 1.0)
    assert "_template" not in text.split("Library texts sampled:")[1].split(".")[0]
