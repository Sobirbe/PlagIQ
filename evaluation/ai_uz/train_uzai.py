"""Train PlagIQ's Uzbek AI-writing indicator (uzai.py) and set its threshold.

Training data (see AI_UZ.md): human Uzbek academic text published 2010-2021 — abstracts (human_uz.json) and passages of
open-access works (human_full_uz.json) — and 2021-2026 journal papers the owner confirmed as human-written and may use
(human_recent_uz.json; JMMF's 2026 issues held out, see HOLD_OUT); chatbot text — abstracts written by Claude, ChatGPT (three runs) and Gemini, and
full-length sections, continuations, articles and a dissertation by Gemini, Claude and ChatGPT. Polished texts are left
out (training on them did not help and they sit closest to human writing). The features are uzai.features, the code the
website runs, so training and serving cannot drift apart.

Threshold: 5-fold cross-validation grouped by topic/source work (as in the study); the threshold is the higher of the
scores above which 1 % of held-out human passages and 1 % of held-out human abstracts fall — a student's abstract is scored
like any other passage on the website, and abstracts score higher. The final model is trained on everything.
Writes ../../models/uz_ai_v1.npz and prints the cross-validated catch and false-alarm rates.

    python3 train_uzai.py          (needs human_uz.json and human_full_uz.json, which are not in the repository)
"""
import os, sys, time, zlib, numpy as np
from pathlib import Path
from scipy import sparse
from sklearn.linear_model import LogisticRegression
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
import uzai
import combined as A                      # abstracts: texts, labels, categories, topic groups
import full_study as F                    # full-length: texts, labels, categories, source-work groups

HUMAN = {"human", "human (polished originals)", "human originals of D/E", "human journals 2021-26 (owner-confirmed)"}
SKIP = {"Claude polished", "ChatGPT run 1 polished", "ChatGPT run 2 polished", "Gemini polished",
        "Gemini polished (D)", "Claude polished (D)", "ChatGPT polished (D)",
        "human + Gemini continuation (E)", "human + Claude continuation (E)", "human + ChatGPT continuation (E)"}
texts, y, cat, group, unit = [], [], [], [], []
for X, Y, C, G, u in ((A.X, A.y, A.cat, [g for *_, g in A.items], "abstract"), (F.X, F.y, F.cat, [g for *_, g in F.items], "passage")):
    for t, l, c, g in zip(X, Y, C, G):
        if c in SKIP: continue
        texts.append(t); y.append(0 if c in HUMAN else 1); cat.append(c); group.append(f"{u}:{g}"); unit.append(u)
# Recent journal papers (2021-2026) that the owner confirmed as human-written and has permission to use
# (human_recent_uz.json, built by build_recent.py; not in the repository): grouped by article, so each is held out from
# the model that scores it.
recent = Path(__file__).with_name("human_recent_uz.json")
# JMMF 2026 issues: some articles read as chatbot-written (with the earlier model, flags rose from ~0 % in 2024 to 2-9 %
# of passages in 2026, clustered in whole articles), so by the owner's decision they are not trained on as human.
HOLD_OUT = {"MMF-Volume-3-Issue-1_JAN-FEB_2026", "MMF-Volume-3-Issue-2_mart-2026", "MMF-Volume-3-Issue-3_aprel-2026",
            "volume_1784811619_721", "volume_1784811656_423", "volume_1784811736_454", "volume_1790679567_129"}
held_out_test = []
# The whole JMMF journal is kept out of training and used as a real-world test set: with its 2024-2025 issues trained on
# as human, ChatGPT sections were caught less (13 of 24 against 17) with no fewer false alarms (0 either way).
# HOLD_SOURCE="" trains on JMMF 2024-2025 as well (2026 stays held out).
HOLD_SOURCE = os.environ.get("HOLD_SOURCE", "Management")
if recent.is_file():
    import json
    for d in json.load(open(recent)):
        if d.get("issue") in HOLD_OUT or (HOLD_SOURCE and d.get("source", "").startswith(HOLD_SOURCE)):                   # kept out of training; only scored at the end
            held_out_test.append((d["issue"], d["chunks"])); continue
        for c in d["chunks"]:
            texts.append(uzai.clean(c)); y.append(0); cat.append("human journals 2021-26 (owner-confirmed)")
            group.append(f"recent:{d['id']}"); unit.append("passage")
