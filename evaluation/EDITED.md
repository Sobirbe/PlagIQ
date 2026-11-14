# Edited copying (near-duplicate passages)

**What it finds.** Copied text where words were replaced, inserted or deleted, so no seven identical words remain in a row. Before this change, replacing one word in four hid copying completely.

**How** (`corpus.edited_runs`, `corpus._seed_candidates`):
1. **Anchors.** Three-word phrases shared by the document and one source. After light editing, short runs of identical words survive.
2. **Regions.** Anchors that follow one another (at most 8 words apart) along nearly the same alignment (at most 3 words inserted or deleted between them) are joined. A region needs at least 4 anchors.
3. **Verification.** The two passages are aligned word by word (`difflib.SequenceMatcher`). A region counts only if:
   - it is at least 20 words long;
   - at least 14 words, and at least 60% of the region, are identical;
   - at most 20% of it is boilerplate (template text the exact layer ignores);
   - it is real text, not numbers.
4. **Boilerplate.** An edited region found in 3 or more *different works* is ignored, as for exact matches.
5. **Finding candidates in the library.** The engine stores 1 in 4 three-word phrases of each text (chosen by hash) in a new `seeds` table. A library text sharing at least 2 sampled phrases with the document is a candidate (up to 30). Phrases held by more than 20 texts are ignored as too common. Every candidate is then verified against its full text, as above.

**Reporting.**
- Every passage has a `type`: `exact` or `edited`. Edited passages also have a `similarity`, the share of identical words.
- The result gives `edited_tokens`: matched words found only as edited copying. The page and the PDF say "Of which edited copying: N words", and label each edited passage "Edited copy · N% identical words".
- The headline percentage counts every verified match, exact or edited. Both are literal overlap with a source and are verified word by word. Meaning-only similarity (paraphrase, translation) is **not** included.

## Measured on real texts

`evaluation/real_eval.py`: real passages (40, 80 and 150 words) are taken from a 215-text library (en 15, ru 48, uz 152) and inserted into real articles that are not in the library. Each library search is checked the way the engine runs it. Values are *before → after*, with 30 cases per row.

| Lang | How the copy was edited | Recall | Right source | False marks per 1,000 words of not-copied text |
|---|---|---|---|---|
| en | copied unchanged | 99% → **99%** | 100% → 100% | 9.0 → 9.0 |
| en | letter case, punctuation, spacing and line breaks changed | 99% → **99%** | 100% → 100% | 9.0 → 9.0 |
| en | a third of a/e/o/p/c/x/y letters swapped for the other alphabet's look-alikes | 98% → **98%** | 100% → 100% | 9.0 → 9.0 |
| en | one word in 12 replaced | 85% → **92%** | 100% → 100% | 9.0 → 9.0 |
| en | one word in 4 replaced | 3% → **89%** | 100% → 100% | 9.0 → 9.0 |
| en | a word inserted after every 5th word | 1% → **82%** | 100% → 100% | 9.0 → 9.0 |
| en | every 5th word deleted | 1% → **84%** | 100% → 100% | 9.0 → 9.0 |
| en | one word in 3 replaced (beyond edited-copy detection) | 0% → **0%** | 0% → 0% | 9.0 → 9.0 |
| en | sentences in a different order | 95% → **95%** | 100% → 100% | 9.0 → 9.0 |
| ru | copied unchanged | 100% → **100%** | 100% → 100% | 2.4 → 2.4 |
| ru | letter case, punctuation, spacing and line breaks changed | 100% → **100%** | 100% → 100% | 2.4 → 2.4 |
| ru | a third of a/e/o/p/c/x/y letters swapped for the other alphabet's look-alikes | 100% → **100%** | 100% → 100% | 2.4 → 2.4 |
| ru | one word in 12 replaced | 87% → **97%** | 100% → 100% | 2.4 → 2.4 |
| ru | one word in 4 replaced | 2% → **83%** | 100% → 100% | 2.4 → 2.4 |
| ru | a word inserted after every 5th word | 5% → **98%** | 100% → 100% | 2.4 → 2.4 |
| ru | every 5th word deleted | 2% → **100%** | 100% → 100% | 2.4 → 2.5 |
| ru | one word in 3 replaced (beyond edited-copy detection) | 1% → **1%** | 100% → 100% | 2.4 → 2.4 |
| ru | sentences in a different order | 93% → **93%** | 100% → 100% | 2.4 → 2.4 |
| uz | copied unchanged | 100% → **100%** | 100% → 100% | 12.1 → 12.1 |
| uz | letter case, punctuation, spacing and line breaks changed | 100% → **100%** | 100% → 100% | 12.1 → 12.1 |
| uz | Uzbek only: Latin rewritten in Cyrillic or the reverse | 99% → **99%** | 100% → 100% | 12.1 → 12.1 |
| uz | a third of a/e/o/p/c/x/y letters swapped for the other alphabet's look-alikes | 100% → **100%** | 100% → 100% | 12.1 → 12.1 |
| uz | one word in 12 replaced | 88% → **97%** | 100% → 100% | 12.1 → 12.1 |
| uz | one word in 4 replaced | 0% → **98%** | 0% → 100% | 12.1 → 12.1 |
| uz | a word inserted after every 5th word | 9% → **99%** | 100% → 100% | 12.1 → 12.1 |
| uz | every 5th word deleted | 4% → **100%** | 100% → 100% | 12.1 → 12.1 |
| uz | one word in 3 replaced (beyond edited-copy detection) | 0% → **3%** | 100% → 100% | 12.1 → 12.1 |
| uz | sentences in a different order | 94% → **94%** | 100% → 100% | 12.1 → 12.1 |

