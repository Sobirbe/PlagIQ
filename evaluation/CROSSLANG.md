# Cross-language reuse: measurement before building (November 2026)

**Question:** if an Uzbek manuscript contains a translation of a Russian or English text that is in PlagIQ's library, can a multilingual model running on PlagIQ's own engine find the source, without wrongly flagging Uzbek texts that are not translations?

**Status:** this was measurement only. Nothing here is used by the website or the engine yet.

Script: `evaluation/crosslang_eval.py`. Raw figures: `evaluation/CROSSLANG.json`.

## Test data
- **FLORES-101** (CC BY-SA 4.0): 2,009 sentences professionally translated into Uzbek (Latin), Russian and English, grouped into 561 short passages (2–6 sentences) by source article. The texts are news and encyclopedia articles, not academic papers.
- **Real academic abstracts from OpenAlex:** 158 Uzbek and 323 Russian, all in pedagogy, economics, agriculture, medicine, law, history, linguistics, IT, ecology, psychology or literature. Each abstract's language was confirmed by PlagIQ's own detector; 59 Uzbek and 52 Russian abstracts had wrong OpenAlex labels and were dropped.
- **Library:** the Russian (or English) passages of half of the articles, chosen at random with seed 7, plus the 323 Russian abstracts as same-field distractors.
- **What should be found:**
  - the Uzbek translations of those 280 passages;
  - "half-translated" passages: the first half translated from a library source, the second half from an article that is not in the library.
- **What must not be flagged:**
  - the Uzbek passages of the other 281 articles, whose source is absent;
  - the 158 real Uzbek academic abstracts, which are on the same subjects as the Russian ones but are not translations.

## Results

### 1. Model choice (passage similarity only)
| Model | Size | Source ranked first, Uzbek→Russian / →English | Half-translated, source ranked first | Median score: translation vs unrelated Uzbek abstract |
|---|---|---|---|---|
| **LaBSE** | 1.8 GB | **100% / 100%** | **94% / 93%** | **0.91 vs 0.70** (RU), 0.91 vs 0.49 (EN) |
| multilingual-e5-small | 0.47 GB | 100% / 100% | 76% / 80% | 0.90 vs 0.86: too close to separate |
| paraphrase-multilingual-MiniLM-L12-v2 | 0.47 GB | 40% / 54% | 10% / 19% | no usable gap: does not cover Uzbek well |

### 2. Same subject is not the same text
A whole-passage score alone flags Uzbek abstracts that merely share a subject with a Russian one. With LaBSE at a cut-off of 0.75, it flagged **17.7%** of the Uzbek academic abstracts. Reading the highest-scoring cases confirmed they are all genuine false alarms: "Umumiy pedagogika" against "Педагогика духовно-нравственного воспитания", or Uzbek law against "Исламское право". None was a hidden translation.

### 3. Sentence-level search (LaBSE)
Each manuscript sentence is matched against the library's sentences. A passage is flagged only when **at least 2 of its sentences** each match a sentence of the **same** source with similarity ≥ the cut-off.

| Cut-off | Full translations found, RU / EN | Half-translated found, RU / EN | False alarms, Uzbek news / academic |
|---|---|---|---|
| 0.75 | 98.2% / 98.9% | 49.3% / 52.1% | 0% / **1.9%** (RU), 0% / 0% (EN) |
| **0.80 (recommended)** | **94.6% / 96.4%** | **43.9% / 47.1%** | **0% / 0%** |
| 0.85 | 78.9% / 84.3% | 30.4% / 31.8% | 0% / 0% |

Requiring 3 sentences instead of 2 halves the recall and gains nothing, because 2 sentences already gave no false alarms at 0.80.

**About the half-translated figures:** many FLORES passages have 3 sentences, so "half" is often a single translated sentence, which can never meet a 2-sentence rule. About 50% is therefore close to the ceiling in this test. In a real manuscript, a translated paragraph usually has more than one sentence.

By comparison, the passage-first approach (a passage score of 0.75, then the same 2-sentence check) found 94.6% of full translations but only 17.1% of half-translated passages. Sentence-level search is the better design.

