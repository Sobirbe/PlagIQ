"""Full-length Uzbek AI-text detection: passages of human open-access works 2010-2021 (human_full_uz.json, from
fetch_fulltext.py) against full-length chatbot text: Gemini article sections (full_gemini.json, blocks C), the Gemini
articles (gemini1.txt, gemini2_humanized.txt) and the ChatGPT dissertation (chatgpt_dissertation.txt).

Short article/dissertation paragraphs are merged into pieces of at least 120 words, so length does not separate the
classes. Every text belongs to a group (its source work: a human work, the work whose title a section was written for,
one article, the dissertation); 5-fold cross-validation splits by group. Polished passages (blocks D), partly-AI passages
(a human passage + Gemini's continuation, blocks E) and the human originals of D and E are test-only.
Thresholds come from held-out human passages (1 % and 5 % flagged)."""
import json, re, zlib, numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from baseline import clean
from score_articles import paragraphs

Hw = json.load(open("human_full_uz.json")); plan = json.load(open("full_plan.json")); G = json.load(open("full_gemini.json"))
D = {w["id"]: w for w in Hw}
items = []                                                          # (text, label, category, group)
orig = {(x["id"], x["chunk"]): x for k in ("d", "e") for x in plan[k]}
for w in Hw:
    for i, c in enumerate(w["chunks"]):
        items.append((clean(c), 0, "human originals of D/E" if (w["id"], i) in orig else "human", w["id"]))
for x in plan["c"]:
    items.append((clean(G[x["block"]][x["n"] - 1]), 1, "Gemini sections", x["id"]))
for x in plan["d"]:
    items.append((clean(G[x["block"]][x["n"] - 1]), 1, "Gemini polished (D)", x["id"]))
for x in plan["e"]:
    cont = G[x["block"]][x["n"] - 1]; first = D[x["id"]]["chunks"][x["chunk"]]
    items.append((clean(first + " " + cont), 1, "partly AI: human + Gemini continuation (E)", x["id"]))
    items.append((clean(cont), 1, "Gemini continuation alone (E)", x["id"]))

def merged(paras, owner, minw=120):
    out, cur, own = [], [], None
    for p, o in zip(paras, owner):
        if own is not None and o != own and cur: out.append((" ".join(cur), own)); cur = []
        cur.append(p); own = o
        if len(" ".join(cur).split()) >= minw: out.append((" ".join(cur), own)); cur = []
    if cur and out and out[-1][1] == own: out[-1] = (out[-1][0] + " " + " ".join(cur), own)
    elif cur: out.append((" ".join(cur), own))
    return out
for f, cat in (("gemini1.txt", "Gemini articles"), ("gemini2_humanized.txt", "Gemini 'humanized' articles"),
               ("chatgpt_dissertation.txt", "ChatGPT dissertation")):
    ps, ow = paragraphs(f)
    for t, o in merged(ps, list(ow)): items.append((t, 1, cat, f"{f}:{o}"))

X = [t for t, *_ in items]; y = np.array([l for _, l, *_ in items]); cat = np.array([c for _, _, c, _ in items])
fold = np.array([zlib.crc32(g.encode()) % 5 for *_, g in items])
ORDER = ["Gemini sections", "Gemini articles", "Gemini 'humanized' articles", "ChatGPT dissertation",
         "Gemini polished (D)", "partly AI: human + Gemini continuation (E)", "Gemini continuation alone (E)", "human originals of D/E"]
WRITTEN = {"Gemini sections", "Gemini articles", "Gemini 'humanized' articles", "ChatGPT dissertation"}
REGIMES = {"F1 Gemini sections only": {"Gemini sections"},
           "F2 all full-length written": WRITTEN,
           "F3 written, ChatGPT left out": WRITTEN - {"ChatGPT dissertation"},
           "F4 written + polished": WRITTEN | {"Gemini polished (D)"}}

def run(ai_train):
    trainable = (cat == "human") | np.isin(cat, list(ai_train)); s = np.zeros(len(X))
    for f in range(5):
        tr = np.where((fold != f) & trainable)[0]; te = np.where(fold == f)[0]
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True, max_features=300000)
        clf = LogisticRegression(max_iter=3000, C=4, class_weight="balanced").fit(vec.fit_transform([X[i] for i in tr]), y[tr])
        s[te] = clf.predict_proba(vec.transform([X[i] for i in te]))[:, 1]
    return s

if __name__ == "__main__":
    print("texts:", {c: int((cat == c).sum()) for c in ["human"] + ORDER})
    print("median words:", {c: int(np.median([len(X[i].split()) for i in np.where(cat == c)[0]])) for c in ["human"] + ORDER})
    for name, ai in REGIMES.items():
        s = run(ai); hs = s[cat == "human"]
        print(f"\n{name}")
        for fp in (0.01, 0.05):
            thr = np.quantile(hs, 1 - fp)
            print(f"  {fp:.0%} of human passages flagged -> " + " | ".join(
                f"{c} {int((s[cat == c] > thr).sum())}/{int((cat == c).sum())}" for c in ORDER))
