"""Step 2 of the academic cross-language evaluation: score LaBSE sentence-level search on machine-translated
academic abstracts (from crosslang_translate.py), with student-style edits, Uzbek Cyrillic, and a shortlist.

For each direction (source -> manuscript language):
  Library: the original abstracts of the items marked in_library, plus up to 500 other real abstracts in the
  source language (distractors, never translated).
  Must be found (the item's own source must win):
    * mt      - the machine translation as produced;
    * edited  - about 15% of words dropped, two sentences swapped, two sentences merged;
    * half    - the first half of the translation followed by half of an unrelated real abstract.
  Must NOT be flagged:
    * mt_absent - machine translations of abstracts that are NOT in the library (same field, same style);
    * original  - up to 400 real abstracts written in the manuscript language (not translations).
  Uzbek manuscripts are also tested in Cyrillic script (transliterated), with Cyrillic negatives.
Rule: a text is flagged for a library abstract when at least `need` of its sentences each find their best
library sentence in that abstract with cosine similarity >= cutoff.
Shortlist: only the top-K library abstracts by (mean sentence vector) similarity are searched sentence by
sentence; the loss against a full search is reported.

Usage: python evaluation/crosslang_academic.py --abstracts academic.json --translations translations.json --out evaluation/CROSSLANG_ACADEMIC.json
"""
import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from real_eval import uz_latin_to_cyrillic  # noqa: E402

SENT = re.compile(r"[^.!?]+[.!?]?")
MODEL = "sentence-transformers/LaBSE"


def sentences(text):
    parts = [p.strip() for p in SENT.findall(text) if len(p.split()) >= 4]
    return parts or [text.strip()]


def edit(sents, rng):
    """Student-style edits of a translation: drop about 15% of words, swap two sentences, merge two."""
    out = []
    for s in sents:
        words = s.split()
        keep = [w for i, w in enumerate(words) if i in (0, len(words) - 1) or rng.random() > 0.15]
        out.append(" ".join(keep))
    if len(out) >= 3:
        i = rng.randrange(1, len(out) - 1)
        out[i], out[i + 1] = out[i + 1], out[i]
    if len(out) >= 4:
        out[0:2] = [out[0].rstrip(".!?") + ", " + out[1][0].lower() + out[1][1:]]
    return out


def run(abstracts, translations, seed=13, distractors=500, negatives=400):
    import numpy as np
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL, device="cpu")
    enc = lambda texts: model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    rng = random.Random(seed)
    translated = {item["id"] for items in translations.values() for item in items}
    results = []
    for direction, items in translations.items():
        source, target = direction.split("->")
        inside = [i for i in items if i["in_library"]]
        absent = [i for i in items if not i["in_library"]]
        pool = [a for a in abstracts[source] if a["id"] not in translated]
        rng.shuffle(pool)
        library = [" ".join(i["source"]) for i in inside] + [a["text"] for a in pool[:distractors]]
        originals = [a for a in abstracts[target] if a["id"] not in translated]
        rng.shuffle(originals)
        originals = originals[:negatives]
        lib_sents, owner, doc_vecs = [], [], []
        for n, text in enumerate(library):
            ss = sentences(text)
            lib_sents += ss
            owner += [n] * len(ss)
        owner = np.array(owner)
        started = time.monotonic()
        lib_vec = enc(lib_sents)
        for n in range(len(library)):
            v = lib_vec[owner == n].mean(0)
            doc_vecs.append(v / np.linalg.norm(v))
        doc_vecs = np.array(doc_vecs)
        sets = {
            "mt": [(i["translation"], k) for k, i in enumerate(inside)],
            "edited": [(edit(list(i["translation"]), rng), k) for k, i in enumerate(inside)],
            "half": [(i["translation"][: max(1, len(i["translation"]) // 2)]
                      + sentences(originals[k % len(originals)]["text"])[len(i["translation"]) // 2:][:3], k)
                     for k, i in enumerate(inside)],
            "mt_absent": [(i["translation"], None) for i in absent],
            "original": [(sentences(a["text"])[:6], None) for a in originals],
        }
        scripts = ["latin", "cyrillic"] if target == "uz" else ["native"]
        for script in scripts:
            convert = uz_latin_to_cyrillic if script == "cyrillic" else (lambda s: s)
            encoded = {}
            for kind, queries in sets.items():
                flat, spans = [], []
                for sents, _ in queries:
                    spans.append((len(flat), len(flat) + len(sents)))
                    flat += [convert(s) for s in sents]
                vec = enc(flat)
                prepared = []
                for (a, b), (_, k) in zip(spans, queries):
                    qvec = vec[a:b]
                    q = qvec.mean(0)
                    ranking = np.argsort(-(doc_vecs @ (q / np.linalg.norm(q))))
                    prepared.append(((qvec @ lib_vec.T, ranking), k))      # similarities computed once per text
                encoded[kind] = prepared

            def decide(prep, cutoff, need, shortlist=None):
                sims, ranking = prep
                if shortlist:
                    sims = np.where(np.isin(owner, ranking[:shortlist])[None, :], sims, -1.0)
                best, score = owner[sims.argmax(1)], sims.max(1)
                hits = best[score >= cutoff]
                if not len(hits):
                    return None
                values, counts = np.unique(hits, return_counts=True)
                return int(values[counts.argmax()]) if counts.max() >= need else None

            for cutoff in (0.75, 0.80, 0.85):
                for shortlist in (None, 20, 10, 5):
                    row = {"direction": direction, "script": script, "cutoff": cutoff, "need": 2,
                           "shortlist": shortlist, "library": len(library)}
                    for kind, queries in encoded.items():
                        outcomes = [decide(q, cutoff, 2, shortlist) for q, _ in queries]
                        if kind in ("mt", "edited", "half"):
                            row[kind] = float(np.mean([o == k for o, (_, k) in zip(outcomes, queries)]))
                        else:
                            row["fa_" + kind] = float(np.mean([o is not None for o in outcomes]))
                            row["n_" + kind] = len(queries)
                    results.append(row)
        print(f"{direction}: {time.monotonic() - started:.0f} s", flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--abstracts", required=True)
    parser.add_argument("--translations", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--distractors", type=int, default=500, help="extra library abstracts per direction")
    parser.add_argument("--negatives", type=int, default=400, help="real manuscript-language abstracts per direction")
    args = parser.parse_args()
    abstracts = json.loads(Path(args.abstracts).read_text("utf-8"))
    translations = json.loads(Path(args.translations).read_text("utf-8"))
    results = run(abstracts, translations, distractors=args.distractors, negatives=args.negatives)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")
    for r in results:
        if r["cutoff"] == 0.80:
            print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})


if __name__ == "__main__":
    main()