### 4. Cost on the engine (LaBSE, measured on 4 CPU threads)
- **Model:** 1.8 GB on disk; about **1.2 GB of memory after loading**, about 1.4 GB while working; loads in about 1–2 seconds once cached, about 20 seconds on first download.
- **Speed:** about **78 sentences a second**. A 5,000-word article (about 250 sentences) takes about 3 s; a 100,000-word dissertation (about 5,000 sentences) about 65 s.
- **Library index:** every library sentence must be embedded once. For about 2,300 texts that is roughly 300,000–400,000 sentences, about 1–1.5 hours one-off, plus a few seconds per newly imported text. Storage at 768 numbers per sentence:
  - 8-bit quantised: about 300 MB;
  - 16-bit: about 600 MB;
  - either way on top of today's library, so **the engine volume must be enlarged first**.
- **Search:** comparing every manuscript sentence with every library sentence is too slow on a CPU. The design therefore needs two steps:
  1. a shortlist of the most similar library texts per chapter (one vector per text section, cheap);
  2. sentence-level comparison only within that shortlist.
  The shortlist step must itself be measured: it must not lose the sources that step 2 would find.

## What this shows, and what it does not
- **Shows:** on professional translations, LaBSE with sentence-level search finds about 95% of fully translated passages, in Uzbek→Russian and Uzbek→English, with **no false alarms** on 281 Uzbek news passages and 158 real Uzbek academic abstracts.
- **Does not show:**
  - **Academic manuscripts:** FLORES is news and encyclopedia text.
  - **Translated copying by students:** they use machine translation and edit the result, not professional translation.
  - **The other directions** (Russian→Uzbek, Uzbek Cyrillic, English→Russian): not tested yet.
  - **Scale:** 158 + 281 negatives can only show that the false-alarm rate is low, roughly under 1–2%, not that it is zero; a library 100 times larger gives more chances for coincidences.
  - **8-bit storage:** whether it keeps the same quality was not measured.

## Round 2: academic texts, machine translation with edits, all directions (November 2026)
Scripts: `evaluation/crosslang_translate.py` (step 1) and `evaluation/crosslang_academic.py` (step 2). Raw figures: `evaluation/CROSSLANG_ACADEMIC.json`.

**Data:**
- 1,487 Uzbek, 1,106 Russian and 2,020 English academic abstracts from OpenAlex, across 28 subjects. The English ones are all by authors at Uzbek institutions. Every abstract's language was confirmed by PlagIQ's detector.
- 200 abstracts per direction were machine-translated sentence by sentence with NLLB-200 (600M, CC BY-NC: used for measurement only, never in the product). This is how students typically copy across languages.
- Each abstract is used in one direction only.

**Per direction:**
- **Library:** the originals of 100 translated abstracts, plus 500 other real abstracts in the same language (600 in all).
- **Must be found:** the 100 translations; the same translations edited (about 15% of words dropped, two sentences swapped, two merged); and half-translated texts (half translation, half an unrelated real abstract).
- **Must not be flagged:** translations of the other 100 abstracts, whose source is not in the library; and 400 real abstracts written in the manuscript's language.
- Uzbek manuscripts were also tested in Cyrillic script.

**Rule:** at least 2 sentences each matching a sentence of the same library abstract with similarity ≥ 0.80. Model: LaBSE.

| Manuscript ← source | Plain machine translation | Edited translation | Half translated | False alarms: real abstracts (400) | Flagged: translations of texts not in the library (100) |
|---|---|---|---|---|---|
| Uzbek Latin ← Russian | **100%** | **98%** | 80% | **0%** | 1 (boilerplate, see below) |
| Uzbek Cyrillic ← Russian | 99% | 84% | 78% | **0%** | 0 |
| Uzbek Latin ← English | **100%** | 90% | 72% | 0.5% (2, at the cut-off) | 0 |
| Uzbek Cyrillic ← English | **100%** | 84% | 71% | 0.5% (2, at the cut-off) | 0 |
| Russian ← Uzbek | **100%** | **99%** | 74% | **0%** | 2 (real reuse, see below) |
| Russian ← English | **100%** | **97%** | 77% | **0%** | 3* (2 real reuse) |

