"""Ground-truth evaluation of citation-aware classification (synthetic, seeded).

Each manuscript contains copied passages whose location is known: inside a
quotation followed by a reference marker, inside a quotation without one, in
the bibliography, or in ordinary text. Distractors: a table of contents that
names the bibliography heading, an appendix after the bibliography, Uzbek
apostrophes, several quotation and reference styles per language.

Run: python evaluation/eval_citations.py [--trials 25] [--out evaluation/CITATIONS.md]
"""
import argparse
import platform
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
import citations  # noqa: E402
from corpus import compare_texts, lexical  # noqa: E402
from run_eval import LANGS, render, word_spans  # noqa: E402

QUOTES = {"uz-Latn": [("«", "»"), ("“", "”"), ('"', '"')], "uz-Cyrl": [("«", "»"), ("“", "”")],
          "ru": [("«", "»"), ("„", "“")], "en": [("“", "”"), ('"', '"')]}
REFS = {"uz-Latn": ["[12]", "[3, b. 45]", "(Karimov, 2019)"], "uz-Cyrl": ["[7]", "(Каримов, 2019)"],
        "ru": ["[4]", "[2, с. 15]", "(Иванов, 2018, с. 5)"], "en": ["[9]", "(Smith et al., 2020a)", "[1, p. 7]"]}
HEADINGS = {"uz-Latn": ["Foydalanilgan adabiyotlar ro‘yxati", "Adabiyotlar", "FOYDALANILGAN ADABIYOTLAR"],
            "uz-Cyrl": ["Фойдаланилган адабиётлар рўйхати", "Адабиётлар"],
            "ru": ["Список литературы", "Список использованной литературы", "Литература"],
            "en": ["References", "Bibliography", "REFERENCES"]}
APPENDIX = {"uz-Latn": "Ilova 1", "uz-Cyrl": "Илова 1", "ru": "Приложение А", "en": "Appendix A"}
CATS = ("quotation_cited", "quotation_uncited", "bibliography", "other")


def build(rng, lang):
    code, vocab = LANGS[lang]
    source = render(rng, vocab, 900)
    spans = word_spans(source)
    picks, used = [], set()
    while len(picks) < 5:
        k = rng.randrange(0, len(spans) - 12)
        if not used & set(range(k - 12, k + 24)):
            used |= set(range(k, k + 12))
            picks.append(source[spans[k][0]:spans[k + 11][1]])
    parts, truth = [], []

    def add(text, category=None):
        start = sum(len(p) for p in parts)
        parts.append(text)
        if category:
            truth.append((start, start + len(text), category))

    heading = rng.choice(HEADINGS[lang])
    if rng.random() < 0.5:                                  # table of contents distractor
        add(f"1. Kirish\n2. Asosiy qism\n3. {heading}\n\n")
    add(render(rng, vocab, 120) + " Oʻzbek g‘oya ta’lim. Qxw ")
    open_, close = rng.choice(QUOTES[lang])
    add(f"{render(rng, vocab, 30)} Qxv {open_}")
    add(picks[0], "quotation_cited")
    add(f"{close} {rng.choice(REFS[lang])}. Qxw {render(rng, vocab, 60)} Qxv ")
    open_, close = rng.choice(QUOTES[lang])
    add(f"{open_}")
    add(picks[1], "quotation_uncited")
    add(f"{close} Qxw {render(rng, vocab, 60)} Qxv ")
    add(picks[2], "other")
    add(" Qxw " + render(rng, vocab, 200) + f"\n\n{heading}\n1. Qxv ")
    add(picks[3], "bibliography")
    add(" Qxw. 2019.\n2. " + render(rng, vocab, 15) + "\n")
    if rng.random() < 0.5:                                  # appendix after the bibliography
        add(f"\n{APPENDIX[lang]}\n" + render(rng, vocab, 20) + " Qxv ")
        add(picks[4], "other")
        add(" Qxw.")
    return code, "".join(parts), source, truth


