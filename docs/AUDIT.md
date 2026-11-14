# PlagIQ technical audit — November 2026

Read-only audit of `main` at commit `7a9aade`, which includes PR #51 (Zenodo and OCR). PR #52, which improves title detection for scanned PDFs, is open and not included. No code or production data was changed.

**Evidence levels used below**
- **Run**: executed in the development sandbox. The full test suite gave `370 passed`; the reproductions are quoted.
- **Read**: established by reading the code; file and line references are given.
- **Live**: seen on the production site (`/health`, reports the owner shared).
- **Needs info**: cannot be settled from the repository.

## 1. Executive assessment

PlagIQ is a small, well-tested monolith: one FastAPI web service plus one private "engine" service, each with SQLite. Its central design decisions are sound:
- matching is exact and verified, so every reported percentage is backed by visible passages;
- the denominator is defined (manuscript words);
- overlapping matches are merged, so no word is counted twice;
- a check with no comparable text reports "not evaluated" instead of 0%.

The weaknesses fall into four groups:
1. **A confirmed critical bug that turns real copying into 0%.** The boilerplate filter discards any seven-word sequence found in three or more of the compared sources. A paper that exists as a journal version, a repository copy and a web mirror is therefore *invisible*. Reproduced in §4.
2. **Coverage and status.** Results computed on partial coverage (failed full-text downloads, failed queries, library unavailable) still show a headline percentage with no "incomplete" label. Web search is configured in code but off in production.
3. **Detection scope.** Only exact seven-word matches are detected. Edited copying (a word changed every few words), paraphrase and translation are not. A cross-language model was measured, but it cannot run within the current 1 GB Railway limit.
4. **Operations.** Every check runs inside one long HTTP request. State is in memory: report store, rate limits and scan slots. There is no application logging or metrics. Railway's healthcheck and restart policy are the only monitoring.

Security is above average for a project of this size. There is an SSRF guard on all outgoing downloads, CSRF origin checks, `SameSite=Strict` HttpOnly cookies, no manuscript storage unless the user opts in, and upload and decompression limits. The main gaps:
- file parsers and OCR run inside the web process;
- there is no malware scanning (mitigated: files are never stored or served back);
- the anonymous rate limit may be shared by all users behind Railway's proxy (needs info);
- the CSP allows inline scripts.

## 2. Current architecture

```mermaid
flowchart LR
  B[Browser<br/>index.html, vanilla JS, no framework] -- POST /api/check (one long request) --> W
  TG[Telegram bot] -- webhook --> W
  subgraph Railway
    W[Web service: FastAPI app.py<br/>extract.py (PyMuPDF, python-docx, olefile, Tesseract OCR)<br/>corpus.py matcher, citations.py, integrity.py<br/>report_pdf.py, payments.py, auth.py]
    WV[(/data volume<br/>users.sqlite3: accounts, check log,<br/>payments ledger)]
    E[Engine service: engine_service.py<br/>corpus.py SQLite index of 7-word fingerprints<br/>background import loops]
    EV[(engine-volume ~433 MB<br/>corpus.sqlite3, import status)]
    W --- WV
    E --- EV
    W -- Bearer ENGINE_API_KEY, /v1/check, /v1/deposit --> E
  end
  W -- search phrases --> OA[OpenAlex API]
  W -- PDF/HTML downloads, compared then discarded --> OAS[Open-access publishers]
  W -. off in production .-> WS[Yandex / Brave web search]
  W -. off in production .-> PG[Pangram AI detector]
  W --> PAY[Payme / Click / Uzum, test mode]
  E -- imports --> SRC[OpenAlex, CORE, CyberLeninka, Uzbek journals,<br/>Wikipedia, Zenodo via OpenAlex]
```

