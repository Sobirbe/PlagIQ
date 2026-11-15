"""Uzbek AI-text detection with Claude + two independent ChatGPT runs.

Every text belongs to a topic group (a human abstract, the AI abstracts written from its title, the AI polishes of it);
5-fold cross-validation splits by group, so a test text's topic is never seen in training. Thresholds come from the
held-out human abstracts, so the false-alarm rates are measured. Three training regimes:
  E1  human + Claude-written                      (what we had)
  E2  E1 + ChatGPT-written (both runs)
  E3  E2 + AI-polished (Claude and ChatGPT)
Human originals of the polished texts are never trained on; they are scored as extra human test texts."""
import json, zlib, numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from baseline import clean, uzbek_only

H = json.load(open("human_uz.json")); sp = json.load(open("split.json")); plan = json.load(open("chatgpt_plan.json"))
idx = {h["id"]: i for i, h in enumerate(H)}
items = []                                    # (text, label, category, group)
def add(t, lab, cat, grp): items.append((clean(t), lab, cat, str(grp)))

polish_orig = {}                              # human index -> who polished it
for k in json.load(open("ai_polished1.json")): polish_orig.setdefault(sp["polish"][int(k)], []).append("claude")
for pid in plan["polish_ids"]: polish_orig.setdefault(idx[pid], []).append("gpt")
for i, h in enumerate(H):
    if uzbek_only(h["text"]):
        add(h["text"], 0, "human (polished originals)" if i in polish_orig else "human", f"h{i}")
for n, t in enumerate(json.load(open("ai_batch1.json"))): add(t, 1, "Claude written", f"c{n}")
for k, t in json.load(open("ai_polished1.json")).items(): add(t, 1, "Claude polished", f"h{sp['polish'][int(k)]}")
for run, f in (("ChatGPT run 1", "chatgpt.json"), ("ChatGPT run 2", "chatgpt2.json")):
    for blk, texts in json.load(open(f)).items():
        b = int(blk[1:]) - 1
        for n, t in enumerate(texts):
            if blk[0] == "A": add(t, 1, f"{run} written", f"h{idx[plan['generate_titles'][b * 10 + n]]}")
            else: add(t, 1, f"{run} polished", f"h{idx[plan['polish_ids'][b * 5 + n]]}")

X = [t for t, *_ in items]; y = np.array([l for _, l, *_ in items]); cat = np.array([c for _, _, c, _ in items])
fold = np.array([zlib.crc32(g.encode()) % 5 for *_, g in items])
REGIMES = {"E1": {"Claude written"},
           "E2": {"Claude written", "ChatGPT run 1 written", "ChatGPT run 2 written"},
           "E3": {"Claude written", "ChatGPT run 1 written", "ChatGPT run 2 written",
                  "Claude polished", "ChatGPT run 1 polished", "ChatGPT run 2 polished"}}
ORDER = ["Claude written", "ChatGPT run 1 written", "ChatGPT run 2 written",
         "Claude polished", "ChatGPT run 1 polished", "ChatGPT run 2 polished", "human (polished originals)"]
print("texts:", {c: int((cat == c).sum()) for c in ["human"] + ORDER})
for name, ai_train in REGIMES.items():
    trainable = (cat == "human") | np.isin(cat, list(ai_train))
    s = np.zeros(len(X))
    for f in range(5):
        tr = np.where((fold != f) & trainable)[0]; te = np.where(fold == f)[0]
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True)
        clf = LogisticRegression(max_iter=3000, C=4, class_weight="balanced").fit(vec.fit_transform([X[i] for i in tr]), y[tr])
        s[te] = clf.predict_proba(vec.transform([X[i] for i in te]))[:, 1]
    hs = s[cat == "human"]
    print(f"\n{name}: trained on human + {', '.join(sorted(ai_train))}")
    for fp in (0.01, 0.05):
        thr = np.quantile(hs, 1 - fp)
        print(f"  {fp:.0%} false alarms on human -> " + " | ".join(f"{c} {np.mean(s[cat == c] > thr):.0%}" for c in ORDER))
