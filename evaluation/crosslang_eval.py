"""Measure cross-language retrieval (translated copying) before anything reaches users.

Question: if a manuscript in Uzbek contains a translation of a Russian or English source that is in
PlagIQ's library, does a multilingual sentence-embedding model put that source first, and how often
does it wrongly flag Uzbek text that is NOT a translation of anything in the library?

Data (none of it is stored in the repository):
  * FLORES-101 dev+devtest (CC BY-SA 4.0): the same 2,009 sentences professionally translated into
    Uzbek (Latin), Russian and English, grouped into short passages by source article. Download the
    Parquet files uzb|rus|eng-dev|devtest.parquet from huggingface.co/datasets/gsarti/flores_101.
  * Real academic abstracts from OpenAlex (Uzbek and Russian, language confirmed by PlagIQ's detector),
    as hard negatives and distractors: {"uz": [{"id", "title", "text"}], "ru": [...]}.

Design:
  * Articles are split at random (seed 7) into IN (their Russian/English passage is in the library) and OUT.
  * Library: Russian (or English) passages of IN articles + Russian academic abstracts (distractors).
  * Positives: Uzbek passages of IN articles; correct only if the first result is their own source.
  * Mixed positives: half of an IN translation followed by half of an OUT passage (partial reuse).
  * Negatives: Uzbek passages of OUT articles (same kind of text, source absent) and real Uzbek
    academic abstracts (not translations). Any flag on a negative is a false alarm.
  * The threshold is chosen on one half of the negatives (at most 1% false alarms) and the false-alarm
    rate is reported on the other half, so the figure is not tuned on the data it is measured on.

Usage:
  python evaluation/crosslang_eval.py --flores DIR --abstracts abstracts.json --out evaluation/CROSSLANG.json \
      --model sentence-transformers/LaBSE --model intfloat/multilingual-e5-small
"""
import argparse
import json
import os
import random
import resource
import time
from pathlib import Path


def load_flores(folder):
    import pandas as pd
    out = {}
    for code, lang in (("uzb", "uz"), ("rus", "ru"), ("eng", "en")):
        frames = [pd.read_parquet(Path(folder) / f"{code}-{split}.parquet").assign(split=split) for split in ("dev", "devtest")]
        out[lang] = pd.concat(frames)
    passages = {}
    for (split, url), group in out["uz"].groupby(["split", "URL"]):
        ids = sorted(group["id"])
        key = f"{split}:{url}"
        passages[key] = {}
        for lang, frame in out.items():
            rows = frame[(frame["split"] == split) & (frame["id"].isin(ids))].sort_values("id")
            passages[key][lang] = list(rows["sentence"])
    return {k: v for k, v in passages.items() if len(v["uz"]) >= 2}


def embed(model, name, texts, batch=32):
    prefix = "query: " if "e5" in name else ""                    # e5 models expect a prefix
    return model.encode([prefix + t for t in texts], batch_size=batch, normalize_embeddings=True, show_progress_bar=False)


SENT = __import__("re").compile(r"[^.!?]+[.!?]?")


def sentences(text):
    parts = [p.strip() for p in SENT.findall(text) if len(p.split()) >= 4]
    return parts or [text]


def aligned_sentences(model, name, queries, library, candidates, cutoff):
    """Sentence-level verification: for each query, how many of its sentences have a counterpart in the
    candidate source with similarity >= cutoff. A translation aligns sentence by sentence; a different
    text on the same topic rarely does."""
    import numpy as np
    out = []
    for query, candidate in zip(queries, candidates):
        q, src = sentences(query), sentences(library[candidate])
        qv, sv = embed(model, name, q), embed(model, name, src)
        out.append(int(((qv @ sv.T).max(1) >= cutoff).sum()))
    return np.array(out)


