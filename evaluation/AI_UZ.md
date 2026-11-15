# Uzbek AI-written text: measurement before building (November 2026)

**Question:** can PlagIQ tell Uzbek academic text written or polished by an AI chatbot from human-written Uzbek text, with
very few false alarms on human writing? No detector has been shown to work on Uzbek (Pangram does not support it).
**Status:** measurement only. Nothing here is used by the website or the engine.

## Data (folder `evaluation/ai_uz/`)
- **Human:** 493 Uzbek academic abstracts published 2010–2021 (before ChatGPT), from OpenAlex, language confirmed by
  PlagIQ's detector, including Uzbek works OpenAlex labels Russian or English (`fetch_human.py`, which re-creates
  `human_uz.json`; the texts are other authors' and are not kept in the repository).
- **AI-written:** 20 Uzbek abstracts written by Claude (an Anthropic model) for the titles of 20 human abstracts
  (`ai_batch1.json`, same topics, so topic cannot give the answer away).
- **AI-polished:** 16 human abstracts rewritten by Claude "in better academic style" (`ai_polished1.json`).
- **Planned:** the same tasks done by ChatGPT (and Gemini) from `chatgpt-prompts.txt`; other genres than abstracts.

## First feasibility check (`baseline.py`)
Text is put into one script (Cyrillic → Latin), apostrophes unified and spacing/quotation quirks removed first, and
Uzbek abstracts that include a Russian version are left out, so the classifier cannot use alphabet or formatting.
Character n-grams + logistic regression, 5-fold cross-validation; polished texts and their human originals are only
test data.

| Threshold set for false alarms on human abstracts | AI-written caught | AI-polished caught | Human originals of the polished texts flagged |
|---|---|---|---|
| 1 % | 45 % | 19 % | 0 % |
| 5 % | 100 % | 38 % | 6 % |

AI-written abstracts use stock phrases far more (e.g. "…tahlil qilinadi" in 90 % vs 18 % of human abstracts,
"…ko‘rib chiqilgan" 85 % vs 11 %) and shorter, more uniform sentences.

**What this does not show:** 36 AI texts from one model are far too few; a classifier may learn one model's habits
rather than "AI"; human OpenAlex abstracts carry their own quirks (typos, run-on sentences) that student documents may
not; abstracts are one short, formulaic genre. Polished text is clearly the hard case. No figure here may be used to
describe a product.
