"""Build the prompt file for the AI-EDITED part of the Uzbek AI-detector study (prompts-edit-uz.txt, edit_plan.json).

Real users rarely paste raw chatbot text: they ask a chatbot to edit, paraphrase, expand, shorten or correct their own
writing. The first study (blocks D of prompts-full-uz.txt, 12 passages per chatbot) was too small to learn that. Here
36 human passages, all published before ChatGPT (30 November 2022) under CC BY (human_full_uz.json, from
fetch_fulltext.py), go to each chatbot with five kinds of request:

  F1-F3   ilmiy uslubda tahrirlash (academic polish)
  F4-F6   qayta yozish, ma'nosini saqlab (paraphrase, as users do to pass a plagiarism check)
  F7-F8   kengaytirish (expand to about twice the length)
  F9-F10  qisqartirish (condense to about half)
  F11-F12 faqat imlo va grammatik xatolarni tuzatish (light correction; kept as its own category — such text may be
          too close to the human original to call AI-written)

Only works put online before ChatGPT count: Zenodo records by their upload date (zenodo_created.json, from the Zenodo
API), since 60 of the 139 Zenodo records in human_full_uz.json were uploaded after 30 November 2022. Each passage is one chunk (130-280 words), at most two far-apart passages per work, none used in prompts-full-uz.txt. The human originals
stay in edit_plan.json so the study can keep each original and its edits in the same cross-validation group.

    python3 edit_prompts.py      (needs human_full_uz.json, which is not in the repository)
"""
import json, random, re

BLOCKS = [
    ("F1", "polish"), ("F2", "polish"), ("F3", "polish"),
    ("F4", "paraphrase"), ("F5", "paraphrase"), ("F6", "paraphrase"),
    ("F7", "expand"), ("F8", "expand"), ("F9", "shorten"), ("F10", "shorten"),
    ("F11", "correct"), ("F12", "correct"),
]
ASK = {
    "polish": "Quyidagi har bir matn parchasini ilmiy uslubda tahrirlab, yaxshilab bering (mazmunini o‘zgartirmang, "
              "tilini ravon va akademik qiling).",
    "paraphrase": "Quyidagi har bir matn parchasini o‘z so‘zlaringiz bilan qayta yozing: ma’nosini to‘liq saqlang, "
                  "lekin jumlalar va so‘zlar asl matndan farq qilsin (antiplagiat tekshiruvidan o‘tishi uchun).",
    "expand": "Quyidagi har bir matn parchasini taxminan ikki barobar kengaytiring: fikrlarni chuqurroq ochib bering, "
              "ilmiy uslubni saqlang.",
    "shorten": "Quyidagi har bir matn parchasini taxminan ikki barobar qisqartiring: asosiy fikrlarni ilmiy uslubda "
               "saqlang.",
    "correct": "Quyidagi har bir matn parchasidagi faqat imlo, tinish belgilari va grammatik xatolarni tuzating. Boshqa "
               "hech narsani o‘zgartirmang.",
}
TAIL = (" Natijani o‘zbek tilida (lotin yozuvida), oddiy matn ko‘rinishida (sarlavhalarsiz, ro‘yxatlarsiz, izohsiz) bering. "
        "Har birini alohida xatboshida, oldiga raqamini qo‘yib yozing.")


def good(text):
    words = text.split()
    if not 130 <= len(words) <= 280: return False
    if sum(c.isdigit() for c in text) > 0.03 * len(text): return False          # tables, statistics, references
    if re.search(r"[А-Яа-яЁё]{4,}", text) or re.search(r"\b(et al|doi|https?:)", text, re.I): return False
    if not re.search(r"[‘’']", text) or text.count("“") + text.count("«") > 6: return False   # Uzbek Latin, few quotes
    if not text[:1].isupper() or re.search("[\u0600-\u06ff\u200e\u200f\u202a-\u202e\ufffd―]", text): return False
    if len(re.findall(r"\s\d{1,3}\s[A-Z]", text)) >= 2: return False                 # contents pages, tables
    if re.search(r"www\.|\bvolume\b|\bissue\b|\d-(rasm|jadval)|asosiy qism|•|\[\d", text, re.I): return False
    return text.rstrip()[-1:] in ".!?»”"


