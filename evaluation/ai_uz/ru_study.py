"""Russian AI-text detection, same method as combined.py: human Russian abstracts 2010-2021 by authors at Uzbek
institutions (human_ru.json, from fetch_human_ru.py) against chatbot answers to prompts-ru.txt (ru_<chatbot>.json).

Formatting is removed first (LaTeX $...$, Markdown, quote/dash variants, ё), so the classifier cannot use it.
Texts are grouped by topic; 5-fold cross-validation splits by group; thresholds come from held-out human abstracts.
For every chatbot it reports: caught when that chatbot is in training ("seen") and when it is left out ("unseen")."""
import glob, json, re, zlib, numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

def clean(t):
    t = re.sub(r"\$[^$]*\$", " ", t)                                   # LaTeX formulas
    t = re.sub(r"[*_#`]+", "", t).lower().replace("ё", "е")
    t = re.sub(r"[«»“”„\"]", '"', t); t = re.sub(r"[‐‑‒–—―]", "-", t); t = re.sub(r"[’‘`´]", "'", t)
    t = re.sub(r"\s*([.,;:])\s*", r"\1 ", t)
    return re.sub(r"\s+", " ", t).strip()

H = json.load(open("human_ru.json")); plan = json.load(open("ru_plan.json")); idx = {h["id"]: i for i, h in enumerate(H)}
bots = {re.match(r"ru_(\w+)\.json", f).group(1): json.load(open(f)) for f in sorted(glob.glob("ru_*.json")) if f != "ru_plan.json"}
pol_orig = {idx[p] for p in plan["polish_ids"]}
items = [(clean(h["text"]), 0, "human (polished originals)" if i in pol_orig else "human", f"h{i}") for i, h in enumerate(H)]
for bot, blocks in bots.items():
    for blk, texts in blocks.items():
        b = int(blk[1:]) - 1
        for n, t in enumerate(texts):
            src = plan["generate_titles"][b * 10 + n] if blk[0] == "A" else plan["polish_ids"][b * 5 + n]
            items.append((clean(t), 1, f"{bot} {'written' if blk[0] == 'A' else 'polished'}", f"h{idx[src]}"))
X = [t for t, *_ in items]; y = np.array([l for _, l, *_ in items]); cat = np.array([c for _, _, c, _ in items])
fold = np.array([zlib.crc32(g.encode()) % 5 for *_, g in items])

def run(ai_train):
    trainable = (cat == "human") | np.isin(cat, list(ai_train)); s = np.zeros(len(X))
    for f in range(5):
        tr = np.where((fold != f) & trainable)[0]; te = np.where(fold == f)[0]
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True)
        clf = LogisticRegression(max_iter=3000, C=4, class_weight="balanced").fit(vec.fit_transform([X[i] for i in tr]), y[tr])
        s[te] = clf.predict_proba(vec.transform([X[i] for i in te]))[:, 1]
    return s

def report(name, s):
    hs = s[cat == "human"]
    print(f"\n{name}")
    for fp in (0.01, 0.05):
        thr = np.quantile(hs, 1 - fp)
        print(f"  {fp:.0%} false alarms on human -> " + " | ".join(
            f"{c} {np.mean(s[cat == c] > thr):.0%}" for c in sorted(set(cat)) if c != "human"))

if __name__ == "__main__":
    print("texts:", {c: int((cat == c).sum()) for c in sorted(set(cat))})
    written = {f"{b} written" for b in bots}
    report("trained on human + written by " + ", ".join(bots), run(written))
    if len(bots) > 1:
        for b in bots:
            report(f"trained without {b} (is {b} recognised unseen?)", run(written - {f"{b} written"}))
