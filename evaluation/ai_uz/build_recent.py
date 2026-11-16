"""Build human_recent_uz.json — Uzbek passages of journal issues that the owner confirmed as human-written and has
permission to use for training — from PDF folders.

Each folder is one source (a journal); each PDF one issue; each issue is split into articles at their reference lists
(which are dropped). Only passages PlagIQ reads as Uzbek are kept (Cyrillic Uzbek included); passages seen in an earlier
file (the same issue twice) are skipped. The output, like the PDFs, stays out of the repository.

    python3 build_recent.py "Journal name=path/to/folder" [...]
"""
import json, re, subprocess, sys
from pathlib import Path
from fetch_fulltext import chunks
from score_pdfs import REFS

if __name__ == "__main__":
    out, seen = [], set()
    for arg in sys.argv[1:]:
        source, folder = arg.split("=", 1)
        for pdf in sorted(Path(folder).glob("*.pdf")):
            text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True).stdout
            kept = 0
            for i, part in enumerate(REFS.split(text)):
                ch = [c for c in chunks(part) if c not in seen]
                seen.update(ch)
                if ch:
                    out.append({"id": f"{source}|{pdf.stem}|{i}", "source": source, "issue": pdf.stem, "chunks": ch})
                    kept += len(ch)
            print(f"{source} | {pdf.name}: {kept} Uzbek passages", flush=True)
    json.dump(out, open(Path(__file__).with_name("human_recent_uz.json"), "w"), ensure_ascii=False)
    print(len(out), "article groups,", sum(len(d["chunks"]) for d in out), "passages")