def evaluate(trials, seed):
    rng = random.Random(seed)
    confusion = {lang: {t: dict.fromkeys(CATS, 0) for t in CATS} for lang in LANGS}
    percent_ok = {"none": 0, "quotes": 0, "bibliography": 0, "both": 0, "cases": 0}
    for lang in LANGS:
        for _ in range(trials):
            code, text, source, truth = build(rng, lang)
            q, _, _, intervals = compare_texts(text, [source], code)
            result = citations.assess(text, code, intervals, q=q)
            predicted = {}
            for start, end, where, _ in result["segments"]:
                for i, (_, ws, we) in enumerate(q):
                    if ws >= start and we <= end:
                        predicted[i] = where
            truth_words = {}
            for i, (_, ws, we) in enumerate(q):
                for s, e, cat in truth:
                    if s <= ws and we <= e:
                        truth_words[i] = cat
            for i, cat in truth_words.items():
                confusion[lang][cat][predicted.get(i, "other")] += 1
            # Exclusion arithmetic: counted words must equal truth minus the excluded categories.
            total = len(q)
            by_cat = {c: sum(v == c for v in truth_words.values()) for c in CATS}
            expected = {
                "none": sum(by_cat.values()),
                "quotes": by_cat["bibliography"] + by_cat["other"],
                "bibliography": by_cat["quotation_cited"] + by_cat["quotation_uncited"] + by_cat["other"],
                "both": by_cat["other"]}
            options = {"none": {}, "quotes": {"exclude_quotes": True}, "bibliography": {"exclude_bibliography": True},
                       "both": {"exclude_quotes": True, "exclude_bibliography": True}}
            percent_ok["cases"] += 1
            for name, opts in options.items():
                r = citations.assess(text, code, intervals, q=q, **opts)
                percent_ok[name] += (r["matched_words_counted"] == expected[name] and
                                     r["percent_counted"] == round(100 * expected[name] / total, 2))
    return confusion, percent_ok


def report(confusion, percent_ok, trials, seed):
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    names = {"quotation_cited": "quote + reference", "quotation_uncited": "quote, no reference",
             "bibliography": "bibliography", "other": "other text"}
    out = ["# Citation-aware classification (synthetic ground truth)", "",
           "Generated by `python evaluation/eval_citations.py`. Do not edit by hand.", "",
           f"- Commit `{commit}` · Python {platform.python_version()} · seed {seed} · {trials} manuscripts per language",
           "- **Scope:** generated manuscripts with known passage locations and common distractors. This checks that "
           "the rules behave as documented; it does not show how often real manuscripts use these conventions.", "",
           "## Word-level classification (rows = truth, columns = PlagIQ)", ""]
    for lang, matrix in confusion.items():
        out += [f"### {lang}", "", "| Truth \\ PlagIQ | " + " | ".join(names[c] for c in CATS) + " | Correct |",
                "|---|" + "---|" * (len(CATS) + 1)]
        for t in CATS:
            row = matrix[t]
            n = sum(row.values())
            out.append(f"| {names[t]} | " + " | ".join(str(row[c]) for c in CATS) + f" | {row[t] / n:.3f} |" if n else
                       f"| {names[t]} | " + " | ".join("0" for _ in CATS) + " | – |")
        out.append("")
    n = percent_ok["cases"]
    out += ["## Percentages after exclusions", "",
            f"Cases where the counted words and percentage exactly equal the ground truth: "
            f"no exclusion {percent_ok['none']}/{n}; quotations excluded {percent_ok['quotes']}/{n}; "
            f"bibliography excluded {percent_ok['bibliography']}/{n}; both {percent_ok['both']}/{n}.", "",
            "## Known limits of the rules", "",
            "- A reference placed more than 80 characters after the closing quote, or only in a footnote the "
            "extractor dropped, is reported as *quotation without a nearby reference*.",
            "- Unbalanced or decorative quotation marks can mark the wrong span; quotations over 3,000 characters are ignored.",
            "- A bibliography without a recognised heading is not detected, and text after it is treated as "
            "bibliography until an appendix heading.",
            "- Indented block quotations without quotation marks are not recognised."]
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=25)
    parser.add_argument("--seed", type=int, default=20261111)
    parser.add_argument("--out", type=Path, default=ROOT / "evaluation" / "CITATIONS.md")
    args = parser.parse_args()
    confusion, percent_ok = evaluate(args.trials, args.seed)
    text = report(confusion, percent_ok, args.trials, args.seed)
    args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
