"""Train PlagIQ's Uzbek AI-writing indicator (uzai.py) and set its threshold.

Training data (see AI_UZ.md): human Uzbek academic text published before ChatGPT — abstracts 2010-2021 (human_uz.json),
passages of open-access works 2010-2021 (human_full_uz.json) and journal issues 2021-2022 the owner may use
(human_recent_uz.json; later issues are only scored, by year); chatbot text — abstracts written by Claude, ChatGPT (three runs) and Gemini, and
full-length sections, continuations, articles and a dissertation by Gemini, Claude and ChatGPT. Polished texts are left
out (training on them did not help and they sit closest to human writing). The features are uzai.features, the code the
website runs, so training and serving cannot drift apart.

Threshold: 5-fold cross-validation grouped by topic/source work (as in the study); the threshold is the higher of the
scores above which 1 % of held-out human passages and 1 % of held-out human abstracts fall — a student's abstract is scored
like any other passage on the website, and abstracts score higher. The final model is trained on everything.
Writes ../../models/uz_ai_v1.npz and prints the cross-validated catch and false-alarm rates.

    python3 train_uzai.py          (needs human_uz.json and human_full_uz.json, which are not in the repository)
"""
import sys, time, zlib, numpy as np
from pathlib import Path
from scipy import sparse
from sklearn.linear_model import LogisticRegression
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
import uzai
import combined as A                      # abstracts: texts, labels, categories, topic groups
import full_study as F                    # full-length: texts, labels, categories, source-work groups

HUMAN = {"human", "human (polished originals)", "human originals of D/E", "human journals 2021-22"}
SKIP = {"Claude polished", "ChatGPT run 1 polished", "ChatGPT run 2 polished", "Gemini polished",
        "Gemini polished (D)", "Claude polished (D)", "ChatGPT polished (D)",
        "human + Gemini continuation (E)", "human + Claude continuation (E)", "human + ChatGPT continuation (E)"}
texts, y, cat, group, unit = [], [], [], [], []
for X, Y, C, G, u in ((A.X, A.y, A.cat, [g for *_, g in A.items], "abstract"), (F.X, F.y, F.cat, [g for *_, g in F.items], "passage")):
    for t, l, c, g in zip(X, Y, C, G):
        if c in SKIP: continue
        texts.append(t); y.append(0 if c in HUMAN else 1); cat.append(c); group.append(f"{u}:{g}"); unit.append(u)
# Journal issues the owner may use (human_recent_uz.json, built by build_recent.py; not in the repository). Only issues
# published before ChatGPT (30 November 2022) are trained on as human: the owner reports that most Uzbek publications
# since then are partly AI-generated, so later issues cannot be labelled human. They are scored at the end, by year.
ISSUE_YEAR = {**{str(i): 2021 for i in (8, 10, 11, 12, 13)}, **{str(i): 2022 for i in (14, 15, 16, 17, 18)},
              **{str(i): 2023 for i in (19, 20, 21, 23, 24)}, **{str(i): 2024 for i in (25, 26, 27, 28)},
              **{str(i): 2025 for i in (29, 30, 31, 32)}, **{str(i): 2026 for i in (33, 34)},          # IMRS issue ids
              "agrobiznes_2023_2": 2023, "mmf_1_1_owner": 2024, "mmf_1_1": 2024, "2-son-MMM-v5_1": 2024,
              "MMM-3-son-v1": 2024, "MMM_4-son_v1": 2024, "MMF-Volume-2-Issue1.v1": 2025, "MMF_Volume_2_Issue_2.v1": 2025,
              "MMF-Volume-2-Issue-3": 2025, "MMF-Volume-2-Issue-4": 2025, "MMF-Volume-2-Issue-5": 2025,
              "MMF-Volume-2-Issue-6": 2025, "volume_1784811238_253": 2025, "volume_1784811344_316": 2025,
              "volume_1784811409_327": 2025, "volume_1784811619_721": 2026, "volume_1784811656_423": 2026,
              "volume_1784811736_454": 2026, "volume_1790679567_129": 2026, "MMF-Volume-3-Issue-1_JAN-FEB_2026": 2026,
              "MMF-Volume-3-Issue-2_mart-2026": 2026, "MMF-Volume-3-Issue-3_aprel-2026": 2026}
