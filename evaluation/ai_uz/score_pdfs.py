"""Run the website's Uzbek AI-writing indicator (uzai.py) over recent journal issues (PDF), passage by passage.

Recent papers (2023 on) cannot be labelled human — their authors may have used a chatbot — so they are not training
data. Scored here they give an upper bound on false alarms: every flag is either a false alarm or real chatbot use.
Reference lists are dropped; only passages PlagIQ reads as Uzbek are scored (Cyrillic Uzbek included). The PDFs and their
text are not stored anywhere.

    python3 score_pdfs.py issue1.pdf [issue2.pdf ...]
"""
import re, subprocess, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import uzai
from fetch_fulltext import chunks

REFS = re.compile(r"\n\s*(?:FOYDALANILGAN ADABIYOTLAR(?: RO[‘'ʻ`]YXATI)?|Foydalanilgan adabiyotlar(?: ro[‘'ʻ`]yxati)?|"
                  r"ADABIYOTLAR|Adabiyotlar|REFERENCES|References|СПИСОК ЛИТЕРАТУРЫ|Литература)\b[^\n]*\n")

if __name__ == "__main__":
    thr = uzai._model()[3]
    for pdf in sys.argv[1:]:
        text = subprocess.run(["pdftotext", pdf, "-"], capture_output=True, text=True).stdout
        passages = [c for part in REFS.split(text) for c in chunks(part)]     # each article's references dropped
        s = np.array([uzai.score(p) for p in passages])
        print(f"\n{Path(pdf).name}: {len(passages)} Uzbek passages")
        if not len(s):
            continue
        print(f"  flagged at the website threshold ({thr:.3f}): {int((s > thr).sum())} ({np.mean(s > thr):.1%}); "
              f"median score {np.median(s):.3f}")
        for i in np.argsort(-s)[:5]:
            print(f"   {s[i]:.3f} | {passages[i][:120]}")