| # | Area | What exists (Read unless noted) |
|---|---|---|
| 1 | Frontend | A single `index.html`, about 390 lines: vanilla JavaScript, inline CSS, no build step, no external scripts. UI languages: Uzbek Latin, Russian, English. **No Uzbek Cyrillic UI.** |
| 2 | Backend | FastAPI (`app.py`, 1,180 lines). JSON API under `/api/*`; HTML pages `/`, `/verify`, `/admin`, `/privacy`, `/login`; payment webhooks under `/payments/*`. |
| 3 | Databases | SQLite only. Web: `users.sqlite3` (accounts, check log, orders, transactions, ledger). Engine: `sources` (text and metadata JSON), `windows` (fingerprint, source, token index, `WITHOUT ROWID`, primary key on fingerprint), `corpus_meta`. |
| 4 | Document pipeline | `extract.extract_text`: TXT (UTF-8/16), DOCX (zip-bomb limits), DOC (own piece-table parser), PDF (PyMuPDF; ligatures expanded, line hyphens joined); OCR for pages without text. |
| 5 | Detection | `corpus.lexical` (normalisation, Uzbek Latin/Cyrillic folding) → seven-token windows → 64-bit fingerprints → hits chained and verified word by word (`match_source`) → intervals merged → `words_in_intervals`. One algorithm in every mode. |
| 6 | External APIs | OpenAlex (live); open-access downloads (live); Yandex/Brave (code present, **off**); Pangram (code present, **off**); Telegram (live); Payme/Click/Uzum (test mode; currently disabled in production). |
| 7 | Auth | Telegram login widget, phone sign-in through the bot, email/password (scrypt). Session cookie HMAC-signed, HttpOnly, SameSite=Strict. Owner-only `/admin` by Telegram ID. Optional Basic-auth pilot gate. |
| 8 | File storage | Uploads are kept in memory only (`spool_max_size` raised so nothing is spilled to disk) and never stored. Reports are kept in a process dictionary for 15 minutes, tied to a session cookie. Only opt-in archive deposits are stored, in the engine. |
| 9 | Background work | No queue. The website does checks synchronously, with `SCAN_CONCURRENCY` (default 2) in-process slots. The engine runs daemon threads (import loops, ligature upgrade) and its own request queue (`ENGINE_QUEUE_SECONDS`). |
| 10 | Railway | Dockerfile build; `/health` check with a 60 s timeout; restart on failure up to 5 times. Plan limit is 1 GB memory and 2 vCPU per service. No staging environment is defined in the repository (needs info). |

## 3. Plagiarism engine audit

| Question | Finding |
|---|---|
| Processing of uploads | Read into memory (20 MB per file, 45 MB per request), extracted in a worker thread, language detected from the text (it overrides the menu), at least 7 words required. |
| PDF / DOCX extraction | PyMuPDF `get_text` page by page, plus de-hyphenation and ligature expansion. DOCX: own walker over the body XML (tables, text boxes, breaks). Headers and footers in DOCX are **not** extracted (Read). |
| Segmentation | Every overlapping seven-token window (sliding window, stride 1), with no winnowing, so recall on exact copying is complete. |
| Query generation | `discovery`: title-like first line plus the most distinctive sentence from each part or chapter; 3 queries, +1 per 3,000 words, at most 8; 14 words each. |
| External sources | OpenAlex only for discovery. Downloads go to the open-access locations OpenAlex lists (8 works, 30 s budget). The web providers are off. |
| Verified matches? | Yes. A source counts only through passages verified word by word against its text. Records with metadata only are listed but not compared. |
| Percentage | Matched manuscript words ÷ all manuscript words (`_measure`, `words_in_intervals`). Exclusions (quotations, bibliography) are applied afterwards in `citations.assess`. Both figures are kept (`percent_all`, `percent_counted`). |
| Double counting | No. Intervals are merged across sources before counting. A source's own share may overlap another's; the report says so. |
| References / quotations | Rule-based (`citations.py`): quotation marks, citation markers, references heading. Exclusion is optional and both numbers are shown. |
| Failed scan → 0%? | Partly guarded. No compared text gives `percent=None` with a reason; an engine failure in corpus mode gives HTTP 503. **Not guarded:** partial coverage (H1) and the boilerplate filter (C1). |

## 4. Root causes of unreliable or 0% results

| ID | Severity | Root cause | Evidence |
|---|---|---|---|
| C1 | **CRITICAL** | `corpus.compare_texts` (lines 260–267) and `corpus.search` (lines 310–318) treat a fingerprint that occurs in ≥ `BOILERPLATE_SOURCES` (3) different sources as boilerplate and drop it. Copies of the *same work* (journal + repository + mirror; the same article imported by two harvesters; three students depositing the same thesis) are counted as "different sources", so **verbatim copying from a widely mirrored work scores 0%**. | Run: one source → 24/24 words matched; 3 copies → **0/24**. `compare_sources` with journal + repository + mirror → **0.0%**. Library with 3 copies → **0 sources found**. |
| H1 | HIGH | An academic check computes its percentage from whatever was compared. Failed downloads (`fulltext` failed/timed out), `discovery` status `partial`, library errors (`search_library` returns `searched: False`) and web-search errors are recorded in side fields but **do not change the result's status**. A 0.4% on abstracts only looks the same as a 0.4% on full texts. | Read `app.py` lines 960–1006 and 880–900. Live: the wp07-35 report (0.44%) before PR #50. |
| H4 | HIGH | Exact matching only. Changing one word in every 6–7 words, reordering sentences, paraphrase and translation all escape detection. This is documented in every report, but it is the largest gap in detection. | Read `METHOD` in `app.py`. The cross-language model (LaBSE) was measured in `evaluation/CROSSLANG.md`; it needs more than 1 GB of memory. |
| M4 | MEDIUM | Few, short search phrases. If OpenAlex does not return the copied work for 3–8 phrases of 14 words, it is never compared. The title query was taken from series labels and stamps; fixed in #50 and #52. | Live: wp07-35; the CamScanner syllabus. |
| — | — | Checked and **not** found as causes: Unicode handling (NFC, invisible characters, Uzbek apostrophe variants and Latin/Cyrillic folding are tested); Cyrillic support; thresholds (seven words is the only threshold); percentage arithmetic; silent exceptions in matching (the 11 broad `except Exception` handlers are in import loops, downloads and web search, and each records the error). | Run: test suite. Read: grep of `except Exception`. |

