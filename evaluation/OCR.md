# OCR on scanned pages: measured accuracy

**What was measured.** 15 real academic abstracts per script, taken from OpenAlex (Uzbek Cyrillic was made by transliterating the Uzbek Latin ones). Each was typeset on an A4 page and turned into a picture-only PDF, as a scanner would produce. PlagIQ then read it back with its own `extract.extract_text(ocr=True)`: the same code and Tesseract models as the website. Script: `evaluation/ocr_eval.py`; raw numbers: `OCR_RESULTS.json`.

**The three conditions:**
- **clean**: 200 dpi.
- **poor**: 150 dpi, tilted 0.8°, light noise, JPEG quality 45; an office scanner.
- **rough**: 120 dpi, tilted 2°, heavy noise and blur, JPEG quality 30; a poor phone photo.

**The columns:**
- **Word accuracy**: 1 − word error rate against the original, using the same word normalisation as matching.
- **Copying found**: the share of the page's words that PlagIQ's seven-word matching still finds when the OCR text is compared with the original. For a copied page this is what the score would show.

| Script | Condition | Pages | Word accuracy | Worst page | Copying found | Seconds / page |
|---|---|---|---|---|---|---|
| Uzbek (Latin) | clean | 15 | 99.8% | 98.6% | 99.8% | 6.5 |
| Russian | clean | 15 | 98.7% | 96.0% | 98.4% | 3.4 |
| English | clean | 15 | 100.0% | 99.2% | 100.0% | 3.9 |
| Uzbek (Cyrillic) | clean | 15 | 99.4% | 96.4% | 98.8% | 3.4 |
| Uzbek (Latin) | poor | 15 | 99.5% | 97.6% | 99.2% | 5.4 |
| Russian | poor | 15 | 98.2% | 95.4% | 97.9% | 3.0 |
| English | poor | 15 | 99.5% | 96.8% | 99.6% | 2.9 |
| Uzbek (Cyrillic) | poor | 15 | 99.5% | 96.4% | 99.1% | 3.1 |
| Uzbek (Latin) | rough | 15 | 91.2% | 83.3% | 85.6% | 4.8 |
| Russian | rough | 15 | 97.4% | 94.8% | 96.1% | 2.9 |
| English | rough | 15 | 96.1% | 92.3% | 95.3% | 2.4 |
| Uzbek (Cyrillic) | rough | 15 | 94.8% | 90.8% | 90.7% | 2.9 |

## What this means
- On clean and office-quality scans, a fully copied page is still found at **97.9–100%** in all four scripts. The models were chosen correctly for every page.
- On rough phone photos detection drops, most in Uzbek Latin (**86%**): misread ʻ/ʼ letters and noise break seven-word sequences. OCR errors can only **hide** matches, never invent them, so a bad scan under-counts copying but never over-counts it.
- The times were measured in the development sandbox and may differ on Railway. That is why one upload is OCR'd at a time and the page and time limits are adjustable (DEPLOYMENT.md, Part 3q).

## Limits of this test
- The pages are synthetic: typeset abstracts made into pictures, one font, one column. Real scans of tables, formulas, two-column layouts, handwriting or stamps were **not** tested and will do worse.
- 15 pages per script and condition. Treat differences under about one percentage point as noise.
