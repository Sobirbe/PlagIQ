"""Accuracy on REAL texts: how much copying PlagIQ finds, and how often it flags text that was not copied.

Unlike run_eval.py (synthetic vocabulary), this uses a real library and real held-out articles:

  1. Real paragraphs are taken from texts in the library (the sources a student could copy from).
  2. Each paragraph is copied into a "student document" made of real held-out text that is NOT in
     the library, in the ways students actually copy: unchanged, reformatted, switched between
     Uzbek Latin and Cyrillic, with look-alike letters, lightly or heavily edited, reordered.
  3. The document is checked exactly as the engine checks it (corpus.search), and scored:
       recall          share of copied words that PlagIQ marked
       found           share of cases where at least half of the copied words were marked
       right source    the library text the passage came from is among the reported sources
       false marks     words marked in the held-out (not copied) part, per 1,000 such words
  4. Whole held-out articles are also checked on their own, to show the level of matching on
     genuinely different texts (shared formulas, quotations, or real overlap worth reading).

Run on the engine (Railway shell), against the real library, in one command:
    python evaluation/real_eval.py --db /data/corpus.sqlite3 --out /data/REAL_RESULTS.md
Without --holdout, 25 library texts per language are set aside as the held-out articles and hidden
from every search during the test (nothing is changed in the library). With --holdout FOLDER, real
articles that are not in the library are used instead: a manifest.json in the corpus.py format, or
plain .txt/.pdf/.docx files whose language is detected. All checks are read-only.
"""
import argparse
import json
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import corpus  # noqa: E402
from extract import extract_text  # noqa: E402

TRANSFORMS = ["exact", "reformatted", "script_switch", "lookalike_letters", "light_edit", "heavy_edit", "inserted",
              "deleted", "third_edit", "reordered"]
DESCRIPTION = {
    "exact": "copied unchanged",
    "reformatted": "letter case, punctuation, spacing and line breaks changed",
    "script_switch": "Uzbek only: Latin rewritten in Cyrillic or the reverse",
    "lookalike_letters": "a third of a/e/o/p/c/x/y letters swapped for the other alphabet's look-alikes",
    "light_edit": "one word in 12 replaced",
    "heavy_edit": "one word in 4 replaced",
    "inserted": "a word inserted after every 5th word",
    "deleted": "every 5th word deleted",
    "third_edit": "one word in 3 replaced (beyond edited-copy detection)",
    "reordered": "sentences in a different order",
}
LENGTHS = (40, 80, 150)