## 5. Database access (what is really searched)

| Source | Status | Notes |
|---|---|---|
| Live internet | **Placeholder in production.** Yandex and Brave adapters are present and tested with mocks; `/health` shows `web_search:false`. | Needs a Yandex or Brave key (DEPLOYMENT.md, Part 3g). |
| Academic repositories | **Live: OpenAlex** search, and download of open-access full texts per check (not stored). | OpenAlex mostly has abstracts; full texts depend on the publisher. |
| Previously uploaded documents | **Opt-in only.** Ordinary checks are not stored. Archive deposits (author or institution consent) are indexed. | `/health` shows `archive:true`. |
| Internal database | **Live: engine library.** 2,288 sources at the time of the syllabus report, from OpenAlex/CORE/CyberLeninka/Uzbek journals/Wikipedia/Zenodo imports. | Subject to C1 when a work was imported more than once. |
| External plagiarism providers | **None.** There is no Copyleaks or PlagiarismCheck.org code. | — |
| AI detector | Pangram adapter present (English, Russian); **off in production**. | No vendor in the code supports Uzbek. |

## 6. Stability and performance

| Topic | Finding |
|---|---|
| Timeouts | Every external call is bounded. OpenAlex 9 s per request; downloads 30 s in total; web search 15 s per call + 20 s for pages; engine 120 s (15–180); OCR 240 s; Pangram 90 s. The worst-case academic check is several minutes inside **one HTTP request** (HIGH, H3). Railway's maximum request duration needs confirming. |
| Retries | OpenAlex: up to two shorter retries per query when too few results come back; no retry after an error. Engine and downloads: none (by design, within the time budget). There is no retry or backoff for whole jobs. |
| Logging | **No application logging** in `app.py`; failures appear only in the result JSON (MEDIUM, M1). |
| Concurrency | 2 scan slots per process (`SCAN_CONCURRENCY`), and 1 OCR job at a time. The report store, rate limits and slots are process-local, so a restart loses reports and more than one replica is impossible (HIGH, H3). |
| File limits | 20 MB per file, 45 MB per request, 500 PDF pages, 1.2 M characters, DOCX expansion limits, OCR at most 120 pages. |
| Database | SQLite with WAL. The engine's `windows` table has its primary key on fingerprint (the lookup path). Measured in BENCHMARKS.md: 0.05–0.07 s per search up to 5,000 sources, about 171 MB per 1,000 sources. |
| Memory | Run (sandbox): extraction of a 70-page PDF took 0.06 s; matching a 30,000-word document against 8 full texts + 12 abstracts took 0.61 s; peak process memory 154 MB. **Network time dominates; CPU does not.** OCR and the evaluated cross-language model are the memory-heavy parts. |
| Background failures | Engine loops catch, record and continue; `/admin` shows the import status. There is no dead-letter queue (nothing is queued). |

## 7. Security

| ID | Severity | Finding |
|---|---|---|
| S1 | MEDIUM | Untrusted files are parsed in the web process (PyMuPDF, python-docx, olefile, Tesseract). A crafted file can exhaust memory or crash the process. Mitigations present: size, page and expansion limits. Recommended: parse in a subprocess with a memory and CPU limit. |
| S2 | MEDIUM | No malware scanning. Risk is limited because uploads are never stored, served back or executed. ClamAV adds about 1 GB of signatures, which does not fit the current plan. |
| S3 | MEDIUM (needs info) | The anonymous rate limit is keyed on the client address. With `TRUSTED_PROXY_HOPS=0` (the default), every anonymous user behind Railway's proxy may share one allowance of 12 checks per hour. The production value must be checked. Signed-in users are limited per account. All limits are in memory and reset on restart. |
| S4 | LOW | CSP has `script-src 'unsafe-inline'` because the page is one inline file. `highlight_html` is built server-side from escaped text. Moving the scripts to a file would allow a strict CSP. |
| S5 | OK | SSRF: `harvest.Fetcher` resolves hosts and refuses non-global addresses, cross-host redirects to non-HTTPS or private hosts, and oversize bodies. Used for all downloads. |
| S6 | OK | Secrets come only from environment variables; none are in the repository (`.env.example` has placeholders). Engine calls use a bearer key with `trust_env=False` and no redirects. |
| S7 | OK | Privacy: manuscripts are kept in memory for 15 minutes and never logged. Third-party transfer (Pangram, web search) needs a separate consent. Account deletion removes payment records. |
| S8 | LOW | Backups are manual downloads from `/admin`. Railway volume snapshot settings need info. |

