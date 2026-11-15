"""Does a classifier trained on human + Claude-written Uzbek abstracts recognise ChatGPT-written ones it never saw?
Thresholds are set on held-out human abstracts (5-fold), so the false-alarm rates are measured, not assumed."""
import json, re, numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
from baseline import clean, uzbek_only
H = json.load(open("human_uz.json")); sp = json.load(open("split.json"))
pol = json.load(open("ai_polished1.json")); polish_idx = {sp["polish"][int(k)] for k in pol}
gpt = [clean(t) for block in json.load(open("chatgpt.json")).values() for t in block]
X_h = [clean(h["text"]) for i, h in enumerate(H) if uzbek_only(h["text"]) and i not in polish_idx]
X_c = [clean(t) for t in json.load(open("ai_batch1.json"))]
X, y = X_h + X_c, np.array([0] * len(X_h) + [1] * len(X_c))
held, gpt_scores = np.zeros(len(X)), []
for tr, te in StratifiedKFold(5, shuffle=True, random_state=1).split(X, y):
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True)
    clf = LogisticRegression(max_iter=3000, C=4, class_weight="balanced").fit(vec.fit_transform([X[i] for i in tr]), y[tr])
    held[te] = clf.predict_proba(vec.transform([X[i] for i in te]))[:, 1]
    gpt_scores.append(clf.predict_proba(vec.transform(gpt))[:, 1])
g = np.mean(gpt_scores, 0); hs = held[y == 0]
print(f"human {len(X_h)}, Claude {len(X_c)} (training); ChatGPT {len(gpt)} (never seen)")
for fp in (0.01, 0.05):
    thr = np.quantile(hs, 1 - fp)
    print(f"  threshold for {fp:.0%} false alarms on human: Claude (held out) {np.mean(held[y == 1] > thr):.0%}, ChatGPT {np.mean(g > thr):.0%}")
