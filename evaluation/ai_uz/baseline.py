"""Feasibility only: can a simple style classifier tell Claude-written / Claude-polished Uzbek abstracts from human ones
(2010-2021)? Text is put into one script and cleaned of formatting quirks first, so the classifier cannot use them."""
import json, re, random, numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
H = json.load(open("human_uz.json")); sp = json.load(open("split.json"))
gen = json.load(open("ai_batch1.json")); pol = json.load(open("ai_polished1.json"))
CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяқғўҳ", ["a","b","v","g","d","e","yo","j","z","i","y","k","l","m","n","o","p","r","s","t","u","f","x","ts","ch","sh","sh","'","i","","e","yu","ya","q","g'","o'","h"]))
RU = re.compile(r"\b(в|и|на|статье|данной|рассматривается|что|это|как|для)\b")
def clean(t):
    t = t.lower()
    for a in "ʻ‘’`ʼ´": t = t.replace(a, "'")
    t = "".join(CYR.get(c, c) for c in t)
    t = re.sub(r"[«»“”„―‖\"]", '"', t); t = re.sub(r"\s*([.,;:])\s*", r"\1 ", t); t = re.sub(r"\s+", " ", t)
    return t.strip()
def uzbek_only(t):        # drop texts that mix in a Russian abstract
    return len(RU.findall(t.lower())) < 3
if __name__ == "__main__":
    polish_src = {int(k) for k in pol}; polish_idx = [sp["polish"][k] for k in polish_src]
    human = [(clean(h["text"]), i) for i, h in enumerate(H) if uzbek_only(h["text"]) and i not in polish_idx]
    X_h = [t for t, _ in human]; X_g = [clean(t) for t in gen]; X_p = [clean(pol[k]) for k in sorted(pol, key=int)]
    X_porig = [clean(H[sp["polish"][int(k)]]["text"]) for k in sorted(pol, key=int)]
    print(f"human {len(X_h)} | AI-generated {len(X_g)} | AI-polished {len(X_p)} (+ their {len(X_porig)} human originals)")
    # 5-fold: train on human + generated folds; polished and their originals are only ever test data
    y = np.array([0]*len(X_h) + [1]*len(X_g)); X = X_h + X_g
    scores = np.zeros(len(X)); pol_scores, orig_scores = [], []
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=1).split(X, y):
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True)
        clf = LogisticRegression(max_iter=3000, C=4, class_weight="balanced")
        clf.fit(vec.fit_transform([X[i] for i in tr]), y[tr])
        scores[te] = clf.predict_proba(vec.transform([X[i] for i in te]))[:, 1]
        pol_scores.append(clf.predict_proba(vec.transform(X_p))[:, 1]); orig_scores.append(clf.predict_proba(vec.transform(X_porig))[:, 1])
    ps, os_ = np.mean(pol_scores, 0), np.mean(orig_scores, 0)
    hs, gs = scores[y == 0], scores[y == 1]
    for fp in (0.01, 0.05):
        thr = np.quantile(hs, 1 - fp)
        print(f"threshold for {fp:.0%} false alarms on human: AI-generated caught {np.mean(gs > thr):.0%} | "
              f"AI-polished caught {np.mean(ps > thr):.0%} | their human originals flagged {np.mean(os_ > thr):.0%}")
