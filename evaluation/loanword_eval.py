"""Uzbek loanword spellings: is a passage still found when the student spells its Russian loanwords the other
accepted way (sentabr / sentyabr, rejissor / rejissyor, obyekt / ob'yekt, sivilizatsiya / tsivilizatsiya)?

Real 40-word passages containing such words are taken from the Uzbek texts of a library, their loanwords are
re-spelled, the passage is put into real held-out text, and the library is searched (corpus.search). Recall =
share of the passage's words marked. Also reported: the same passages unchanged, as a control.

Usage: python evaluation/loanword_eval.py --db library.db --holdout FOLDER [--cases 60]
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus  # noqa: E402
from real_eval import holdout_texts  # noqa: E402

CONS = "bdfghjklmnpqrstvxzcw"
SOFT = re.compile(rf"(?<=[{CONS}])y(?=[aoue])")
HARD = re.compile(rf"(?<=[{CONS}])['ʻʼ‘’]y")


def respell(word):
    """The other accepted spelling of a loanword, or None for an ordinary word."""
    low = word.lower()
    if HARD.search(low):
        return HARD.sub("y", word)
    if low.startswith("ts") and len(low) > 3:
        return word[1:]
    if low.startswith("s") and low.endswith(("siya", "tsiya")) and len(low) > 6:
        return "t" + word
    if SOFT.search(low):
        return SOFT.sub("", word)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--holdout", required=True)
    parser.add_argument("--cases", type=int, default=60)
    args = parser.parse_args()
    db = corpus.connect(args.db)
    rng = random.Random(7)
    filler = [t for lang, _, t in holdout_texts(args.holdout) if lang == "uz"]
    cases = []
    for (raw, text) in db.execute("SELECT metadata, text FROM sources"):
        if json.loads(raw)["language"] != "uz":
            continue
        words = text.split()
        for i in range(0, max(0, len(words) - 40), 40):
            chunk = words[i:i + 40]
            changed = [respell(w) for w in chunk]
            if sum(1 for c in changed if c) >= 2:
                cases.append((chunk, [c or w for c, w in zip(changed, chunk)]))
    rng.shuffle(cases)
    cases = cases[:args.cases]
    results = {"unchanged": [], "respelled": []}
    for original, respelled in cases:
        other = rng.choice(filler).split()
        cut = rng.randrange(0, max(1, len(other) - 160))
        before, after = " ".join(other[cut:cut + 80]), " ".join(other[cut + 80:cut + 160])
        for name, passage in (("unchanged", original), ("respelled", respelled)):
            copied = " ".join(passage)
            student = before + "\n\n" + copied + "\n\n" + after
            start = len(before) + 2
            report = corpus.search(db, student, "uz")
            q = corpus.lexical(student, "uz")
            inside = [k for k, (_, s, e) in enumerate(q) if s >= start and e <= start + len(copied)]
            marked = {k for k, (_, s, e) in enumerate(q) for a, b in report["unique_passages"] if s >= a and e <= b}
            results[name].append(sum(k in marked for k in inside) / max(1, len(inside)))
    out = {name: {"cases": len(r), "recall": round(sum(r) / max(1, len(r)), 4),
                  "found_half": round(sum(1 for x in r if x >= 0.5) / max(1, len(r)), 4)} for name, r in results.items()}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
