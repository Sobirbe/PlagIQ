"""Uzbek AI-writing indicator (experimental): PlagIQ's own detector for Uzbek, which Pangram does not support.

What it is: a character n-gram classifier trained on Uzbek academic writing published 2010-2021 (before ChatGPT) and on
Uzbek academic text written or continued by ChatGPT, Gemini and Claude (evaluation/AI_UZ.md, evaluation/ai_uz/
train_uzai.py). It scores the document passage by passage (about 120-260 words, sentence-aligned) and marks passages
whose score is above the threshold at which 1 % of held-out human passages were flagged.

What it is not: proof, a probability, or a verdict about a person. In the study it catches most text written from scratch
by a chatbot that is in its training data, only part of the text of a chatbot that is not, and almost none of a human
text that a chatbot polished. Its false-alarm rate on recent student writing has not been measured yet; until it has,
UZ_AI_DETECT defaults to "preview": only site owners and accounts with a pilot allowance see the result.

    UZ_AI_DETECT = off | preview | on       (default preview)

Runs inside the website process with numpy only; the text never leaves the server. The result is kept out of the cache,
the stored report, the PDF and the certificate (app.PRIVATE_FIELDS)."""
import os, re, zlib
from functools import lru_cache
from pathlib import Path
import numpy as np

BUCKETS = 1 << 19                                   # hashed feature space (must match the trained model)
NGRAMS = (2, 3, 4, 5)
MODEL = Path(__file__).with_name("models") / "uz_ai_v1.npz"
MIN_WORDS, MAX_WORDS = 120, 260                     # passage size, as in the study
CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяқғўҳ", ["a","b","v","g","d","e","yo","j","z","i","y","k","l","m","n","o","p","r","s","t","u","f","x","ts","ch","sh","sh","'","i","","e","yu","ya","q","g'","o'","h"]))


def mode():
    m = os.environ.get("UZ_AI_DETECT", "preview").strip().lower()
    return m if m in ("off", "preview", "on") else "preview"


def available():
    return mode() != "off" and MODEL.is_file()


def settings():
    return {"mode": mode(), "model": MODEL.stem if MODEL.is_file() else None}


def visible_to(user, has_pilot=None):
    """Whether this account gets the indicator: everyone when "on"; in "preview" only owners and pilot testers."""
    if not available():
        return False
    if mode() == "on":
        return True
    if not user:
        return False
    import auth
    if auth.is_owner(user):
        return True
    return bool(has_pilot(user)) if has_pilot else False


def clean(t):
    """One script and one apostrophe, formatting quirks removed: the model must not learn typography."""
    t = t.lower()
    for a in "ʻ‘’`ʼ´": t = t.replace(a, "'")
    t = "".join(CYR.get(c, c) for c in t)
    t = re.sub(r"\$[^$]*\$", " ", t)
    t = re.sub(r"[«»“”„―‖\"]", '"', t); t = re.sub(r"\s*([.,;:])\s*", r"\1 ", t); t = re.sub(r"\s+", " ", t)
    return t.strip()


def features(text):
    """Sublinear term frequencies of hashed character n-grams within words (like sklearn's char_wb, 2-5)."""
    counts = {}
    for w in clean(text).split():
        p = " " + w + " "
        for n in NGRAMS:
            if len(p) < n:
                continue
            for i in range(len(p) - n + 1):
                h = zlib.crc32(p[i:i + n].encode("utf-8")) & (BUCKETS - 1)
                counts[h] = counts.get(h, 0) + 1
    if not counts:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    idx = np.fromiter(counts.keys(), dtype=np.int64, count=len(counts))
    tf = 1.0 + np.log(np.fromiter(counts.values(), dtype=float, count=len(counts)))
    return idx, tf


@lru_cache(maxsize=1)
def _model():
    z = np.load(MODEL, allow_pickle=False)
    idf = np.ones(BUCKETS, dtype=np.float32); idf[z["idf_idx"]] = z["idf_val"]
    coef = np.zeros(BUCKETS, dtype=np.float32); coef[z["coef_idx"]] = z["coef_val"]
    return idf, coef, float(z["intercept"]), float(z["threshold"]), str(z["version"])


def score(text):
    """Model score in (0, 1) for one passage; higher reads more like the chatbot text in training."""
    idf, coef, b, _, _ = _model()
    idx, tf = features(text)
    if not len(idx):
        return 0.0
    v = tf * idf[idx]
    n = np.linalg.norm(v)
    z = b + (float(v @ coef[idx]) / n if n else 0.0)
    return float(1 / (1 + np.exp(-z)))


def passages(text):
    """(start, end) character spans of sentence-aligned passages of MIN_WORDS..MAX_WORDS words (shorter tails are
    joined to the previous passage)."""
    spans, start, words = [], None, 0
    for m in re.finditer(r"[^.!?\n]+(?:[.!?]+|\n|$)", text):
        n = len(m.group().split())
        if not n:
            continue
        if start is None:
            start = m.start()
        words += n
        if words >= MIN_WORDS:                     # a single very long sentence stays one passage
            spans.append([start, m.end()]); start, words = None, 0
    if start is not None and words:
        if spans and words < MIN_WORDS:
            spans[-1][1] = len(text.rstrip())
        else:
            spans.append([start, len(text.rstrip())])
    return [(s, e) for s, e in spans]


def assess(text):
    """(summary for the page, flagged passages with start/end for highlighting). Only Uzbek passages are scored."""
    import corpus
    _, _, _, threshold, version = _model()
    scored, flagged = 0, []
    for s, e in passages(text):
        chunk = text[s:e]
        if len(chunk.split()) < MIN_WORDS // 2 or corpus.detect_language(chunk) != "uz":
            continue
        scored += 1
        p = score(chunk)
        if p > threshold:
            flagged.append({"start": s, "end": e, "label": "ai", "score": round(p, 3), "words": len(chunk.split())})
    return {"status": "done" if scored else "no_uzbek_passages", "provider": "PlagIQ", "model": version,
            "experimental": True, "passages": scored, "passages_flagged": len(flagged),
            "words_flagged": sum(f["words"] for f in flagged)}, flagged