# ---------------------------------------------------------------- Uzbek transliteration (approximate)
_APOS = "['ʻʼ‘’`]"
_L2C = [(rf"o{_APOS}", "ў"), (rf"g{_APOS}", "ғ"), ("sh", "ш"), ("ch", "ч"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"),
        ("ye", "е"), ("a", "а"), ("b", "б"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "ҳ"), ("i", "и"),
        ("j", "ж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "қ"), ("r", "р"),
        ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("x", "х"), ("y", "й"), ("z", "з"), (_APOS, "ъ")]
_C2L = {"ш": "sh", "ч": "ch", "ў": "o‘", "ғ": "g‘", "ё": "yo", "ю": "yu", "я": "ya", "э": "e", "ъ": "’", "ҳ": "h",
        "қ": "q", "х": "x", "й": "y", "ц": "ts", "щ": "sh", "ь": "", "ы": "i", "а": "a", "б": "b", "в": "v", "г": "g",
        "д": "d", "е": "e", "ж": "j", "з": "z", "и": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
        "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f"}


def _case(src, out):
    return out[:1].upper() + out[1:] if src[:1].isupper() else out


def uz_latin_to_cyrillic(text):
    def word(m):
        w = m.group().lower()
        if w.startswith("e"):
            w = "э" + w[1:]
        for pattern, repl in _L2C:
            w = re.sub(pattern, repl, w)
        return _case(m.group(), w)
    return re.sub(rf"[A-Za-z]+(?:{_APOS}[A-Za-z]+)*", word, text)


def uz_cyrillic_to_latin(text):
    def word(m):
        return _case(m.group(), "".join(_C2L.get(c, c) for c in m.group().lower()))
    return re.sub(r"[Ѐ-ӿ]+", word, text)


# ---------------------------------------------------------------- transformations
LOOK_L2C = dict(zip("aeopcxy", "аеорсху"))
LOOK_C2L = {v: k for k, v in LOOK_L2C.items()}


def transform(name, passage, language, rng, replacement_words):
    words = passage.split()
    if name == "exact":
        return passage
    if name == "reformatted":
        out = []
        for i, w in enumerate(words):
            w = w.upper() if i % 9 == 0 else w.lower() if i % 5 == 0 else w
            w = re.sub(r"[,;:]", "", w) if i % 3 == 0 else w
            out.append(w + ("\n" if i % 11 == 10 else ""))
        return "  ".join(out)
    if name == "script_switch":
        cyrillic = sum(1 for c in passage if "Ѐ" <= c <= "ӿ")
        return uz_cyrillic_to_latin(passage) if cyrillic > len(passage) / 4 else uz_latin_to_cyrillic(passage)
    if name == "lookalike_letters":
        table = LOOK_C2L if sum(1 for c in passage if "Ѐ" <= c <= "ӿ") > len(passage) / 4 else LOOK_L2C
        return "".join(table[c] if c in table and rng.random() < 0.33 else c for c in passage)
    if name in ("light_edit", "heavy_edit"):
        step = 12 if name == "light_edit" else 4
        return " ".join(rng.choice(replacement_words) if i % step == step - 1 else w for i, w in enumerate(words))
    if name == "inserted":
        return " ".join(w + (" " + rng.choice(replacement_words) if i % 5 == 4 else "") for i, w in enumerate(words))
    if name == "deleted":
        return " ".join(w for i, w in enumerate(words) if i % 5 != 4)
    if name == "third_edit":
        return " ".join(rng.choice(replacement_words) if i % 3 == 2 else w for i, w in enumerate(words))
    if name == "reordered":
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", passage) if s]
        if len(sentences) < 2:
            half = len(words) // 2
            return " ".join(words[half:] + words[:half])
        rng.shuffle(sentences)
        return " ".join(sentences)
    raise ValueError(name)


# ---------------------------------------------------------------- data
def holdout_texts(folder):
    """[(language, title, text)] of real articles that are not in the library."""
    folder = Path(folder)
    out = []
    manifest = folder / "manifest.json"
    if manifest.exists():
        for entry in json.loads(manifest.read_text("utf-8")):
            path = folder / entry["text_file"]
            if path.is_file():
                text = extract_text(path.name, path.read_bytes(), max_pages=500)
                out.append((corpus.detect_language(text) or entry.get("language"), entry.get("title", path.name), text))
    else:
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in (".txt", ".pdf", ".docx"):
                text = extract_text(path.name, path.read_bytes(), max_pages=500)
                out.append((corpus.detect_language(text), path.name, text))
    return [(l, t, x) for l, t, x in out if l in corpus.LANGUAGES and len(x.split()) >= 300]


def words_with_spans(text):
    return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]


def passage_from(text, length, rng, tries=20):
    """A prose passage (at least 80% of its words contain letters): tables of numbers are
    deliberately not matched by PlagIQ, so they are not used as copied passages."""
    spans = words_with_spans(text)
    if len(spans) < length + 40:
        return None
    for _ in range(tries):
        start = rng.randrange(20, len(spans) - length - 20)
        passage = text[spans[start][0]:spans[start + length - 1][1]]
        words = passage.split()
        if sum(any(c.isalpha() for c in w) for w in words) >= 0.8 * len(words):
            return passage
    return None


# ---------------------------------------------------------------- scoring
def score(db, student, passage_span, language, source_id, ignore=frozenset()):
    report = corpus.search(db, student, language, max_passages=50, ignore=ignore)
    q = corpus.lexical(student, language)
    marked = set()
    for start, end in report["unique_passages"]:
        marked.update(i for i, (_, s, e) in enumerate(q) if s >= start and e <= end)
    inside = [i for i, (_, s, e) in enumerate(q) if s >= passage_span[0] and e <= passage_span[1]]
    outside = [i for i in range(len(q)) if i not in set(inside)]
    hit = sum(1 for i in inside if i in marked)
    return {"recall": hit / len(inside) if inside else 0.0,
            "false": sum(1 for i in outside if i in marked), "outside": len(outside),
            "right_source": any(s.get("sha256") == source_id for s in report["sources"])}


def run(db_path, holdout, per_language, seed, holdout_per_language=25):
    rng = random.Random(seed)
    with closing(corpus.connect(db_path)) as db:
        rows = db.execute("SELECT id, metadata, text FROM sources").fetchall()
        library = defaultdict(list)
        for identifier, raw, text in rows:
            meta = json.loads(raw)
            if meta.get("rights_basis") in corpus.DEPOSITS:
                continue                                   # never sample students' unpublished work
            if len(text.split()) >= 300:
                library[meta["language"]].append((identifier, meta, text))
        held, hidden = defaultdict(list), frozenset()
        if holdout:
            for language, title, text in holdout_texts(holdout):
                held[language].append((title, text))
        else:
            nums = dict(db.execute("SELECT id, num FROM sources").fetchall())
            chosen = []
            for language, items in library.items():
                rng.shuffle(items)
                take = min(holdout_per_language, len(items) // 3)
                for identifier, meta, text in items[:take]:
                    held[language].append((meta.get("title", ""), text))
                    chosen.append(nums[identifier])
                library[language] = items[take:]
            hidden = frozenset(chosen)
        results, cases = defaultdict(list), []
        for language in corpus.LANGUAGES:
            if not library[language] or not held[language]:
                continue
            filler_words = [w for _, t in held[language] for w in t.split()][:200000]
            for n in range(per_language):
                identifier, meta, text = rng.choice(library[language])
                length = LENGTHS[n % len(LENGTHS)]
                passage = passage_from(text, length, rng)
                if not passage:
                    continue
                title, other = rng.choice(held[language])
                ow = other.split()
                cut = rng.randrange(0, max(1, len(ow) - 320))
                before, after = " ".join(ow[cut:cut + 150]), " ".join(ow[cut + 150:cut + 300])
                for name in TRANSFORMS:
                    if name == "script_switch" and language != "uz":
                        continue
                    copied = transform(name, passage, language, rng, filler_words)
                    student = before + "\n\n" + copied + "\n\n" + after
                    span = (len(before) + 2, len(before) + 2 + len(copied))
                    r = score(db, student, span, language, identifier, hidden)
                    results[(language, name)].append(r)
                    cases.append({"language": language, "transform": name, "length": length,
                                  "source": meta.get("title", "")[:80], **r})
        whole = []
        for language, items in held.items():
            for title, text in items:
                report = corpus.search(db, text, language, ignore=hidden)
                q = corpus.lexical(text, language)
                matched = corpus.words_in_intervals(q, report["unique_passages"])
                whole.append({"language": language, "title": title[:90], "percent": round(100 * matched / max(1, len(q)), 2),
                              "top": (report["sources"][0]["title"][:80] if report["sources"] else "")})
        size = {lang: len(v) for lang, v in library.items()}
    return results, cases, whole, size, {k: len(v) for k, v in held.items()}


def markdown(results, cases, whole, size, held, seed, seconds):
    lines = ["# PlagIQ accuracy on real texts", "",
             f"Generated by `python evaluation/real_eval.py` (seed {seed}, {seconds:.0f} s). "
             f"Library texts sampled: {', '.join(f'{k} {v}' for k, v in sorted(size.items()))}. "
             f"Held-out real articles (not in the library): {', '.join(f'{k} {v}' for k, v in sorted(held.items()))}.", "",
             "Each case copies a real passage (40, 80 or 150 words) from a library text into real held-out text and checks it "
             "the way the engine does. **Recall** = share of copied words marked; **found** = at least half marked; "
             "**right source** = the original library text is among the reported sources; **false marks** = marked words in "
             "the part that was not copied, per 1,000 words.", "",
             "| Language | How it was copied | Cases | Recall | Found | Right source | False marks /1,000 |",
             "|---|---|---|---|---|---|---|"]
    for (language, name), rs in sorted(results.items(), key=lambda kv: (kv[0][0], TRANSFORMS.index(kv[0][1]))):
        recall = statistics.mean(r["recall"] for r in rs)
        found = sum(r["recall"] >= 0.5 for r in rs) / len(rs)
        right = sum(r["right_source"] for r in rs if r["recall"] > 0) / max(1, sum(r["recall"] > 0 for r in rs))
        false = 1000 * sum(r["false"] for r in rs) / max(1, sum(r["outside"] for r in rs))
        lines.append(f"| {language} | {DESCRIPTION[name]} | {len(rs)} | {recall:.0%} | {found:.0%} | {right:.0%} | {false:.1f} |")
    lines += ["", "## Whole held-out articles (texts that are not in the library)", "",
              "These are real articles PlagIQ has never seen. A non-zero result is not automatically an error: shared "
              "formulas, quotations or real reuse between journals all produce matches; the top source is listed for review.", "",
              "| Language | Articles | Median | 90th percentile | Highest |", "|---|---|---|---|---|"]
    by = defaultdict(list)
    for w in whole:
        by[w["language"]].append(w["percent"])
    for language, values in sorted(by.items()):
        values.sort()
        p90 = values[min(len(values) - 1, int(0.9 * len(values)))]
        lines.append(f"| {language} | {len(values)} | {statistics.median(values):.2f}% | {p90:.2f}% | {values[-1]:.2f}% |")
    flagged = sorted((w for w in whole if w["percent"] >= 1), key=lambda w: -w["percent"])[:10]
    if flagged:
        lines += ["", "Held-out articles with 1% or more matched (for manual review):", ""]
        lines += [f"- {w['language']} · {w['percent']}% · “{w['title']}” → top source “{w['top']}”" for w in flagged]
    lines += ["", "## Reading these results", "",
              "- PlagIQ finds **unchanged runs of seven or more words**. Copying that keeps such runs (unchanged, reformatted, "
              "script-switched, look-alike letters, lightly edited, reordered sentences) should be found; heavy editing that "
              "breaks most runs is expected to be missed. These numbers show how much.",
              "- The cases are built from real texts, but the edits are simulated. Real student copying mixes them.",
              "- Results depend on the library: run this script on the engine after imports to measure the live library."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Accuracy on real texts (read-only).")
    parser.add_argument("--db", required=True)
    parser.add_argument("--holdout", type=Path, help="folder of real articles not in the library (default: set aside library texts)")
    parser.add_argument("--per-language", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    started = time.time()
    results, cases, whole, size, held = run(args.db, args.holdout, args.per_language, args.seed)
    text = markdown(results, cases, whole, size, held, args.seed, time.time() - started)
    if args.out:
        args.out.write_text(text, "utf-8")
    if args.json:
        args.json.write_text(json.dumps({"cases": cases, "whole": whole}, ensure_ascii=False, indent=1), "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