Whole held-out articles (texts not in the library):

| Lang | Median | 90th percentile | Highest |
|---|---|---|---|
| en | 1.46% → 1.46% | 2.15% → 2.15% | 2.15% → 2.15% |
| ru | 0.00% → 0.00% | 1.13% → 1.13% | 1.38% → 1.38% |
| uz | 1.45% → 1.51% | 7.40% → 9.95% | 16.05% → 16.60% |

The Uzbek increase comes from Uzbek Wikipedia biographies that reuse lightly edited sentences from the "Oʻzbek adabiyoti" article. That is genuine reuse, not a false alarm. For example: "Usmon Nosir 1912 yilning 13 noyabrida Namangan shahrida tugʻildi…".

## Settings compared (in-memory prototype on the same library)

| Setting | Anchors | Region | Identical | One word in 4 replaced | False marks (en / ru / uz) |
|---|---|---|---|---|---|
| exact only | — | — | — | 1–2% | 11.4 / 1.5 / 27.3 |
| A | 3 | 15 words | 10 words, 50% | 94–98% | 11.4 / 1.5 / 28.2 |
| **B (chosen)** | 4 | 20 words | 14 words, 60% | 92–97% | 11.4 / 1.5 / 27.5 |
| C | 3, gap 10 | 15 words | 10 words, 45% | 96–98% | 12.3 / 1.5 / 28.2 |

Before the boilerplate guard, setting B raised English false marks from 6 to 27 per 1,000 words, all of it journal template text (headers, "Article history", licence lines). With the guard, false marks are back to the exact-only level. B finds about as much as A and C with the fewest extras.

## Speed and size (sandbox)

- **Library search** (20 Uzbek documents of 750 words): 0.073 s median (0.16 s max) before → 0.161 s median (0.78 s max) after.
- **In-memory comparison** (a 3,700-word document against 8 full texts of 26,000 words): 0.18 s → 0.22 s.
- **Index size:** sampled phrases add about 24% more rows than the existing seven-word index. The rows are smaller, so the database grows by roughly 15–20%.
- **Existing library:** filled in the background after the engine starts, at 50 texts per batch. Until it finishes, edited copies are found only in texts that already have seeds or that also match exactly.

## Limits
- **One word in three replaced** leaves no three-word phrase intact, so it is not found (0–3%).
- **Paraphrase and translation** are not detected (see CROSSLANG.md for the measured cross-language model, not deployed).
- **English recall** (82–89%) is lower than Russian and Uzbek in this run. Short 40-word passages sometimes end up just under the 20-word or 4-anchor minimums.
