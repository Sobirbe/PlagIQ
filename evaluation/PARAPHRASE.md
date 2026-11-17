# Same-language paraphrase: measurement before building (November 2026)

**Question.** A chatbot can rewrite a published Uzbek text so that no word sequence survives ("rewrite it to pass the
plagiarism check"). Does PlagIQ find the source? If not, can the multilingual sentence model PlagIQ already uses for
translated reuse (LaBSE, `crosslang.py`) find it within Uzbek, without flagging unrelated human text?

**Status:** measurement only. Nothing here is used by the website or the engine.

## Test data
- **Chatbot edits:** the 107 answers of ChatGPT, Gemini and Claude to `ai_uz/prompts-edit-uz.txt`. Each one edits a real
  pre-ChatGPT Uzbek passage under CC BY: polish 27, paraphrase "to pass a plagiarism check" 27, expand 18, shorten 18,
  spelling/grammar only 17.
- **Library:** the 33 whole works the passages came from, plus 300 other whole articles from the same OAK journals
  (`ai_uz/oak_full_uz.json`) as same-field distractors. That is 336 texts and 24,625 sentences.
- **Must not be flagged:** 500 passages of other articles from the same journals, not in the library.

## 1. Word matching (what PlagIQ does now)
Each edit is checked against **its own source only** (`app.compare`, the easiest case). The table gives the share of
the edit's words matched:

| request | median | ≥ 10 % matched |
|---|---|---|
| polish | 14 % | 16/27 |
| paraphrase | 0 % | **0/27** |
| expand | 4.6 % | 6/18 |
| shorten | 0 % | **0/18** |
| spelling/grammar | 94.6 % | 17/17 |

A chatbot paraphrase of a published Uzbek text passes PlagIQ's word matching completely.

## 2. Sentence meaning (LaBSE, `paraphrase_eval.py`)
The rule is the engine's cross-language rule applied within Uzbek: a library text is reported when at least 2 sentences
of the checked passage each match one of its sentences with similarity at or above the cut-off.

| cut-off | polish | paraphrase | expand | shorten | spelling | unrelated passages flagged |
|---|---|---|---|---|---|---|
| 0.75 | 27/27 | 24/27 | 17/18 | 13/18 | 17/17 | 43/500 |
| **0.80** | **26/27** | **20/27** | **16/18** | **7/18** | 17/17 | 35/500 |
| 0.85 | 23/27 | 13/27 | 10/18 | 3/18 | 17/17 | 32/500 |
| 0.90 | 18/27 | 8/27 | 7/18 | 2/18 | 17/17 | 31/500 |

**What the 35 "unrelated" passages flagged at 0.80 actually are** (each match was read):
- 28 share two or more identical sentences with a library article. The same article appears twice in the journal data
  (duplicate records). This is real duplication, and word matching finds it too.
- 3 are real reuse: an article republished with light edits, near-identical sentences, and a shared annotation.
- **4 are false alarms**, and all four come from template text, not content:
  - shared reference-list entries (the same textbooks cited), in 3 cases;
  - a keyword line («Таянч сўз ва иборалар: …»), in 1 case;
  - a journal footer, together with one of the reference cases.

So about **4 in 500 (0.8 %)** unrelated passages were flagged. Leaving reference lists, keyword lines and headers out
of the comparison would remove these four. No content sentence was matched to an unrelated text.

## Conclusion and limits
Sentence-meaning matching finds the source of most chatbot paraphrases that word matching misses entirely: at 0.80,
paraphrase 20/27, polish 26/27, expand 16/18. Shortened texts are harder (7/18). Limits:
- The test is small: 107 edits and 500 unrelated passages.
- The library has 336 texts; the real library is much larger, and false alarms grow with it. The cross-language
  module's shortlist step (20 candidate texts per manuscript part) would also apply here.
- The edits come from one style of prompt per request.

Before building, measure the rule on the full engine library with reference lists, keyword lines and headers left out.
It would be reported like translated reuse: as "possible paraphrased reuse — review", separate from the percentage.
The engine already loads the model when `CROSSLANG=1`, so no new model is needed.
