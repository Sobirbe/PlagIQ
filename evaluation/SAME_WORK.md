# Copies of one work and the boilerplate filter

**Problem (audit finding C1).** A seven-word sequence found in 3 or more compared texts is treated as boilerplate: journal headers, licence lines. Copies of *the same work* were counted as different texts, for example:
- a journal version, a preprint and a repository copy;
- the same article imported by two harvesters;
- an abstract and its full text.

So copying from a widely available paper was **dropped completely**.

**Fix.** `corpus.same_work` treats two texts as one work when they share at least 20 seven-word windows (about 26 words), and those windows are at least half of the shorter text. The filter now counts *different works*, both in a check's fetched texts (`compare_texts`) and in the library (`search`). A sequence held by more than 12 texts is still treated as boilerplate without the extra check.

**Measurement.** `evaluation/real_eval.py` (method in REAL_RESULTS.md). The library has 215 real texts; the held-out folder contains real articles that are not in it. Two libraries were used:
- **normal**: as is;
- **tripled**: every text stored three times (unchanged, with "Preprint version." in front, and with a repository note after), as happens when one paper comes from several sources.

Every cell reads *before / after* the fix.

| Lang | How it was copied | Recall, normal | False marks per 1,000, normal | **Recall, tripled** | Right source, tripled | False marks, tripled |
|---|---|---|---|---|---|---|
| en | copied unchanged | 99% / 99% | 11.6 / 11.6 | **0% / 97%** | 0% / 100% | 0.0 / 8.5 |
| en | letter case, punctuation, spacing and line breaks changed | 99% / 99% | 11.6 / 11.6 | **0% / 97%** | 0% / 100% | 0.0 / 8.5 |
| en | a third of a/e/o/p/c/x/y letters swapped for the other alphabet's look-alikes | 99% / 99% | 11.6 / 11.6 | **0% / 97%** | 0% / 100% | 0.0 / 8.5 |
| en | one word in 12 replaced | 86% / 86% | 11.6 / 11.6 | **0% / 84%** | 0% / 100% | 0.0 / 8.5 |
| en | one word in 4 replaced | 2% / 2% | 11.4 / 11.4 | **0% / 2%** | 0% / 100% | 0.0 / 8.4 |
| en | sentences in a different order | 96% / 96% | 11.5 / 11.5 | **0% / 96%** | 0% / 100% | 0.0 / 8.4 |
| ru | copied unchanged | 99% / 99% | 0.8 / 0.8 | **0% / 99%** | 0% / 100% | 0.0 / 0.8 |
| ru | letter case, punctuation, spacing and line breaks changed | 99% / 99% | 0.8 / 0.8 | **0% / 98%** | 0% / 100% | 0.0 / 0.8 |
| ru | a third of a/e/o/p/c/x/y letters swapped for the other alphabet's look-alikes | 99% / 99% | 0.8 / 0.8 | **0% / 99%** | 0% / 100% | 0.0 / 0.8 |
| ru | one word in 12 replaced | 86% / 86% | 0.8 / 0.8 | **0% / 88%** | 0% / 100% | 0.0 / 0.8 |
| ru | one word in 4 replaced | 1% / 1% | 0.8 / 0.8 | **0% / 2%** | 0% / 100% | 0.0 / 0.8 |
| ru | sentences in a different order | 93% / 93% | 0.8 / 0.8 | **0% / 89%** | 0% / 100% | 0.0 / 0.8 |
| uz | copied unchanged | 100% / 100% | 23.1 / 23.1 | **0% / 100%** | 0% / 100% | 0.0 / 14.5 |
| uz | letter case, punctuation, spacing and line breaks changed | 100% / 100% | 23.1 / 23.1 | **0% / 100%** | 0% / 100% | 0.0 / 14.5 |
| uz | Uzbek only: Latin rewritten in Cyrillic or the reverse | 97% / 97% | 23.1 / 23.1 | **0% / 96%** | 0% / 100% | 0.0 / 14.5 |
| uz | a third of a/e/o/p/c/x/y letters swapped for the other alphabet's look-alikes | 100% / 100% | 23.1 / 23.1 | **0% / 100%** | 0% / 100% | 0.0 / 14.5 |
| uz | one word in 12 replaced | 88% / 88% | 23.1 / 23.1 | **0% / 88%** | 0% / 100% | 0.0 / 14.5 |
| uz | one word in 4 replaced | 0% / 0% | 23.1 / 23.1 | **0% / 1%** | 0% / 100% | 0.0 / 14.5 |
| uz | sentences in a different order | 91% / 91% | 23.1 / 23.1 | **0% / 94%** | 0% / 100% | 0.0 / 14.5 |

Whole held-out articles (texts not in the library), highest match per language:
- **Normal library:** unchanged by the fix (en 2.15%, ru 1.38%, uz 16.05%).
- **Tripled library:** 0% everywhere before the fix, because every shared passage, even a real one, was dropped. After the fix the values are the same as in the normal library.

The tripled library sampled 28 Uzbek cases instead of 30, because the larger library changes the random choice.

**Speed** (`corpus.search`, 20 Uzbek documents of 750 words, sandbox):

| | Normal library | Tripled library |
|---|---|---|
| Before | 0.062 s median (0.142 s max) | 0.014 s (0.018 s) |
| After | 0.055 s median (0.180 s max) | 0.463 s (1.033 s) |

The tripled library was faster before only because every match was thrown away. Afterwards, the copies are compared with each other (their fingerprints are computed once and cached).

**Limit.** A work with more than 12 copies among the texts holding a sequence is still treated as boilerplate.
