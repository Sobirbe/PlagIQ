"""Reproducible ground-truth evaluation of PlagIQ's exact matcher.

Every case is generated from a fixed seed: a passage of known length is copied
from a synthetic source into a synthetic manuscript, optionally transformed,
and the matcher's output is scored against the known location.

What this measures: correctness of normalization, seven-word matching, passage
alignment and the index path, plus the method's *designed* blind spots (edited
or reordered copying). What it cannot measure: behaviour on real academic
prose (formulaic phrases, citations), for which licensed real-world test
collections are required. Synthetic vocabulary is owner-created; no
publications are used.

Run:  python evaluation/run_eval.py [--trials 10] [--out evaluation/RESULTS.md]
"""
import argparse
import json
import platform
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import corpus  # noqa: E402
from corpus import compare_texts, connect, ingest, lexical, search  # noqa: E402
from extract import dehyphenate  # noqa: E402
from textfold import fold  # noqa: E402

# Owner-created parallel Uzbek vocabulary (Latin, Cyrillic).
UZ_PAIRS = [
    ("ilmiy", "илмий"), ("tadqiqot", "тадқиқот"), ("natija", "натижа"), ("ta'lim", "таълим"),
    ("o'quvchi", "ўқувчи"), ("g'oya", "ғоя"), ("maqola", "мақола"), ("jamiyat", "жамият"),
    ("rivojlanish", "ривожланиш"), ("iqtisodiyot", "иқтисодиёт"), ("muhim", "муҳим"), ("masala", "масала"),
    ("tahlil", "таҳлил"), ("usul", "усул"), ("asosiy", "асосий"), ("davlat", "давлат"), ("til", "тил"),
    ("adabiyot", "адабиёт"), ("tarix", "тарих"), ("madaniyat", "маданият"), ("olim", "олим"), ("ish", "иш"),
    ("yangi", "янги"), ("yondashuv", "ёндашув"), ("sohada", "соҳада"), ("bilan", "билан"), ("va", "ва"),
    ("uchun", "учун"), ("hamda", "ҳамда"), ("ko'p", "кўп"), ("yil", "йил"), ("qishloq", "қишлоқ"),
    ("xo'jalik", "хўжалик"), ("sanoat", "саноат"), ("ekologik", "экологик"), ("muammo", "муаммо"),
    ("yechim", "ечим"), ("mintaqa", "минтақа"), ("o'zbek", "ўзбек"), ("respublika", "республика"),
    ("universitet", "университет"), ("talaba", "талаба"), ("o'qituvchi", "ўқитувчи"), ("sifat", "сифат"),
    ("baholash", "баҳолаш"), ("tizim", "тизим"),
]
RU = ("исследование результаты анализ образование развитие экономика общество метод данные система "
      "качество оценка университет студент преподаватель проблема решение значение процесс условия "
      "учёный объём отчёт её всё научный современный важный основной новый региональный сельское "
      "хозяйство промышленность экологический республика языка литература история культура работы "
      "в и на для с по также").split()
EN = ("research results analysis education development economy society method data system quality "
      "assessment university student teacher problem solution value process conditions scientist "
      "report scientific modern important main new regional agriculture industry ecological republic "
      "language literature history culture the of and in for with on also").split()

LANGS = {"uz-Latn": ("uz", [p[0] for p in UZ_PAIRS]), "uz-Cyrl": ("uz", [p[1] for p in UZ_PAIRS]),
         "ru": ("ru", RU), "en": ("en", EN)}
LAT2CYR = {a: b for a, b in UZ_PAIRS}
CYR2LAT = {b: a for a, b in UZ_PAIRS}
LENGTHS = (5, 7, 12, 30, 80)
WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|')*")


def render(rng, vocab, n):
    """Sentences of 8-15 words with capitals and full stops."""
    words, out = [rng.choice(vocab) for _ in range(n)], []
    i = 0
    while i < n:
        k = rng.randint(8, 15)
        chunk = words[i:i + k]
        chunk[0] = chunk[0][:1].upper() + chunk[0][1:]
        out.append(" ".join(chunk) + ".")
        i += k
    return " ".join(out)


def word_spans(text):
    return [(m.start(), m.end()) for m in WORD.finditer(text)]


def recase(src, dst):
    return dst[:1].upper() + dst[1:] if src[:1].isupper() else dst


def swap_script(text, script):
    table = LAT2CYR if script == "uz-Latn" else CYR2LAT
    return WORD.sub(lambda m: recase(m.group(), table.get(m.group().lower(), m.group())), text)


def every_nth(text, n, fn):
    count = [0]

    def sub(m):
        count[0] += 1
        return fn(m.group()) if count[0] % n == 0 else m.group()
    return WORD.sub(sub, text)


