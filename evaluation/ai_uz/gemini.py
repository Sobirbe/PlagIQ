"""Score Gemini-written Uzbek articles (gemini1.txt, or the file given, never seen in training) paragraph by paragraph.
Only prose paragraphs are scored: headings, display formulas and reference lists are dropped and inline $...$ removed.
Thresholds come from held-out human abstracts (combined.py folds), so a paragraph is flagged only if it scores above
what 1 % / 5 % of human abstracts score. Note the genre gap: training has abstracts, these are article sections."""
import re, sys, numpy as np
from combined import fit_folds, cat, REGIMES
from baseline import clean

arts = re.split(r"(?m)^===== ARTICLE \d+: (.+?) =====\s*$", open(sys.argv[1] if len(sys.argv) > 1 else "gemini1.txt").read())
paras, owner = [], []
for name, body in zip(arts[1::2], arts[2::2]):
    for p in re.split(r"\n\s*\n", body.split("ADABIYOTLAR VA QISQARTMALAR")[0]):
        p = re.sub(r"\$[^$]*\$", " ", p.strip())
        if p.startswith("MAVZU") or len(p.split()) < 40: continue        # headings and formulas fall out here
        p = re.sub(r"^[A-Z‘’ ]+\n", "", p)                               # section heading on the first line
        paras.append(clean(p)); owner.append(name)
owner = np.array(owner)
for regime in ("E1", "E2"):
    s, g = fit_folds(REGIMES[regime], paras)
    hs = s[cat == "human"]
    print(f"\n{regime} ({len(paras)} Gemini paragraphs, trained without Gemini):")
    for fp in (0.01, 0.05):
        thr = np.quantile(hs, 1 - fp)
        per = " | ".join(f"{n.split(' &')[0].split()[0].title()} {int((g[owner == n] > thr).sum())}/{int((owner == n).sum())}"
                         for n in dict.fromkeys(owner))
        print(f"  {fp:.0%} false alarms on human -> {np.mean(g > thr):.0%} of paragraphs flagged ({per})")
    print("  median score: Gemini paragraphs %.2f, human abstracts %.2f" % (np.median(g), np.median(hs)))
