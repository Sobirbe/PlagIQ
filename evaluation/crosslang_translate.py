"""Step 1 of the academic cross-language evaluation: machine-translate real academic abstracts.

Students who copy across languages usually run a text through machine translation and then edit it,
so the test material is produced the same way. Translations are made sentence by sentence with
NLLB-200 (facebook/nllb-200-distilled-600M; CC BY-NC 4.0: used here only for measurement, never in
the product) and cached, so step 2 (crosslang_academic.py) can be re-run without translating again.

Directions (source -> manuscript language): ru->uz, en->uz, uz->ru, en->ru. Uzbek Cyrillic is made
from the Uzbek Latin output by transliteration in step 2.

Usage: python evaluation/crosslang_translate.py --abstracts academic.json --out translations.json [--per-direction 300]
"""
import argparse
import json
import random
import re
import time
from pathlib import Path

CODES = {"uz": "uzn_Latn", "ru": "rus_Cyrl", "en": "eng_Latn"}
DIRECTIONS = (("ru", "uz"), ("en", "uz"), ("uz", "ru"), ("en", "ru"))
SENT = re.compile(r"[^.!?]+[.!?]?")


def sentences(text, limit=6):
    parts = [p.strip() for p in SENT.findall(text) if len(p.split()) >= 4]
    return parts[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--abstracts", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-direction", type=int, default=300, help="abstracts per direction (half go into the library)")
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    name = "facebook/nllb-200-distilled-600M"
    tokenizer = AutoTokenizer.from_pretrained(name)
    model = AutoModelForSeq2SeqLM.from_pretrained(name)
    data = json.loads(Path(args.abstracts).read_text("utf-8"))
    out = json.loads(Path(args.out).read_text("utf-8")) if Path(args.out).exists() else {}
    rng = random.Random(args.seed)
    used = {lang: set() for lang in CODES}                 # an abstract is used in one direction only
    for source, target in DIRECTIONS:
        key = f"{source}->{target}"
        pool = [a for a in data[source] if a["id"] not in used[source] and len(sentences(a["text"])) >= 3]
        rng.shuffle(pool)
        chosen = pool[: args.per_direction]
        used[source].update(a["id"] for a in chosen)
        done = {item["id"] for item in out.get(key, [])}
        items = out.setdefault(key, [])
        tokenizer.src_lang = CODES[source]
        started = time.monotonic()
        for n, abstract in enumerate(chosen):
            if abstract["id"] in done:
                continue
            src = sentences(abstract["text"])
            batch = tokenizer(src, return_tensors="pt", padding=True, truncation=True, max_length=200)
            generated = model.generate(**batch, forced_bos_token_id=tokenizer.convert_tokens_to_ids(CODES[target]),
                                       max_new_tokens=220, num_beams=2)
            mt = tokenizer.batch_decode(generated, skip_special_tokens=True)
            items.append({"id": abstract["id"], "title": abstract["title"], "source": src, "translation": mt,
                          "in_library": n % 2 == 0})
            if n % 20 == 19:
                Path(args.out).write_text(json.dumps(out, ensure_ascii=False), "utf-8")
                print(f"{key}: {n + 1}/{len(chosen)} ({time.monotonic() - started:.0f} s)", flush=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False), "utf-8")
        print(f"{key}: done, {len(items)} abstracts", flush=True)


if __name__ == "__main__":
    main()