## 8. Prioritised roadmap

| Order | Item | Severity | Size | Needs from owner |
|---|---|---|---|---|
| 1 | Fix C1: count copies of the same work as one source when deciding boilerplate, in both `compare_texts` and `search`. Add regression tests and re-run `evaluation/real_eval.py` before and after. | CRITICAL | Small | — |
| 2 | Explicit check status (H1): `completed_matches`, `completed_no_matches`, `incomplete_coverage` (with reasons), `failed_extraction`, `failed_search`, `failed_internal`. Shown on the page, the PDF and the certificate; an incomplete check never shows a bare percentage. | HIGH | Medium | — |
| 3 | Merge #52; set the web-search key; confirm `TRUSTED_PROXY_HOPS`. | HIGH | — | Key, setting |
| 4 | Structured logging (no manuscript text) and a `/admin` metrics panel: checks, failures by stage, p50/p95 time, provider errors, spending counters. | MEDIUM | Medium | — |
| 5 | Persistent jobs. SQLite job table on `/data` with a worker, status, retries, a dead-letter state and idempotency key. The browser polls, so the user can leave. Redis/Celery is not needed at this scale and adds a paid service. This is also the basis for a dashboard with scan history. | HIGH | Large | Decide whether reports may be stored (privacy text changes) |
| 6 | Near-duplicate detection (edited copying), selected by measurement on `evaluation/` benchmarks: exact seven-word windows vs. shorter shingles with alignment-based verification. Reported as a separate category. | HIGH | Large | — |
| 7 | UI: Uzbek Cyrillic UI, upload area, honest progress stages, dashboard (after 5), trust pages. | MEDIUM | Large | Approve texts and prices |
| 8 | External provider layer (Copyleaks, PlagiarismCheck.org) with mock adapters, webhooks and cost tracking. Off until credentials and a contract exist; per-check consent before any document leaves PlagIQ. | MEDIUM | Large | Account, budget, privacy decision |
| 9 | AI detection: the existing Pangram module is separate, consented, refunded on failure and English/Russian only. Add a per-institution off switch, a benchmark on human and AI texts per language, and documented limitations. | MEDIUM | Medium | Pangram key for the benchmark |
| 10 | Semantic and cross-language stage (LaBSE, measured). | MEDIUM | Large | Railway plan above 1 GB, or a separate service |
| 11 | Parse untrusted files in a sandboxed subprocess; staging environment; load test on staging. | MEDIUM | Medium | A staging service |

## 9. Files requiring modification (by roadmap item)

1. `corpus.py` (`compare_texts`, `search`, a new grouping helper); `test_matching.py`; `evaluation/REAL_RESULTS.md`.
2. `app.py` (`analyse`, `search_library`), `report_pdf.py`, `certificate.py`, `verify.py`, `index.html`, `engine_client.py`.
4. `app.py`, `admin.py`, a new `metrics.py`.
5. `app.py`, a new `jobs.py`, `auth.py` (history), `index.html`.
6. `corpus.py`, `evaluation/run_eval.py`, `evaluation/real_eval.py`.
7. `index.html`, `privacy.html`.
8. New `providers/` package; `app.py`; `admin.py`.
9. `aidetect.py`, `institutions.py`, `index.html`.

## 10. Recommended automated tests

- **C1 regression:** a verbatim copy compared with 1, 3 and 5 copies of its source (academic and library) must stay at 100%. A real boilerplate line in 3 *different* works must still be ignored.
- **Status:** each failure mode (download timeout, OpenAlex 5xx, engine down, OCR timeout, extraction failure) gives the matching status, and never `completed` with 0%.
- **Jobs:** a duplicate submission returns the same job; a job is resumed after a restart; it goes to dead-letter after N failures.
- **Security:** an oversize request; a zip-bomb DOCX; a PDF with excessive pages; SSRF to a private IP and to a redirect; cross-origin POST; another session's report ID → 404.
- **UI (Playwright, already used in `tests/`):** 320/390/768/1440 px with no horizontal overflow; keyboard-only upload; all four UI languages.

## 11. Information needed

- The production values of `TRUSTED_PROXY_HOPS`, `SCAN_CONCURRENCY`, `CORPUS_MAX_SOURCES` and `BOILERPLATE_SOURCES` (names only; never the secrets).
- Railway's request duration limit for this service, and whether volume snapshots are on.
- How many works in the live library exist in more than one copy (one read-only `/admin` diagnostic can count this after item 1).
- Whether a staging environment may be created (load testing, provider sandboxes).