def evaluate(name, flores, abstracts, target="ru", seed=7):
    import numpy as np
    from sentence_transformers import SentenceTransformer
    started = time.monotonic()
    model = SentenceTransformer(name, device="cpu")
    load_seconds = time.monotonic() - started
    rng = random.Random(seed)
    keys = sorted(flores)
    rng.shuffle(keys)
    inside, outside = keys[: len(keys) // 2], keys[len(keys) // 2:]
    library = [" ".join(flores[k][target]) for k in inside] + ([a["text"] for a in abstracts.get("ru", [])] if target == "ru" else [])
    positives = [" ".join(flores[k]["uz"]) for k in inside]
    mixed = []
    for i, k in enumerate(inside):
        own, other = flores[k]["uz"], flores[outside[i % len(outside)]]["uz"]
        mixed.append(" ".join(own[: max(1, len(own) // 2)] + other[len(other) // 2:]))
    news_negatives = [" ".join(flores[k]["uz"]) for k in outside]
    academic_negatives = [a["text"] for a in abstracts.get("uz", [])]

    started = time.monotonic()
    lib = embed(model, name, library)
    pos, mix = embed(model, name, positives), embed(model, name, mixed)
    neg_news, neg_acad = embed(model, name, news_negatives), embed(model, name, academic_negatives)
    texts = len(library) + len(positives) + len(mixed) + len(news_negatives) + len(academic_negatives)
    seconds = time.monotonic() - started

    def ranked(queries):
        """Top result, its score, and the rank of the query's own source (library row i for query i)."""
        scores = queries @ lib.T
        idx = np.arange(len(queries))
        own = scores[idx, idx]
        return scores.argmax(1), scores.max(1), (scores > own[:, None]).sum(1) + 1

    top_pos, best_pos, rank_pos = ranked(pos)
    top_mix, best_mix, rank_mix = ranked(mix)
    neg_scores = {"news": (neg_news @ lib.T).max(1), "academic": (neg_acad @ lib.T).max(1) if len(neg_acad) else np.array([])}
    # Threshold on one half of the negatives, false alarms measured on the other half.
    tune = np.concatenate([neg_scores["news"][0::2], neg_scores["academic"][0::2]])
    threshold = float(np.quantile(tune, 0.99)) if len(tune) else 0.5
    correct = lambda top, best: float(np.mean((top == np.arange(len(top))) & (best >= threshold)))
    result = {
        "model": name, "target": target, "library": len(library), "positives": len(positives),
        "negatives_news": len(news_negatives), "negatives_academic": len(academic_negatives),
        "recall_at_1": float(np.mean(rank_pos == 1)), "recall_at_5": float(np.mean(rank_pos <= 5)),
        "mixed_recall_at_1": float(np.mean(rank_mix == 1)),
        "threshold": round(threshold, 3),
        "flagged_correctly": correct(top_pos, best_pos), "mixed_flagged_correctly": correct(top_mix, best_mix),
        "false_alarms_news": float(np.mean(neg_scores["news"][1::2] >= threshold)),
        "false_alarms_academic": float(np.mean(neg_scores["academic"][1::2] >= threshold)) if len(neg_acad) else None,
        "median_score_positive": float(np.median(best_pos)), "median_score_academic_negative":
            float(np.median(neg_scores["academic"])) if len(neg_acad) else None,
        "texts_per_second_cpu": round(texts / seconds, 1), "cpu_threads": os.cpu_count(),
        "load_seconds": round(load_seconds, 1), "dimensions": int(lib.shape[1]),
        "peak_memory_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
    }
    # Threshold sweep on all negatives, and sentence-level verification (at least 2 aligned sentences).
    sweep = []
    neg_all = {"news": neg_scores["news"], "academic": neg_scores["academic"]}
    aligned = {"positive": aligned_sentences(model, name, positives, library, top_pos, 0.80),
               "mixed": aligned_sentences(model, name, mixed, library, top_mix, 0.80),
               "news": aligned_sentences(model, name, news_negatives, library, (neg_news @ lib.T).argmax(1), 0.80),
               "academic": aligned_sentences(model, name, academic_negatives, library, (neg_acad @ lib.T).argmax(1), 0.80)
               if len(neg_acad) else np.array([], dtype=int)}
    for cut in (0.70, 0.75, 0.80, 0.85, 0.88):
        row = {"passage_cutoff": cut}
        for verify in (False, True):
            ok = lambda kind, best: (best >= cut) & ((aligned[kind] >= 2) if verify else True)
            tag = "verified_" if verify else ""
            row[tag + "full"] = float(np.mean(ok("positive", best_pos) & (top_pos == np.arange(len(top_pos)))))
            row[tag + "mixed"] = float(np.mean(ok("mixed", best_mix) & (top_mix == np.arange(len(top_mix)))))
            row[tag + "fa_news"] = float(np.mean(ok("news", neg_all["news"])))
            row[tag + "fa_academic"] = float(np.mean(ok("academic", neg_all["academic"]))) if len(neg_acad) else None
        sweep.append(row)
    result["sweep"] = sweep
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--flores", required=True)
    parser.add_argument("--abstracts", required=True)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--target", action="append", default=None, help="ru and/or en (default both)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    flores = load_flores(args.flores)
    abstracts = json.loads(Path(args.abstracts).read_text("utf-8"))
    results = json.loads(Path(args.out).read_text("utf-8")) if Path(args.out).exists() else []
    for name in args.model:
        for target in args.target or ["ru", "en"]:
            result = evaluate(name, flores, abstracts, target)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            results = [r for r in results if (r["model"], r["target"]) != (name, target)] + [result]
            Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
