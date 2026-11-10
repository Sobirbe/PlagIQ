# PlagIQ · multilingual academic comparison pilot (v0.5)

PlagIQ compares Uzbek Latin/Cyrillic, Russian and English manuscripts. It reports **observed overlap in a stated scope**, never a plagiarism verdict, AI-authorship score or guarantee of originality.

## Available comparisons

- **Reference:** exact normalized seven-token matches against your uploaded/pasted reference.
- **Academic discovery:** explicit consent sends at most 14 words to OpenAlex; compares returned abstracts only. Metadata records are not full-text coverage. Failure/no usable abstracts displays **Not evaluated**.
- **Private corpus:** the existing Noplag-style client now connects to an included, authenticated private backend (`engine_service.py`). This is PlagIQ's bounded exact-match implementation of the adapter protocol, not a bundled upstream Noplag distribution or its advanced algorithms. Seven-token SHA-256 fingerprints retrieve sources; original-character offsets support evidence and Uzbek script folding. Overlapping spans are merged before calculating character overlap. Tokens and characters are different measurement units.

**No academic collection is bundled or claimed.** The corpus starts empty; this pilot caps imports at 100 sources. Rights-gated imports support all three languages, but operational coverage exists only after authorized full texts are supplied. Corpus counts are reported by language, not inferred from a publisher's size. Empty language coverage displays **Not evaluated**. Searching a populated corpus with no matches can return 0%; this does not establish originality. Source lists are bounded (25 browser / 15 PDF entries); truncation is disclosed and totals include all matches. Exact quotes and bibliography text can match and are **not excluded** (stated in every result and PDF); paraphrase matching and cross-language translation detection are not implemented.

All modes share one tokenizer (`corpus.lexical`/`corpus.tokens`): Unicode NFC composition, removal of invisible characters (soft hyphen, zero-width), lower-casing, Uzbek Latin↔Cyrillic folding and Russian ё→е, with original-character offsets kept for evidence. Reference and OpenAlex results are **token (word)** measures; private-corpus results are **character** measures over merged passages. The UI and PDF name the unit.

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
- `test_app.py`, `test_security_corpus.py`, `test_followup.py`, `tests/browser_smoke.py`, `tests/browser_followup.py`: regression checks.
- `.env.example`: every configuration variable, explained (no secrets).
