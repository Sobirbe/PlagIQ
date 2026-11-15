"""Uzbek AI-text detection with XLM-R (FacebookAI/xlm-roberta-base, MIT licence, pinned revision), on exactly the texts,
topic-grouped folds and human-set thresholds of combined.py, so results compare directly with the char n-gram detector.

    python3 xlmr_study.py frozen            # XLM-R as a fixed encoder (mean-pooled) + logistic regression; minutes
    python3 xlmr_study.py finetune E4 [f..] # fine-tune XLM-R per fold (top 4 layers; CPU), saving each fold
    python3 xlmr_study.py collect E4        # report a finished fine-tuning run

Besides the abstracts it scores, with the fold models (averaged): full-length AI paragraphs (Gemini articles, the ChatGPT
dissertation) and human full-text passages from fetch_fulltext.py (false alarms on full-length human writing).
Model directory: $XLMR_DIR. Fold scores are saved to xlmr_scores_<mode>_<regime>.json (gitignored: they index other
authors' texts only by position, but stay local with them)."""
import json, os, re, sys, time, numpy as np, torch
from sklearn.linear_model import LogisticRegression
from transformers import AutoTokenizer, AutoModel, AutoModelForSequenceClassification
from combined import X, y, cat, fold, REGIMES, ORDER
from baseline import clean

MODEL = os.environ.get("XLMR_DIR", "xlmr"); MAXLEN = int(os.environ.get("MAXLEN", "192"))
FREEZE = int(os.environ.get("FREEZE", "8"))   # lower encoder layers kept fixed when fine-tuning (CPU budget)
torch.manual_seed(0); torch.set_num_threads(int(os.environ.get("THREADS", "4")))
tok = AutoTokenizer.from_pretrained(MODEL)

def extra_sets():
    """Full-length texts scored by every fold model: AI paragraphs and human full-text passages."""
    sets = {}
    for name, f in (("Gemini article paragraphs", "gemini1.txt"), ("Gemini 'humanized' paragraphs", "gemini2_humanized.txt"),
                    ("ChatGPT dissertation paragraphs", "chatgpt_dissertation.txt")):
        from score_articles import paragraphs
        sets[name] = paragraphs(f)[0]
    if os.path.exists("human_full_uz.json"):
        sets["human full-text passages"] = [clean(c) for d in json.load(open("human_full_uz.json")) for c in d["chunks"]]
    return sets

def embed(texts, model, bs=16):
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), bs):
            b = tok(texts[i:i + bs], truncation=True, max_length=MAXLEN, padding=True, return_tensors="pt")
            h = model(**b).last_hidden_state; m = b["attention_mask"].unsqueeze(-1)
            out.append(((h * m).sum(1) / m.sum(1)).numpy())
    return np.vstack(out)

def frozen(regime):
    enc = AutoModel.from_pretrained(MODEL).eval()
    ex = extra_sets(); names = list(ex); allx = [t for n in names for t in ex[n]]
    t0 = time.time(); E = embed(list(X), enc); EX = embed(allx, enc) if allx else None
    print(f"embedded {len(X) + len(allx)} texts in {time.time() - t0:.0f}s", flush=True)
    trainable = (cat == "human") | np.isin(cat, list(REGIMES[regime])); s = np.zeros(len(X)); exs = []
    for f in range(5):
        tr = np.where((fold != f) & trainable)[0]; te = np.where(fold == f)[0]
        clf = LogisticRegression(max_iter=5000, C=1.0, class_weight="balanced").fit(E[tr], y[tr])
        s[te] = clf.predict_proba(E[te])[:, 1]
        if EX is not None: exs.append(clf.predict_proba(EX)[:, 1])
    return s, names, ex, (np.mean(exs, 0) if exs else None)