def transforms(lang):
    t = {
        "exact copy": lambda s: s,
        "case + punctuation": lambda s: re.sub(r"[.]", "", s).upper().replace(" ", ", "),
        "Unicode NFD": lambda s: unicodedata.normalize("NFD", s),
        "invisible chars": lambda s: WORD.sub(lambda m: m.group()[:2] + "­" + m.group()[2:] if len(m.group()) > 4 else m.group(), s).replace(" ", " ​", 3),
        "PDF line hyphenation": "pdf",
        "light edit (1 in 10 words)": lambda s: every_nth(s, 10, lambda w: "zzq"),
        "heavy edit (1 in 4 words)": lambda s: every_nth(s, 4, lambda w: "zzq"),
        "word order changed": lambda s: re.sub(r"(\S+) (\S+)", r"\2 \1", s),
    }
    if lang.startswith("uz"):
        t["Latin↔Cyrillic"] = lambda s: swap_script(s, lang)
    if lang == "uz-Latn":
        t["apostrophe variants"] = lambda s: s.replace("o'", "oʻ").replace("g'", "gʻ").replace("'", "’")
    if lang == "ru":
        t["ё written as е"] = lambda s: s.replace("ё", "е").replace("Ё", "Е")
    return t


EXPECTED_FULL = {"exact copy", "case + punctuation", "Unicode NFD", "invisible chars", "PDF line hyphenation",
                 "Latin↔Cyrillic", "apostrophe variants", "ё written as е"}


def make_case(rng, lang, length, transform):
    code, vocab = LANGS[lang]
    source = render(rng, vocab, 300)
    spans = word_spans(source)
    k = rng.randrange(0, len(spans) - length)
    ss, se = spans[k][0], spans[k + length - 1][1]
    planted = source[ss:se]
    before, after = render(rng, vocab, 150), render(rng, vocab, 150)
    if transform == "pdf":
        hyph = WORD.sub(lambda m: m.group()[:3] + "-\n" + m.group()[3:] if len(m.group()) > 6 and m.group()[3].islower() else m.group(), planted)
        roundtrip = dehyphenate(hyph)
        planted_text = roundtrip
        ok = roundtrip == planted
    else:
        planted_text, ok = transform(planted), True
    # Marker words outside the vocabulary stop a chance match of the neighbouring
    # word from extending the copied run beyond the recorded ground truth.
    before, after = before + " Qxv.", "Qxw " + after
    manuscript = before + " " + planted_text + " " + after
    ps, pe = len(before) + 1, len(before) + 1 + len(planted_text)
    return code, source, manuscript, (ps, pe), (ss, se), ok


def score(code, manuscript, source, planted, src_span, evidence, covered):
    q = lexical(manuscript, code)
    truth = {i for i, (_, s, e) in enumerate(q) if s >= planted[0] and e <= planted[1]}
    tp = len(covered & truth)
    passages = (evidence or {}).get("passages", [])
    aligned = all(src_span[0] <= p["source_start"] and p["source_end"] <= src_span[1] for p in passages)
    exact_bounds = (len(passages) == 1 and passages[0]["query_start"] == planted[0] and
                    passages[0]["query_end"] == planted[1] and passages[0]["source_start"] == src_span[0] and
                    passages[0]["source_end"] == src_span[1])
    return {"truth": len(truth), "tp": tp, "fp": len(covered - truth), "aligned": aligned,
            "exact_bounds": exact_bounds, "detected": bool(covered & truth)}


