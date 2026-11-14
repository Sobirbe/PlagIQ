"""Measure OCR on scanned pages: word accuracy, and whether copied passages are still found after OCR.

Real academic abstracts (OpenAlex; Uzbek Latin, Uzbek Cyrillic made by transliteration, Russian,
English) are typeset on an A4 page, turned into a picture as a scanner would, and read back through
PlagIQ's own extract_text(ocr=True). Two conditions:
  clean - 200 dpi;
  poor  - 150 dpi, tilted 0.8 degrees, grey noise, JPEG quality 45 (an office scanner or a phone);
  rough - 120 dpi, tilted 2 degrees, heavy noise and blur, JPEG quality 30 (a poor phone photo).
Per script and condition: word accuracy (1 - word error rate), the share of the page's words that
PlagIQ's seven-word matching still finds against the original (copying detectable after OCR), and
seconds per page.

Usage: python evaluation/ocr_eval.py --abstracts academic.json --per-script 15 --out evaluation/OCR_RESULTS.json
       [--conditions clean,poor,rough]
"""
import argparse
import difflib
import io
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"


def scanned_pdf(text, condition, rng):
    import pymupdf
    from PIL import Image, ImageFilter
    source = pymupdf.open()
    page = source.new_page()
    page.insert_textbox(pymupdf.Rect(60, 60, 535, 790), text, fontfile=FONT, fontname="serif", fontsize=11.5)
    dpi = {"clean": 200, "poor": 150, "rough": 120}[condition]
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    if condition in ("poor", "rough"):
        rough = condition == "rough"
        image = image.rotate(2.0 if rough else 0.8, fillcolor=255, resample=Image.BICUBIC)
        noise = Image.effect_noise(image.size, 40 if rough else 28)
        image = Image.blend(image, noise, 0.2 if rough else 0.12).filter(ImageFilter.GaussianBlur(0.8 if rough else 0.5))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality={"clean": 95, "poor": 45, "rough": 30}[condition])
    out = pymupdf.open()
    img = out.new_page(width=page.rect.width, height=page.rect.height)
    img.insert_image(img.rect, stream=buffer.getvalue())
    return out.tobytes()


def word_accuracy(reference, hypothesis, language):
    import corpus
    ref = [t for t, _, _ in corpus.lexical(reference, language)]
    hyp = [t for t, _, _ in corpus.lexical(hypothesis, language)]
    matcher = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    errors = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")
    return max(0.0, 1.0 - errors / max(1, len(ref)))


def detectable(original, ocr_text, language):
    """Share of the OCR text's words inside a verified seven-word match with the original."""
    import corpus
    q, evidence, covered, intervals = corpus.compare_texts(ocr_text, [original], language)
    return (corpus.words_in_intervals(q, corpus.merge(intervals)) / max(1, len(q))) if q else 0.0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--abstracts", required=True)
    parser.add_argument("--per-script", type=int, default=15)
    parser.add_argument("--out", required=True)
    parser.add_argument("--conditions", default="clean,poor")
    args = parser.parse_args()
    import extract
    from real_eval import uz_latin_to_cyrillic
    data = json.loads(Path(args.abstracts).read_text("utf-8"))
    rng = random.Random(21)
    sets = {}
    for lang in ("uz", "ru", "en"):
        pool = [a["text"] for a in data[lang] if len(a["text"].split()) >= 120]
        rng.shuffle(pool)
        sets[lang] = pool[: args.per_script]
    sets["uz_cyrillic"] = [uz_latin_to_cyrillic(t) for t in sets["uz"]]
    results = []
    for script, texts in sets.items():
        language = "uz" if script.startswith("uz") else script
        for condition in args.conditions.split(","):
            accuracy, found, seconds, models = [], [], [], {}
            for text in texts:
                pdf = scanned_pdf(text, condition, rng)
                info, started = {}, time.monotonic()
                ocr_text = extract.extract_text("scan.pdf", pdf, ocr=True, info=info)
                seconds.append(time.monotonic() - started)
                models[info.get("ocr_models", "")] = models.get(info.get("ocr_models", ""), 0) + 1
                accuracy.append(word_accuracy(text, ocr_text, language))
                found.append(detectable(text, ocr_text, language))
            row = {"script": script, "condition": condition, "pages": len(texts),
                   "word_accuracy": round(sum(accuracy) / len(accuracy), 4), "worst_page_accuracy": round(min(accuracy), 4),
                   "copying_found": round(sum(found) / len(found), 4), "seconds_per_page": round(sum(seconds) / len(seconds), 2),
                   "models_chosen": models}
            print(json.dumps(row, ensure_ascii=False), flush=True)
            results.append(row)
            Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
