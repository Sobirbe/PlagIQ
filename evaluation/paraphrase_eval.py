"""Same-language paraphrase: can LaBSE (crosslang.py's model) find the source of a chatbot-paraphrased Uzbek passage
without flagging unrelated human text? Measurement only; nothing here is used by the website or the engine.

PlagIQ's word matching finds none of the chatbot paraphrases in evaluation/ai_uz (prompts-edit-uz.txt, blocks F4-F6),
even against their own source. This tests the engine's cross-language rule applied within Uzbek: a library text is a
source when at least MIN_SENTENCES sentences of the checked passage each match one of its sentences with LaBSE cosine
similarity >= cut-off.

Library: the 33 whole works the edited passages came from (human_full_uz.json) and 300 other whole articles from the
same OAK journals (oak_full_uz.json), as same-field distractors.
Must be found: the 107 chatbot edits (polish, paraphrase, expand, shorten, correct), each against its own source work.
Must not be flagged: 500 passages of other OAK articles, not in the library (same journals and fields).

    python3 paraphrase_eval.py <labse folder>       (writes PARAPHRASE.json next to this file)
"""
import json, random, sys, time
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE / "ai_uz"))
import crosslang                                                  # noqa: E402
from edit_study import pairs                                      # noqa: E402

CUTOFFS = (0.75, 0.80, 0.85, 0.90)
MIN_SENTENCES = 2


def sents(text):
    return [text[s:e] for s, e in crosslang.sentences(text)]


def main(folder):
    enc = crosslang.Encoder(folder)
    rnd = random.Random(17)
    works = {w["id"]: w for w in json.load(open(HERE / "ai_uz" / "human_full_uz.json"))}
    edits = list(pairs())
    sources = sorted({x["id"] for *_, x in edits})
    oak = json.load(open(HERE / "ai_uz" / "oak_full_uz.json"))
    rnd.shuffle(oak)
    distract, held = oak[:300], oak[300:]
    library = [(f"src:{i}", " ".join(works[i]["chunks"])) for i in sources] + \
              [(f"oak:{a['oai_id']}", " ".join(a["chunks"])) for a in distract]
    negatives = [p for a in held for p in a["chunks"]]
    rnd.shuffle(negatives); negatives = negatives[:500]

    t0 = time.time()
    lib_sents, owner = [], []
    for k, (name, text) in enumerate(library):
        s = sents(text); lib_sents += s; owner += [k] * len(s)
    cache = Path(folder).parent / "paraphrase_library.npy"
    L = np.load(cache) if cache.is_file() else enc.encode(lib_sents)
    np.save(cache, L); owner = np.array(owner)
    print(f"library: {len(library)} texts, {len(lib_sents)} sentences encoded in {time.time() - t0:.0f}s", flush=True)

    def matches(text):
        """{library index: best similarities of each query sentence to that text}, as a (texts x sentences) max."""
        q = sents(text)
        if not q: return None, 0
        sim = enc.encode(q) @ L.T                                    # (query sentences, library sentences)
        best = np.full((len(library), len(q)), -1.0)
        np.maximum.at(best, owner, sim.T)                            # best per library text and query sentence
        return best, len(q)

    found = {c: defaultdict(lambda: [0, 0]) for c in CUTOFFS}
    wrong = {c: 0 for c in CUTOFFS}
    for kind, bot, original, edited, x in edits:
        best, n = matches(edited)
        own = library.index(next(l for l in library if l[0] == f"src:{x['id']}"))
        for c in CUTOFFS:
            hits = (best >= c).sum(axis=1) >= MIN_SENTENCES if best is not None else np.zeros(len(library), bool)
            found[c][kind][0] += bool(hits[own]); found[c][kind][1] += 1
            wrong[c] += int(hits.sum() - hits[own])
    print(f"edits done in {time.time() - t0:.0f}s", flush=True)
    false = {c: 0 for c in CUTOFFS}; nsent = 0; examples = []
    for p in negatives:
        best, n = matches(p); nsent += n
        if best is None: continue
        for c in CUTOFFS:
            false[c] += bool(((best >= c).sum(axis=1) >= MIN_SENTENCES).any())
        hit = np.where((best >= 0.80).sum(axis=1) >= MIN_SENTENCES)[0]
        if len(hit):                                  # what an unrelated passage matched, for reading (0.80)
            q = sents(p); qv = enc.encode(q); k = int(hit[0]); rows = np.where(owner == k)[0]
            sim = qv @ L[rows].T
            pairs_ = [(float(sim[i].max()), q[i], lib_sents[rows[int(sim[i].argmax())]]) for i in range(len(q)) if sim[i].max() >= 0.80]
            examples.append({"library": library[k][0], "pairs": pairs_})
    out = {"library_texts": len(library), "library_sentences": len(lib_sents), "negatives": len(negatives),
           "negative_sentences": nsent, "min_sentences": MIN_SENTENCES, "results": {}}
    for c in CUTOFFS:
        out["results"][str(c)] = {"found": {k: v for k, v in found[c].items()},
                                  "other_library_texts_flagged_by_edits": wrong[c],
                                  "unrelated_passages_flagged": false[c]}
        cells = "  ".join(f"{k} {v[0]}/{v[1]}" for k, v in found[c].items())
        print(f"cut-off {c:.2f}: {cells}  | wrong sources {wrong[c]} | unrelated passages flagged {false[c]}/{len(negatives)}")
    json.dump(out, open(HERE / "PARAPHRASE.json", "w"), indent=1)
    json.dump(examples, open(Path(folder).parent / "paraphrase_false.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