def evaluate(trials, seed):
    rng = random.Random(seed)
    rows, parity = [], {"cases": 0, "mismatches": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for lang in LANGS:
            for name, fn in transforms(lang).items():
                for length in LENGTHS:
                    agg = {"truth": 0, "tp": 0, "fp": 0, "aligned": 0, "exact_bounds": 0, "detected": 0, "n": 0, "bad_input": 0}
                    for trial in range(trials):
                        code, source, manuscript, planted, src_span, ok = make_case(rng, lang, length, fn)
                        agg["bad_input"] += not ok
                        _, (evidence,), covered, _ = compare_texts(manuscript, [source], code)
                        s = score(code, manuscript, source, planted, src_span, evidence, covered)
                        for key in ("truth", "tp", "fp"):
                            agg[key] += s[key]
                        for key in ("aligned", "exact_bounds", "detected"):
                            agg[key] += s[key]
                        agg["n"] += 1
                        if name in EXPECTED_FULL and trial == 0:
                            # Same case through the private-corpus index must give identical spans.
                            db = connect(Path(tmp) / f"{lang}-{name}-{length}.db".replace(" ", "_").replace("↔", "-"))
                            ingest(db, dict(title="Synthetic", url="https://example.org/s", language=code, authors="Eval",
                                            year=2026, rights_basis="written_permission", permission_reference="synthetic"), source)
                            indexed = search(db, manuscript, code)["unique_passages"]
                            db.close()
                            parity["cases"] += 1
                            parity["mismatches"] += indexed != ((evidence or {}).get("intervals") or [])
                    rows.append({"language": lang, "transform": name, "length": length, **agg})
        # False positives: independent manuscript/source pairs.
        fp = {"pairs": 0, "pairs_with_match": 0, "words_flagged": 0, "words_total": 0}
        for lang, (code, vocab) in LANGS.items():
            for _ in range(trials * 10):
                manuscript, source = render(rng, vocab, 600), render(rng, vocab, 600)
                q, _, covered, _ = compare_texts(manuscript, [source], code)
                fp["pairs"] += 1
                fp["pairs_with_match"] += bool(covered)
                fp["words_flagged"] += len(covered)
                fp["words_total"] += len(q)
    return rows, parity, fp


def fold_check():
    return [(a, b) for a, b in UZ_PAIRS if fold(a) != fold(b)]


def report(rows, parity, fp, trials, seed, seconds):
    try:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = "unknown"
    out = ["# Matcher evaluation (synthetic ground truth)", "",
           "Generated by `python evaluation/run_eval.py`. Do not edit by hand.", "",
           f"- Commit `{commit}` · Python {platform.python_version()} · seed {seed} · {trials} trials per cell · "
           f"normalization v{corpus.NORMALIZATION_VERSION} · {seconds:.0f} s",
           "- **Scope:** synthetic texts from an owner-created vocabulary. This verifies the algorithm, "
           "normalization and index path; it is **not** an accuracy estimate for real manuscripts and must not be "
           "compared with any commercial service.", "",
           "## Recall of copied words, by passage length", "",
           "Recall = copied manuscript words reported as matched ÷ all words in the copied passage. "
           "Passages under 7 words are below the method's threshold by design.", ""]
    langs = list(LANGS)
    out.append("| Language | Change applied to the copied passage | " + " | ".join(f"{n} words" for n in LENGTHS) + " |")
    out.append("|---|---|" + "---|" * len(LENGTHS))
    for lang in langs:
        for name in transforms(lang):
            cells = []
            for n in LENGTHS:
                r = next(r for r in rows if r["language"] == lang and r["transform"] == name and r["length"] == n)
                cells.append(f"{r['tp'] / r['truth']:.2f}" if r["truth"] else "–")
            out.append(f"| {lang} | {name} | " + " | ".join(cells) + " |")
    eligible = [r for r in rows if r["length"] >= 7]
    tp, fpw = sum(r["tp"] for r in rows), sum(r["fp"] for r in rows)
    full = [r for r in eligible if r["transform"] in EXPECTED_FULL]
    out += ["", "## Precision and alignment", "",
            f"- Words reported as matched that were **not** in the copied passage: {fpw} of {tp + fpw} "
            f"(word precision {tp / (tp + fpw):.4f}).",
            f"- Cases (≥7 words, changes the method is designed to handle) where every reported passage pointed "
            f"inside the true source location: {sum(r['aligned'] for r in full)} / {sum(r['n'] for r in full)}.",
            f"- Of those, cases with character-exact passage boundaries in both texts: "
            f"{sum(r['exact_bounds'] for r in full if r['transform'] == 'exact copy')} / "
            f"{sum(r['n'] for r in full if r['transform'] == 'exact copy')} (exact copies only; other changes "
            "alter the manuscript's characters, so boundaries are checked on the source side above).",
            f"- Index path vs in-memory path: {parity['mismatches']} differing results in {parity['cases']} cases.",
            f"- Generated inputs that failed their own self-check: {sum(r['bad_input'] for r in rows)}.",
            "", "## Unrelated documents (false positives)", "",
            f"- {fp['pairs']} independent 600-word pairs; pairs with any reported match: {fp['pairs_with_match']}; "
            f"words flagged: {fp['words_flagged']} of {fp['words_total']}.",
            "- Random synthetic text rarely shares seven-word sequences, so this mainly checks for spurious "
            "matches caused by normalization or hashing. Real academic prose contains formulaic phrases; its "
            "false-positive rate must be measured on licensed real texts before any accuracy claim.", "",
            "## Uzbek Latin/Cyrillic vocabulary folding check", "",
            f"- Parallel word pairs that do **not** fold to the same form: {fold_check() or 'none'} "
            f"(of {len(UZ_PAIRS)}).", "",
            "## Reading these results", "",
            "- Changes the method is designed for (exact, case/punctuation, Unicode form, invisible characters, "
            "PDF hyphenation, Uzbek script, ё/е, apostrophes) should give recall 1.00 from 7 words.",
            "- Edited or reordered copying is **expected** to be partly or wholly missed: an exact seven-word "
            "matcher only finds unchanged runs of seven or more words. These rows document that limitation; "
            "PlagIQ does not claim paraphrase or translation detection."]
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261110)
    parser.add_argument("--out", type=Path, default=ROOT / "evaluation" / "RESULTS.md")
    args = parser.parse_args()
    start = time.time()
    rows, parity, fp = evaluate(args.trials, args.seed)
    text = report(rows, parity, fp, args.trials, args.seed, time.time() - start)
    args.out.write_text(text, encoding="utf-8")
    args.out.with_suffix(".json").write_text(json.dumps({"rows": rows, "parity": parity, "false_positives": fp},
                                                        ensure_ascii=False, indent=1), encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
