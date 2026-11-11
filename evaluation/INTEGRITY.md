# Writing-integrity indicators: what they are and how often they fire on genuine text

PlagIQ does **not** estimate the probability that a text was written by AI. No published method does this reliably. Detectors misjudge non-native English writers, and none has been validated for Uzbek. Instead, `integrity.py` reports **indicators**: concrete, checkable places in the document that deserve a person's attention.

| Indicator | Fires when | Why it matters |
|---|---|---|
| Chatbot phrases | ≥1 phrase such as "As an AI language model", "Certainly! Here is…", "Конечно! Вот…", "Albatta! Mana…" (EN/RU/UZ, Latin and Cyrillic) | text pasted from a chatbot without editing |
| Placeholders | ≥1 slot such as "[Insert citation]", "[Your name]", "(Author, Year)" | unfinished template or tool output |
| Hidden characters | ≥10 zero-width, bidirectional-control or tag characters | inserted by "humanizer" tools to defeat checkers |
| Look-alike letters | ≥5 words mostly in one alphabet with look-alike letters from the other (e.g. Latin "P⟨а⟩per" with Cyrillic а) | letter swapping to defeat checkers |
| Chat formatting | ≥3 Markdown leftovers (`## `, `**bold**`, fences) | output copied from a chat window |
| DOI does not exist | a reference-list DOI unknown to OpenAlex **and** to doi.org | invented references |
| DOI belongs to another work | the DOI's registered title shares fewer than half of its words with the cited entry (compared only when both are in the same alphabet) | real DOI attached to an invented reference |

## False-positive measurement (November 2026)

The local indicators were run on the first 300 Wikipedia articles of at least 300 words in each language, read from the official dumps (2.1 million words in total). Wikipedia is human-edited text, though not academic writing.

| Language | Articles | Words | Articles flagged | What was flagged |
|---|---|---|---|---|
| Uzbek | 300 | 565,211 | 2 | "Toshkent": 42 words with Cyrillic о/а in Latin text (a genuine encoding problem); "Oʻsh": a "[manbaa]" (citation needed) gap |
| Russian | 300 | 1,071,298 | 0 | — |
| English | 300 | 1,420,589 | 1 | "Affidavit": the article quotes a form with "[name …]" and "[date]" slots |

The thresholds were set from this run. In a first pass with lower thresholds, stray zero-width spaces (3 Uzbek articles, 4 each) and keyboard-layout slips or transliterated letters (ӯ, ь) in 12 articles were flagged; letters that are not look-alikes are now ignored. Isolated slips stay below the thresholds; disguise tools insert far more.

**Not measured:** the share of AI-written or "humanized" texts these indicators catch. A careful writer who edits chatbot output leaves none of these traces, so **"No indicators found" never means "human-written"**.

## DOI check

Only the DOIs are sent, and only with the academic-mode consent. Each DOI is first looked up in OpenAlex as a single work (free of charge). DOIs that OpenAlex does not know are then checked with doi.org's handle service, which covers all registration agencies. At most 25 DOIs are checked per document, with a 12-second limit. Lookups that fail are reported as "could not be checked", never as missing. Live check (OpenAlex, November 2026): real DOIs were confirmed, and a real *Cell* DOI attached to an invented remittance study was reported as belonging to a different work.