# Options (to measure each change; the model is written only without --no-write):
#   --keep-late-zenodo  keep open-access works whose Zenodo record was uploaded after ChatGPT (zenodo_created.json)
#   --no-ilmiz          leave out the ilmiz.uz abstracts (ilmiz_abstracts_uz.json, from fetch_ilmiz.py)
#   --no-oak            leave out the OAK journal full texts (oak_full_uz.json, from fetch_oak_fulltext.py)
ARGS = set(sys.argv[1:])
import json, re
if "--keep-late-zenodo" not in ARGS and Path(__file__).with_name("zenodo_created.json").is_file():
    # 60 of 139 Zenodo records in human_full_uz.json were uploaded after 30 Nov 2022 although they claim earlier years:
    # their passages cannot be counted as human for certain, so they are left out of training and testing.
    zc = json.load(open(Path(__file__).with_name("zenodo_created.json")))
    url = {w["id"]: w.get("url") or "" for w in json.load(open(Path(__file__).with_name("human_full_uz.json")))}
    def late(work_id):
        m = re.search(r"zenodo\.org/records?/(\d+)", url.get(work_id, ""))
        return bool(m) and zc.get(m.group(1), "9999") >= "2022-11-30"
    drop = [i for i, (c, g, u) in enumerate(zip(cat, group, unit)) if u == "passage" and c in HUMAN and late(g.split(":", 1)[1])]
    for i in reversed(drop):
        del texts[i], y[i], cat[i], group[i], unit[i]
    print(f"left out {len(drop)} passages of Zenodo works uploaded after ChatGPT", flush=True)
ilmiz = Path(__file__).with_name("ilmiz_abstracts_uz.json")
if "--no-ilmiz" not in ARGS and ilmiz.is_file():
    # Uzbek abstracts from OAK journals published before 30 Nov 2022 (ilmiz.uz index), grouped by journal so a journal's
    # house style never sits on both sides of a fold.
    # ilmiz.uz marks many English and Karakalpak abstracts as Uzbek: only those the site's own language check reads as
    # Uzbek are kept (660 of 1,538 in November 2026).
    import corpus
    n0 = len(texts)
    for a in json.load(open(ilmiz)).values():
        t = uzai.clean(a["abstract"])
        if len(t.split()) >= 40 and corpus.detect_language(a["abstract"]) == "uz":
            texts.append(t); y.append(0); cat.append("human abstracts ilmiz.uz 2010-22")
            group.append(f"ilmiz:{a['journal']}"); unit.append("abstract")
    HUMAN = HUMAN | {"human abstracts ilmiz.uz 2010-22"}
    print(f"added {len(texts) - n0} ilmiz.uz abstracts", flush=True)
oak = Path(__file__).with_name("oak_full_uz.json")
if "--no-oak" not in ARGS and oak.is_file():
    # Full texts of open-licence OAK journal articles from before ChatGPT (fetch_oak_fulltext.py), grouped by journal.
    # "datestamp": the record existed unchanged before 30 Nov 2022; "dc:date": only its publication date says so.
    have = {(w.get("url") or "").rstrip("/") for w in json.load(open(Path(__file__).with_name("human_full_uz.json")))}
    n0, dup = len(texts), 0
    for a in json.load(open(oak)):
        if a["url"].rstrip("/") in have: dup += 1; continue
        c = f"human OAK journals ({a.get('basis', 'datestamp')})"
        for t in a["chunks"]:
            texts.append(uzai.clean(t)); y.append(0); cat.append(c); group.append(f"oak:{a['journal']}"); unit.append("passage")
        HUMAN = HUMAN | {c}
    print(f"added {len(texts) - n0} OAK journal passages ({dup} articles already in human_full_uz.json skipped)", flush=True)
held_out_test = []                                       # (source, year, passages) scored at the end
recent = Path(__file__).with_name("human_recent_uz.json")
if recent.is_file():
    for d in json.load(open(recent)):
        year = ISSUE_YEAR.get(d.get("issue"))
        if year is None or year >= 2023:
            held_out_test.append((d["source"].split(" (")[0], year, d["chunks"])); continue
        for c in d["chunks"]:
            texts.append(uzai.clean(c)); y.append(0); cat.append("human journals 2021-22")
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
for c in sorted(set(cat[y == 0])):
    m = cat == c; print(f"  {c}: {int((held[m] > thr).sum())}/{int(m.sum())} wrongly flagged (held out)")
hr = held[cat == "human journals 2021-22"]
if len(hr): print(f"held-out human journal passages (2021-22) above it: {int((hr > thr).sum())}/{len(hr)}")
for c in sorted(set(cat[y == 1])):
    m = cat == c; print(f"  {c}: {int((held[m] > thr).sum())}/{int(m.sum())} caught (held out)")

if "--no-write" in ARGS: raise SystemExit
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
    from collections import defaultdict
    tally = defaultdict(lambda: [0, 0])
    for source, year, chunks_ in held_out_test:
        for c in chunks_:
            idx, tf = uzai.features(c); v = tf * idf[idx]; nrm = np.linalg.norm(v)
            p = 1 / (1 + np.exp(-(clf.intercept_[0] + (v @ coef[idx]) / nrm))) if nrm else 0.0
            tally[(source, year)][0] += p > thr; tally[(source, year)][1] += 1
    print("2023+ journal passages (not trained on), flagged by year:")
    for (source, year), (f, n) in sorted(tally.items(), key=lambda x: (x[0][0], x[0][1] or 0)):
        print(f"   {source[:40]:40s} {year}: {f}/{n} ({f / n:.1%})")