\* At 0.80, 3 of 100 were flagged; the listing at the same setting showed 2, both genuine reuse. The third sits near the cut-off.

**The "flagged" translations of texts not in the library turned out to be genuine reuse.** Every case was read by hand:
- **Uzbek→Russian:** a thesis on project-based learning for linguistics students. The same thesis is in the library under a second OpenAlex record, with the same title and sentences at similarity 0.94–0.98. A tax-risk article: another paper's abstract repeats its sentences (0.91–0.96).
- **English→Russian:** two pairs of papers with different titles, and in one case different authors, whose abstracts share identical sentences ("Teaching physics at school helps to increase students' knowledge about existence…"; "The scientific article talks about culture as a continuous conscious creative activity…").
- **Russian→Uzbek:** two library catalogue records of conference proceedings from the same university, sharing their bibliographic template ("[Электронный ресурс] : сборник … Учреждение образования 'Гродненский государственный университет имени Янки Купалы' …"). This is boilerplate: in the product, sentences that match many library texts are ignored, as the exact matcher already does.

So, in about 1,700 real abstracts that should not be flagged, the only flags were real duplicates, real reuse, boilerplate, or 2 English→Uzbek cases right at the cut-off. Those 2 were flagged in the scoring run but not when re-checked one at a time, because tiny numerical differences decide a score of about 0.80.

**Cut-off:**
- **0.75** finds more edited text but flags more: up to 7% of the translations of texts not in the library in Uzbek→Russian.
- **0.85** loses many edited translations: 46% found in English→Uzbek.
- **0.80 stays the recommended setting.**

**Uzbek Cyrillic** is found as well as Latin for plain translations (99–100%), but edited translations drop to 84%. The model sees Uzbek Cyrillic less often in its training.

**Shortlist step:** searching sentences only within the 20 library texts closest to the manuscript as a whole loses nothing on plain and edited translations, and 0–3 points on half-translated texts. With 5 texts it loses up to 5 points. Use 20 or more, and re-measure on the full library: 600 texts here, against about 2,300 live and more later.

**Still not shown:**
- translations from other systems (Google, Yandex) and human paraphrase after translation;
- whole chapters rather than abstracts;
- a library of 2,000 to 10,000 texts;
- whether 8-bit storage keeps the same quality.

The data includes real duplicates, so library de-duplication will matter in production.

## Recommendation
1. **Model:** LaBSE on the engine; neither smaller model is good enough for Uzbek.
2. **Design:** sentence-level search with the 2-sentence, 0.80 rule.
3. **Presentation:** show the result **separately** from the exact-match percentage, as "possible translated reuse — needs review". Show each matched sentence pair side by side, and never add it to the plagiarism percentage.
4. **Before switching it on for users** (round 2 above covered academic text, machine translation with edits, the other directions, Uzbek Cyrillic and the shortlist), **measure:**
   - academic manuscripts: permission-cleared theses or articles with their own published translations, or library texts machine-translated for the test;
   - the other language directions;
   - the shortlist step;
   - 8-bit storage;
   - a larger set of Uzbek negatives.
5. **Infrastructure:** about 2 GB more memory for the engine, about 1–2 GB more disk for the model and the sentence index, and a one-off indexing run.

## Reproduce
```bash
pip install sentence-transformers pyarrow
# FLORES-101 Parquet: https://huggingface.co/api/datasets/gsarti/flores_101/parquet/{uzb,rus,eng}/{dev,devtest}/0.parquet
CROSSLANG_SENTENCE_FIRST=1 python evaluation/crosslang_eval.py --flores DIR --abstracts abstracts.json \
    --out evaluation/CROSSLANG.json --model sentence-transformers/LaBSE --model intfloat/multilingual-e5-small
```
`abstracts.json` holds OpenAlex abstracts (`{"uz": [{"id", "title", "text"}], "ru": [...]}`), filtered with `corpus.detect_language`.