y, cat, unit = np.array(y), np.array(cat), np.array(unit)
fold = np.array([zlib.crc32(g.encode()) % 5 for g in group])
print(f"{len(texts)} texts: {int((y == 0).sum())} human, {int((y == 1).sum())} chatbot", flush=True)

t0 = time.time(); rows, cols, vals = [], [], []
for i, t in enumerate(texts):
    idx, tf = uzai.features(t); rows.append(np.full(len(idx), i)); cols.append(idx); vals.append(tf)
TF = sparse.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(len(texts), uzai.BUCKETS))
print(f"features in {time.time() - t0:.0f}s", flush=True)

def fit(rows_idx):
    sub = TF[rows_idx]; df = np.bincount(sub.indices, minlength=uzai.BUCKETS)
    idf = np.log((1 + sub.shape[0]) / (1 + df)) + 1
    Xs = sub.multiply(idf).tocsr(); Xs = sparse.diags(1 / np.maximum(sparse.linalg.norm(Xs, axis=1), 1e-12)) @ Xs
    clf = LogisticRegression(max_iter=3000, C=4, class_weight="balanced").fit(Xs, y[rows_idx])
    return idf, df, clf

def predict(idf, clf, rows_idx):
    Xs = TF[rows_idx].multiply(idf).tocsr(); Xs = sparse.diags(1 / np.maximum(sparse.linalg.norm(Xs, axis=1), 1e-12)) @ Xs
    return clf.predict_proba(Xs)[:, 1]

held = np.zeros(len(texts))
for f in range(5):
    tr, te = np.where(fold != f)[0], np.where(fold == f)[0]
    idf, _, clf = fit(tr); held[te] = predict(idf, clf, te)
    print(f"fold {f} done", flush=True)
hp = held[(y == 0) & (unit == "passage")]; ha = held[(y == 0) & (unit == "abstract")]
thr = float(max(np.quantile(hp, 0.99), np.quantile(ha, 0.99)))
print(f"\nthreshold (1 % of held-out human passages and of human abstracts, the higher): {thr:.4f}")
print(f"held-out human above it: passages {np.mean(hp > thr):.2%} ({int((hp > thr).sum())}/{len(hp)}), "
      f"abstracts {np.mean(ha > thr):.2%} ({int((ha > thr).sum())}/{len(ha)})")
hr = held[cat == "human journals 2021-26 (owner-confirmed)"]
if len(hr): print(f"held-out human journal passages (2021-26) above it: {int((hr > thr).sum())}/{len(hr)}")
for c in sorted(set(cat[y == 1])):
    m = cat == c; print(f"  {c}: {int((held[m] > thr).sum())}/{int(m.sum())} caught (held out)")

idf, df, clf = fit(np.arange(len(texts)))
coef = clf.coef_[0]; keep = np.where(np.abs(coef) > 1e-7)[0]; seen = np.where(df > 0)[0]
out = ROOT / "models" / "uz_ai_v1.npz"; out.parent.mkdir(exist_ok=True)
np.savez_compressed(out, coef_idx=keep.astype(np.int32), coef_val=coef[keep].astype(np.float32),
                    idf_idx=seen.astype(np.int32), idf_val=idf[seen].astype(np.float32),
                    intercept=np.float64(clf.intercept_[0]), threshold=np.float64(thr),
                    version=np.str_(f"uz-ai-v1 ({int((y == 0).sum())} human / {int((y == 1).sum())} chatbot texts, "
                                    f"{time.strftime('%Y-%m-%d')})"))
print(f"\nwrote {out} ({out.stat().st_size // 1024} KB)")
if held_out_test:
    rows = [TFX for TFX in ([(issue, c) for issue, ch in held_out_test for c in ch])]
    Xh = sparse.vstack([sparse.csr_matrix((uzai.features(c)[1], (np.zeros(len(uzai.features(c)[0]), int), uzai.features(c)[0])),
                                          shape=(1, uzai.BUCKETS)) for _, c in rows]).tocsr()
    Xh = Xh.multiply(idf).tocsr(); Xh = sparse.diags(1 / np.maximum(sparse.linalg.norm(Xh, axis=1), 1e-12)) @ Xh
    sh = clf.predict_proba(Xh)[:, 1]; issues = np.array([i for i, _ in rows])
    print(f"held-out journal issues (never trained on): {int((sh > thr).sum())}/{len(sh)} passages flagged")
    for i in dict.fromkeys(issues):
        m = issues == i; print(f"   {i}: {int((sh[m] > thr).sum())}/{int(m.sum())}")
