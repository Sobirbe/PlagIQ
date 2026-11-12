# Discovery: more excerpts and topic relevance (November 2026)

## Why
In a real academic check (8,361-word English paper on market pricing), PlagIQ sent OpenAlex only **3 excerpts of 14 words**. Generic words such as "case study" and "dimensions" then returned unrelated papers (supergravity, quasicrystals, arm movement). **6 of the 8 full-text downloads** went to those papers, and they filled the source list.

## What changed
- **Excerpts:** 3 for documents up to 3,000 words, then one more per further 3,000 words, at most `OPENALEX_MAX_QUERIES` (default 8). They are spread over the whole document, as in web search.
- **Relevance of each result:** the share of its distinctive title and abstract words that also occur in the document.
  - Words have at least 5 letters, with common and generic academic words removed. The first 6 letters are compared, so inflected forms match.
  - Uzbek Latin and Cyrillic are folded to one form.
  - If at least half of the title's distinctive words (at least two) occur in the document, the title score counts. This covers records with a garbled abstract, which do occur in OpenAlex.
  - Records with fewer than 4 distinctive words get no score and are always kept.
- **Off-topic:** a relevance below `MIN_RELEVANCE = 0.3`.
  - Off-topic results are **not downloaded**; full-text slots go to the most relevant results first.
  - They are still compared by their abstract. If they match nothing, they are left out of the source list, but their number is stated on the page, in the PDF ("Off-topic results") and in the certificate and QR counts.
  - **A result with any matching words is always listed, and the percentage never changes.**

## Calibration (`evaluation/discovery_relevance.py`)
Eight topics: 4 English, 2 Russian, 2 Uzbek (pricing to market, teacher professional development, antibiotic resistance, DevOps, inflation and monetary policy, the competence approach in higher education, the education system and teachers, agricultural economics). For each, a test document was built from 25 real OpenAlex abstracts and discovery was run on it: **224 results** (EN 125, RU 53, UZ 46). Results were judged on- or off-topic by reading their titles.

| Cut-off | Results below it | Of those, on-topic |
|---|---|---|
| 0.2 | 7 | 1 before the title rule ("Exchange Rate Exposure", whose OpenAlex abstract is garbled), 0 after |
| **0.3 (chosen)** | **9** | **0** |
| 0.4 | 12 | 0, but approaching borderline cases |

The 9 results below 0.3 are, for example, uterine bleeding and enzyme toxicity for the teacher-training document, Baikal cormorants for the monetary-policy document, and the concept of the Volga in literature for the Uzbek education document.

The cut-off is deliberately **cautious**. Some off-topic results score above it, such as flour processing (0.47) for the antibiotic document, and stay listed. Only clearly unrelated results are filtered.

**Limits:** one calibration run, eight topics, titles judged by one reviewer, and synthetic documents made of abstracts. Real manuscripts have a narrower vocabulary than 25 concatenated abstracts, which makes off-topic results score *lower*, so the filter is if anything more cautious than measured. Recalibrate with real manuscripts when a permission-cleared set exists.
