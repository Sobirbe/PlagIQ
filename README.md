# PlagIQ · multilingual academic comparison pilot (v0.5)

PlagIQ compares Uzbek Latin/Cyrillic, Russian and English manuscripts. It reports **observed overlap in a stated scope**, never a plagiarism verdict, AI-authorship score or guarantee of originality.

## Available comparisons

- **Reference:** exact normalized seven-token matches against your uploaded/pasted reference.
- **Academic discovery:** explicit consent sends at most 14 words to OpenAlex; compares returned abstracts only. Metadata records are not full-text coverage. Failure/no usable abstracts displays **Not evaluated**.
- **Private corpus:** the existing Noplag-style client now connects to an included, authenticated private backend (`engine_service.py`). This is PlagIQ's bounded exact-match implementation of the adapter protocol, not a bundled upstream Noplag distribution or its advanced algorithms. Seven-token SHA-256 fingerprints retrieve sources; original-character offsets support evidence and Uzbek script folding. Overlapping spans are merged before calculating character overlap. Tokens and characters are different measurement units.

**No academic collection is bundled or claimed.** The corpus starts empty; imports are limited to 1,000 sources by default (`CORPUS_MAX_SOURCES`; see measured resources in [evaluation/BENCHMARKS.md](evaluation/BENCHMARKS.md)). Rights-gated imports support all three languages, but operational coverage exists only after authorized full texts are supplied. Corpus counts are reported by language, not inferred from a publisher's size. Empty language coverage displays **Not evaluated**. Searching a populated corpus with no matches can return 0%; this does not establish originality. Source lists are bounded (25 browser / 15 PDF entries); truncation is disclosed and totals include all matches. Exact quotes and bibliography text can match and are **not excluded** (stated in every result and PDF); paraphrase matching and cross-language translation detection are not implemented.

## One matcher, one measure

Every mode (own reference, OpenAlex abstracts, private corpus) uses the same code in `corpus.py`:

1. **Normalize** without losing positions: Unicode NFC, invisible characters removed (soft hyphen, zero-width), lower case, Uzbek Latin↔Cyrillic folding with the tutuq belgisi kept inside words, Russian ё→е; PDF line-end hyphenation is repaired at extraction.
2. **Find** every run of seven or more identical consecutive words (64-bit fingerprints as an index only).
3. **Verify** each candidate word by word against the real source text — a fingerprint collision can never become a match — and join consecutive hits on the same alignment into passages; repeated phrases are reported once.
4. **Measure** the share of manuscript words inside at least one verified passage, each word counted once regardless of the number of sources. The private engine's character spans are converted to this word count by the website; no engine-supplied percentage is used.

The result is exact-copy evidence with character positions in both texts. It does not detect paraphrase, translation or edited copying (measured below).

## Citation-aware results (optional exclusions)

Every matched word is located by fixed, tested rules (`citations.py`): inside quotation marks («», “”, „“, "") with or without a reference marker within 80 characters ([3], [3, b. 45], (Karimov, 2019), (Иванов, 2018, с. 5)), inside the bibliography (last references heading in UZ Latin/Cyrillic, RU or EN after the first 30% of the text, ending at an appendix heading), or elsewhere. Nothing is excluded by default. Users may exclude quotations, the bibliography and/or matches shorter than 10–30 words; the page and PDF then show the percentage before and after and how many words each exclusion removed. The denominator never changes. The rules do not judge whether a citation is correct or adequate. Results: [evaluation/CITATIONS.md](evaluation/CITATIONS.md).

## Growing lawful full-text coverage

`harvest.py` collects open-licence articles from journals that publish an OAI-PMH feed (standard in Open Journal Systems, used by many Uzbek and Russian journals). It accepts only CC BY, CC BY-SA, CC0 or Public Domain Mark, sends NC/ND licences to human review, confirms the licence on the article page, downloads only the PDF the journal declares on its own host over HTTPS, respects robots.txt, and writes a manifest plus `review.csv`. It never indexes anything; the owner imports with `corpus.py`. Tested against a simulated journal only — no live journal was harvested during development.

`oa_import.py` stages open-licence full texts found through **OpenAlex** (filtered by `best_oa_location.license`, language, country, year or journal; PDFs from the publisher link, or OpenAlex's billed cache) or **CORE** (full text kept only if the DOI's licence is confirmed in OpenAlex), with an optional **Unpaywall** licence cross-check and a per-run OpenAlex cost cap. Like the harvester, it never indexes anything itself. It was tested against simulated services only, because the development network could not reach the real APIs.

## Accuracy evidence

`python evaluation/run_eval.py` regenerates [evaluation/RESULTS.md](evaluation/RESULTS.md): seeded synthetic cases with known copied locations in Uzbek Latin, Uzbek Cyrillic, Russian and English, covering script changes, Unicode forms, PDF hyphenation and deliberately unsupported edits. `pytest` runs a small version of it. These are algorithm checks on synthetic text, not accuracy claims for real manuscripts; real-world false-positive rates need licensed real test collections.

The uploaded stack document is background reading, not an instruction to install models or an independently verified benchmark. AI detection remains **Not performed**; additional models require license review and language-specific evaluation.

## Local use and tests

Python 3.12:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pytest -q
.venv/bin/uvicorn app:app --reload
```

Open http://localhost:8000. Use nonconfidential samples. Developer API documentation is disabled on served applications.

Browser regression uses isolated processes, synthetic UZ/RU/EN sources and a temporary database. It checks 320/390/768/1440px layouts, uploads, reference/corpus checks, consent reset, language switching, PDF downloads and deletion; no publisher or production endpoints are contacted:

```bash
.venv/bin/pip install -r requirements-dev.txt
# If Chromium is installed elsewhere, set CHROMIUM_PATH to its executable.
CHROMIUM_PATH=/usr/bin/chromium .venv/bin/python tests/browser_smoke.py
CHROMIUM_PATH=/usr/bin/chromium .venv/bin/python tests/browser_followup.py docs/screenshots
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for owner-friendly Railway instructions, private service setup and rollback. See [SECURITY.md](SECURITY.md) for retention and readiness limits. No payments were introduced or changed.

## Structure

- `app.py`, `index.html`: website, bounded uploads, translated error codes, multilingual workflow.
- `report_pdf.py`: localized (UZ/RU/EN) scope-limited evidence PDF.
- `engine_client.py`: internal-only authenticated adapter; validates paths, lengths and match spans.
- `engine_service.py`: private check/status/report API; never ingests manuscripts.
- `corpus.py`: owner-run local importer and SQLite fingerprint index.
- `textfold.py`: Uzbek script folding with original offsets.
- `discovery.py`: bounded OpenAlex metadata/abstract discovery.
- `citations.py`: quotation, reference-marker and bibliography detection; optional exclusions.
- `harvest.py`: owner-run OAI-PMH harvester for open-licence journal articles (staging only).
- `oa_import.py`: owner-run OpenAlex/CORE open-licence full-text staging with cost cap.
- `extract.py`: bounded TXT/DOCX/PDF text extraction shared by website and importer.
- `evaluation/`: ground-truth evaluations and the corpus scaling benchmark.
- `test_app.py`, `test_security_corpus.py`, `test_followup.py`, `tests/browser_smoke.py`, `tests/browser_followup.py`: regression checks.
- `.env.example`: every configuration variable, explained (no secrets).
