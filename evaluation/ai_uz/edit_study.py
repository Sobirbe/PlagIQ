"""How the live Uzbek AI indicator scores AI-edited text (prompts-edit-uz.txt, answers in edit_<bot>.json).

For each request kind (polish, paraphrase, expand, shorten, correct) and chatbot: how many edited passages score above
the live model's threshold, next to the human originals they were made from (human_full_uz.json), and how much of the
original wording each edit keeps (share of the original's word 3-grams still present), which shows how far each kind
moves the text from the human writing.

    python3 edit_study.py
"""
import json, sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[1]))
import uzai                                                       # noqa: E402

BOTS = ("chatgpt", "gemini", "claude")
KINDS = ("polish", "paraphrase", "expand", "shorten", "correct")


def pairs():
    """(kind, bot, original, edited) for every answer."""
    works = {w["id"]: w for w in json.load(open(HERE / "human_full_uz.json"))}
    plan = json.load(open(HERE / "edit_plan.json"))["f"]
    for bot in BOTS:
        answers = json.load(open(HERE / f"edit_{bot}.json"))
        for x in plan:
            edited = (answers.get(x["block"]) or [None] * 3)[x["n"] - 1]
            if edited:
                yield x["kind"], bot, works[x["id"]]["chunks"][x["chunk"]], edited, x


def kept(original, edited):
    grams = lambda t: {tuple(w) for w in zip(*(uzai.clean(t).lower().split()[i:] for i in range(3)))}
    a = grams(original)
    return len(a & grams(edited)) / max(1, len(a))


if __name__ == "__main__":
    thr = uzai._model()[3]
    print(f"live model threshold {thr:.3f}\n")
    flagged, total, keep, orig_flag, orig_n = defaultdict(int), defaultdict(int), defaultdict(list), 0, set()
    for kind, bot, original, edited, x in pairs():
        total[kind, bot] += 1; flagged[kind, bot] += uzai.score(uzai.clean(edited)) > thr
        keep[kind].append(kept(original, edited))
        if (x["id"], x["chunk"]) not in orig_n:
            orig_n.add((x["id"], x["chunk"])); orig_flag += uzai.score(uzai.clean(original)) > thr
    print(f"{'request':11s} " + "  ".join(f"{b:>9s}" for b in BOTS) + "   wording kept")
    for kind in KINDS:
        cells = "  ".join(f"{flagged[kind, b]:>4d}/{total[kind, b]:<4d}" for b in BOTS)
        print(f"{kind:11s} {cells}   {sum(keep[kind]) / len(keep[kind]):.0%}")
    print(f"\nhuman originals flagged: {orig_flag}/{len(orig_n)}")