def pre_chatgpt(work, zenodo):
    """Published AND put online before 30 Nov 2022: Zenodo records are dated by upload (zenodo_created.json), since
    many uploaded later claim an earlier publication year."""
    m = re.search(r"zenodo\.org/records?/(\d+)", work.get("url") or "")
    return zenodo.get(m.group(1), "9999") < "2022-11-30" if m else "inscience.uz" in (work.get("url") or "")


def main():
    works = json.load(open("human_full_uz.json"))
    plan = json.load(open("full_plan.json"))
    used = {x["id"] for k in plan for x in plan[k]}
    zenodo = json.load(open("zenodo_created.json"))
    pool = []
    for w in works:
        if w["id"] in used or w.get("year", 9999) > 2021 or not pre_chatgpt(w, zenodo) or not str(w.get("license", "")).startswith("cc-by"): continue
        ok = [i for i, c in enumerate(w["chunks"]) if i > 0 and good(c)]          # skip the opening chunk (title, header)
        if ok: pool.append((w, ok))
    rnd = random.Random(20261116)
    rnd.shuffle(pool)
    need = 3 * len(BLOCKS)
    # at most two passages per work, far apart; first passes take one per work
    picks = [(w, ok[len(ok) // 3]) for w, ok in pool] + [(w, ok[-1]) for w, ok in pool if len(ok) > 1 and ok[-1] - ok[len(ok) // 3] > 2]
    if len(picks) < need: raise SystemExit(f"only {len(picks)} usable passages from {len(pool)} works, need {need}")
    picks = picks[:need]; rnd.shuffle(picks)
    out, items = [], []
    head = (
        "O‘zbek tilidagi matnlarni sun’iy intellekt yordamida tahrirlash — PlagIQ AI detektori tadqiqoti (o‘lchov ma’lumotlari)\n"
        "Prompts for PlagIQ's Uzbek AI-detector study, AI-EDITED part. Use the SAME blocks in three chatbots:\n"
        "ChatGPT, Gemini and Claude (claude.ai).\n\n"
        "  F1–F3   polish 3 real passages each (academic style)\n"
        "  F4–F6   paraphrase 3 real passages each (as people do to pass a plagiarism check)\n"
        "  F7–F8   expand 3 real passages each (about twice as long)\n"
        "  F9–F10  shorten 3 real passages each (about half)\n"
        "  F11–F12 correct only spelling and grammar in 3 real passages each\n\n"
        "All passages were published before ChatGPT (before 30 November 2022) under CC BY 4.0; sources are listed\n"
        "under each block.\n\n"
        "HOW TO USE: for each chatbot, open a NEW chat for each block, paste the block up to its \"Sources\" line, and copy the answer into one text\n"
        "file per chatbot, with the block name (F1, F2, ... F12) on its own line before each answer. Name the files e.g.\n"
        "\"chatgpt_edit.txt\", \"gemini_edit.txt\", \"claude_edit.txt\". Do not edit the answers and do not ask the chatbot to\n"
        "\"humanize\" them. If a chatbot stops early, type \"davom eting\" and copy the rest too.\n")
    out.append(head)
    k = 0
    for block, kind in BLOCKS:
        lines, srcs = [f"===== BLOCK {block} =====", ASK[kind] + TAIL], []
        for n in (1, 2, 3):
            w, i = picks[k]; k += 1
            text = " ".join(w["chunks"][i].split())
            lines.append(f"{n}. {text}")
            srcs.append(f"{n}. {w['title']} ({w['year']}), {w['license'].upper()}, {w.get('url') or w['id']}")
            items.append({"block": block, "n": n, "kind": kind, "id": w["id"], "chunk": i, "words": len(text.split())})
        out.append("\n".join(lines) + "\n----- Sources (do not paste) -----\n" + "\n".join(srcs) + "\n")
    open("prompts-edit-uz.txt", "w", encoding="utf-8").write("\n".join(out))
    json.dump({"f": items}, open("edit_plan.json", "w"), ensure_ascii=False, indent=1)
    print(f"{len(items)} passages, {sum(x['words'] for x in items)} words, from {len(pool)} usable works")


if __name__ == "__main__":
    main()