def finetune(regime, folds=range(5), epochs=3, bs=8, lr=3e-5):
    """Fine-tunes one model per fold; each fold's scores are saved to xlmr_fold_<regime>_<f>.json (resumable)."""
    ex = extra_sets(); names = list(ex); allx = [t for n in names for t in ex[n]]
    trainable = (cat == "human") | np.isin(cat, list(REGIMES[regime]))
    for f in folds:
        out = f"xlmr_fold_{regime}_{f}.json"
        if os.path.exists(out): continue
        model = AutoModelForSequenceClassification.from_pretrained(MODEL, num_labels=2)
        for p in model.roberta.embeddings.parameters(): p.requires_grad = False
        for layer in model.roberta.encoder.layer[:FREEZE]:
            for p in layer.parameters(): p.requires_grad = False
        tr = np.where((fold != f) & trainable)[0]; te = np.where(fold == f)[0]
        w = torch.tensor([len(tr) / (2 * (y[tr] == 0).sum()), len(tr) / (2 * (y[tr] == 1).sum())], dtype=torch.float)
        opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr, weight_decay=0.01)
        steps = epochs * ((len(tr) + bs - 1) // bs); sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda k: min(1.0, (k + 1) / (0.1 * steps)) * max(0.0, (steps - k) / steps))
        rng = np.random.default_rng(f); t0 = time.time(); model.train()
        for ep in range(epochs):
            order = rng.permutation(tr)
            for i in range(0, len(order), bs):
                idx = order[i:i + bs]
                b = tok([X[j] for j in idx], truncation=True, max_length=MAXLEN, padding=True, return_tensors="pt")
                loss = torch.nn.functional.cross_entropy(model(**b).logits, torch.tensor(y[idx]), weight=w)
                loss.backward(); torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0); opt.step(); sched.step(); opt.zero_grad()
                if (i // bs) % 20 == 0: print(f"fold {f} epoch {ep + 1} step {i // bs} loss {loss.item():.3f} {time.time() - t0:.0f}s", flush=True)
            print(f"fold {f} epoch {ep + 1}/{epochs} done, {time.time() - t0:.0f}s", flush=True)
        model.eval()
        def score(texts):
            out = []
            with torch.no_grad():
                for i in range(0, len(texts), 16):
                    b = tok(texts[i:i + 16], truncation=True, max_length=MAXLEN, padding=True, return_tensors="pt")
                    out.append(torch.softmax(model(**b).logits, -1)[:, 1].numpy())
            return np.concatenate(out) if out else np.array([])
        json.dump({"test": te.tolist(), "scores": score([X[j] for j in te]).tolist(), "extra": score(allx).tolist() if allx else [],
                   "extra_names": names, "extra_sizes": [len(ex[n]) for n in names]}, open(out, "w"))
        print(f"fold {f} saved, {time.time() - t0:.0f}s", flush=True)
        del model, opt

def collect(regime):
    """Combines the saved fold files of a fine-tuning run (all five must exist)."""
    s = np.zeros(len(X)); exs = []
    for f in range(5):
        d = json.load(open(f"xlmr_fold_{regime}_{f}.json")); s[d["test"]] = d["scores"]; exs.append(d["extra"])
    names, sizes = d["extra_names"], d["extra_sizes"]
    ex = {n: [None] * k for n, k in zip(names, sizes)}       # sizes only; the report needs no texts
    n_min = min(len(e) for e in exs); exs = np.mean([e[:n_min] for e in exs], 0) if n_min else None
    return s, names, ex, exs

def report(title, s, names, ex, exs):
    hs = s[cat == "human"]; print(f"\n{title}")
    for fp in (0.01, 0.05):
        thr = np.quantile(hs, 1 - fp)
        print(f"  {fp:.0%} false alarms on human abstracts -> " + " | ".join(f"{c} {np.mean(s[cat == c] > thr):.0%}" for c in ORDER))
        if exs is not None:
            k = 0; parts = []
            for n in names:
                v = exs[k:k + len(ex[n])]; k += len(ex[n]); parts.append(f"{n} {int((v > thr).sum())}/{len(v)}")
            print("     full-length: " + " | ".join(parts))

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "frozen"
    if mode == "finetune":                                    # python3 xlmr_study.py finetune E4 [fold ...]
        finetune(sys.argv[2], [int(a) for a in sys.argv[3:]] or range(5)); sys.exit()
    regimes = sys.argv[2:] or ["E2", "E3", "E4"]
    for r in regimes:
        res = frozen(r) if mode == "frozen" else collect(r)     # "collect E4" reports a finished fine-tuning run
        json.dump({"scores": res[0].tolist(), "extra": None if res[3] is None else res[3].tolist(), "extra_names": res[1],
                   "extra_sizes": [len(res[2][n]) for n in res[1]]}, open(f"xlmr_scores_{mode}_{r}.json", "w"))
        report(f"XLM-R {mode}, {r}: trained on human + {', '.join(sorted(REGIMES[r]))}", *res)
