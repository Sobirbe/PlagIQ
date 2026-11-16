"""Full-length Uzbek AI-text detection: passages of human open-access works 2010-2021 (human_full_uz.json, from
fetch_fulltext.py) against full-length chatbot text: the chatbots' answers to prompts-full-uz.txt (full_gemini.json,
full_claude.json, full_chatgpt.json — sections C, polished D, continuations E; a set may be incomplete), the Gemini articles (gemini1.txt,
gemini2_humanized.txt) and the ChatGPT dissertation (chatgpt_dissertation.txt).

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

Hw = json.load(open("human_full_uz.json")); plan = json.load(open("full_plan.json"))
BOTS = {b: json.load(open(f)) for b, f in (("Gemini", "full_gemini.json"), ("Claude", "full_claude.json"), ("ChatGPT", "full_chatgpt.json"))}
D = {w["id"]: w for w in Hw}
items = []                                                          # (text, label, category, group)
orig = {(x["id"], x["chunk"]): x for k in ("d", "e") for x in plan[k]}
for w in Hw:
    for i, c in enumerate(w["chunks"]):
        items.append((clean(c), 0, "human originals of D/E" if (w["id"], i) in orig else "human", w["id"]))
for bot, G in BOTS.items():
    for x in plan["c"]:
        if x["block"] in G: items.append((clean(G[x["block"]][x["n"] - 1]), 1, f"{bot} sections", x["id"]))
    for x in plan["d"]:
        if x["block"] in G: items.append((clean(G[x["block"]][x["n"] - 1]), 1, f"{bot} polished (D)", x["id"]))
    for x in plan["e"]:
        if x["block"] not in G: continue
        cont = G[x["block"]][x["n"] - 1]; first = D[x["id"]]["chunks"][x["chunk"]]
        items.append((clean(first + " " + cont), 1, f"human + {bot} continuation (E)", x["id"]))
        items.append((clean(cont), 1, f"{bot} continuation alone (E)", x["id"]))

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
ORDER = ["Gemini sections", "Claude sections", "ChatGPT sections", "Gemini articles", "Gemini 'humanized' articles",
         "ChatGPT dissertation", "Gemini polished (D)", "Claude polished (D)", "Gemini continuation alone (E)",
         "Claude continuation alone (E)", "human + Gemini continuation (E)", "human + Claude continuation (E)", "human originals of D/E"]
ORDER = [c for c in ORDER if c in set(cat)]
GEM = {"Gemini sections", "Gemini articles", "Gemini 'humanized' articles"}; CLA = {"Claude sections"}
GPT = {"ChatGPT sections", "ChatGPT dissertation"}
WRITTEN = GEM | CLA | GPT
REGIMES = {"all three chatbots' full-length writing": WRITTEN,
           "Claude left out": WRITTEN - CLA,
           "Gemini left out": WRITTEN - GEM,
           "ChatGPT left out": WRITTEN - GPT,
           "all writing + polished": WRITTEN | {"Gemini polished (D)", "Claude polished (D)"}}

def run(ai_train):
    trainable = (cat == "human") | np.isin(cat, list(ai_train)); s = np.zeros(len(X))
    for f in range(5):
        tr = np.where((fold != f) & trainable)[0]; te = np.where(fold == f)[0]
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True, max_features=300000)
        clf = LogisticRegression(max_iter=3000, C=4, class_weight="balanced").fit(vec.fit_transform([X[i] for i in tr]), y[tr])
        s[te] = clf.predict_proba(vec.transform([X[i] for i in te]))[:, 1]
    return s

if __name__ == "__main__":
    import sys
    if "--cut" in sys.argv:                                  # length control: every text cut to its first 120 words
        X = [" ".join(t.split()[:120]) for t in X]; print("all texts cut to their first 120 words")
    print("texts:", {c: int((cat == c).sum()) for c in ["human"] + ORDER})
    print("median words:", {c: int(np.median([len(X[i].split()) for i in np.where(cat == c)[0]])) for c in ["human"] + ORDER})
    for name, ai in REGIMES.items():
        s = run(ai); hs = s[cat == "human"]
        print(f"\n{name}")
        for fp in (0.01, 0.05):
            thr = np.quantile(hs, 1 - fp)
            print(f"  {fp:.0%} of human passages flagged -> " + " | ".join(
                f"{c} {int((s[cat == c] > thr).sum())}/{int((cat == c).sum())}" for c in ORDER))
