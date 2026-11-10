# Deploying PlagIQ on Railway — a step-by-step guide for the owner

You do not need to write code to follow this guide. Every step happens in the Railway website. Railway sometimes renames buttons; if a label differs, look for the closest equivalent.

> **Golden rules**
> - Test everything in a **Staging** environment first. Your live site stays on `main` until you choose to merge.
> - Secrets (passwords, keys) go **only** in Railway → *Variables*. Never put them in GitHub, chat or email.
> - This change does not touch payments.

---

## Part 1 — Try the new version safely (about 15 minutes)

1. Open your Railway project for `Sobirbe/PlagIQ`.
2. Create a **Staging** environment (top bar → environment menu → *New environment*, or duplicate production). Make sure it contains **no** production database, payment keys or real customer documents.
3. Click the website service in Staging → *Settings* → *Source*. Choose the branch from this pull request. Leave the build as **Dockerfile**. Leave the start command empty; the Dockerfile already uses Railway's `PORT`.
4. *Settings* → *Networking* → **Generate domain** for staging.
5. *Variables* → add:

   | Variable | Value | Why |
   |---|---|---|
   | `PUBLIC_ORIGIN` | your staging address, e.g. `https://plagiq-staging.up.railway.app` (no trailing `/`) | Blocks requests from other websites. |
   | `TRUSTED_PROXY_HOPS` | `1` | Lets PlagIQ see each visitor's real address. Without it, **all visitors share one 12-scans-per-hour allowance.** |
   | `PRIVATE_PILOT_USER` | a reviewer name | Optional login for the pilot. |
   | `PRIVATE_PILOT_PASSWORD` | a unique password of 16+ characters from a password manager | If set too short, the site deliberately locks everyone out (fails closed). |

   `.env.example` in the repository lists every setting with an explanation.
6. Click **Deploy**. When it finishes, open `https://<staging>/health`. You should see `"status":"ok","version":"0.5"`.
7. Open the staging homepage, sign in, and do the checks in Part 4.

**At this point the "Indexed academic corpus" option is greyed out.** That is expected: the private engine is not set up yet. Your own-reference and OpenAlex checks work without it.

## Part 2 — Add the private corpus engine (optional, about 20 minutes)

The engine holds the authorized full texts. It must **never** be reachable from the internet.

1. In Staging, click **+ New** → *GitHub Repo* → same repository and branch. Name the service **engine**.
2. Engine → *Settings* → *Deploy* → **Custom Start Command**:

   ```sh
   sh -c 'uvicorn engine_service:app --host :: --port ${PORT:-8000} --workers 1'
   ```

   Keep **one replica** for each service. Reports and limits are kept in memory.
3. Engine → *Settings* → *Networking*: make sure there is **no public domain**. Delete one if Railway created it.
4. Generate a random secret of at least 32 characters in your password manager. Add it as `ENGINE_API_KEY` on **both** the engine and the website (the same value).
5. Engine → right-click → **Attach volume** → mount path `/data`. Add the engine variable `CORPUS_DB=/data/corpus.sqlite3` and, if needed, `PORT=8000`.
6. On the website service, add `ENGINE_URL=http://engine.railway.internal:8000`. Use the engine's real private hostname (shown under the engine's *Networking*) and its port.
7. Optional website settings: `ENGINE_TIMEOUT=120` (15–180 seconds) and `SCAN_CONCURRENCY=2` (1–8; higher needs more memory).
8. Redeploy the engine, then the website. `/health` now shows `"corpus_engine_configured": true`. This only means the settings exist. The synthetic test in Part 4 proves the connection works.

**Upgrading an existing engine:** v0.5 changes the index format (7× smaller) and improves Unicode handling. On first start, the engine automatically rebuilds the index from the texts already stored in its database. It downloads nothing. Measured at about 0.1–0.2 s per source. Back up the volume first.

## Part 3 — Add publications you are allowed to index

A technical administrator must run this *inside the engine service*, with its `/data` volume attached. Railway's local CLI may not run inside the remote container, so confirm that first.

1. For each publication, keep written proof that you may store it in full, index it and show short excerpts. Being free to read online, or appearing in OpenAlex, is **not** permission.
2. Put the publication files (`.pdf` with selectable text, `.docx` or UTF-8 `.txt`) in one private folder. Create a `manifest.json` in the same folder:

   ```json
   [{"title": "Exact title", "authors": "Author names", "year": 2025,
     "url": "https://publisher.example/article", "language": "uz", "text_file": "article.pdf",
     "rights_basis": "written_permission", "permission_reference": "Agreement #12, 2026-03-01"}]
   ```

   `language` is `uz`, `ru` or `en`. `rights_basis` is `written_permission`, `open_license` or `public_domain`. The importer checks that these fields are filled in, but **a person must check that they are true.**
3. **Check first (writes nothing):** `python corpus.py /private/import/manifest.json --check --db /data/corpus.sqlite3`
   It lists every problem at once: missing citation or rights fields, wrong year format, non-HTTPS link, a scanned PDF with no text, a text whose script does not match its declared language (e.g. "ru" but mostly Latin letters), or a file outside the folder.
4. **Import:** pause scans, then run the same command without `--check`. Either every record is imported or none is.
5. **See what is indexed:** `python corpus.py --list --db /data/corpus.sqlite3` prints each source with language, year, rights basis, word count and identifier, totals by language, and the **corpus version**. Every PlagIQ report shows the corpus version it searched, so you can prove which collection a result covers.
6. **Withdraw a source:** `python corpus.py --remove <SHA-256> --db /data/corpus.sqlite3`.
7. Limits: 1,000,000 characters per text; 1,000 sources by default (`CORPUS_MAX_SOURCES` on the engine). Measured on a test machine: 1,000 article-sized sources use about 171 MB and searches take about 0.1 s (see `evaluation/BENCHMARKS.md`). Run `python evaluation/benchmark_corpus.py` on the engine before raising the limit. Never delete the volume without a tested backup.

## Part 3b — Collect open-licence articles from a journal (optional)

Many Uzbek and Russian journals run Open Journal Systems (OJS). OJS has a public "OAI-PMH" address that lists every article with its licence, usually `https://<journal site>/index.php/<journal>/oai`. You can find it on the journal's website or ask the editor.

1. **Ask first when in doubt.** The tool only takes articles whose own record says CC BY, CC BY-SA, CC0 or Public Domain. Even so, telling the editor that PlagIQ indexes their open-access articles for similarity checking is good practice. Some journals have non-commercial (NC) licences; the tool puts those aside for you to decide, because PlagIQ may become a paid service.
2. **Look without downloading:**
   `python harvest.py https://journal.example.uz/index.php/j/oai --out /data/staging/journal-j`
   Open `/data/staging/journal-j/review.csv` in Excel. Each article shows `accepted`, `review` (needs your decision, with the reason) or `rejected` (no usable licence or missing details).
3. **Download the accepted PDFs:** run the same command with `--download`. The tool waits between requests and obeys the journal's robots.txt. It takes the PDF only from the journal's own site, and only the file the article page names. If the article page shows a different licence, it puts the article aside for review.
4. **Import:** `python corpus.py /data/staging/journal-j/manifest.json --check`, then the same command without `--check`. Keep the staging folder: it records where each licence statement came from.
5. **Updates:** run step 3 again later. Only new or changed articles are fetched; already harvested ones are skipped.

This was tested against a simulated journal only. Try one journal and a small `--max-records 20` first, and check a few PDFs by hand.

## Part 3c — Collect open-licence full texts through OpenAlex and CORE (optional)

**Get the keys (free registration):**
- OpenAlex: create a free account at openalex.org and copy your API key. OpenAlex now bills per call against a free daily allowance (reported as $1/day per key in 2026); check help.openalex.org for current prices.
- CORE: register at core.ac.uk/services/api for a free API key.
- **Also set `OPENALEX_API_KEY` on the website service.** The "Find academic publications" mode calls OpenAlex. Without a key it may run out of allowance or be refused, and then shows **Not evaluated**.

Put the keys in Railway Variables on the engine service as `OPENALEX_API_KEY` and `CORE_API_KEY`, never in GitHub.

**What the tool does:** `oa_import.py` asks OpenAlex for open-access works in your chosen language or country that carry a CC BY, CC BY-SA, CC0 or public-domain licence. It downloads their PDFs and stages them for your review, like the journal harvester. OpenAlex itself grants no rights to full texts, so the tool checks each work's own licence. Non-commercial or no-derivatives licences go to `review.csv` for your decision. For CORE, a text is kept only if its DOI shows an accepted licence in OpenAlex.

**Commands** (run inside the engine service, like the importer):
1. See what is available, downloading nothing:
   `python oa_import.py openalex --language uz --out /data/staging/openalex-uz --max-records 50`
2. Download the accepted PDFs (free, from the publisher's own link):
   `python oa_import.py openalex --language uz --out /data/staging/openalex-uz --download`
   Other filters: `--country UZ` (authors at Uzbek institutions), `--since 2020`, `--query "oliy ta'lim"`, `--source-id S...` (one journal). Add `--unpaywall-email you@example.org` to double-check every licence with Unpaywall; disagreements go to review.
3. If a publisher link fails, `--pdf-from openalex` uses OpenAlex's cached copy instead (reported price about $0.01 per PDF).
4. CORE: `python oa_import.py core --query "language.code:ru" --out /data/staging/core-ru`
5. Import as usual: `python corpus.py /data/staging/openalex-uz/manifest.json --check`, then without `--check`.

**Spending limit:** every run stops before its estimated OpenAlex cost passes `--max-cost-usd` (default $0.50) and keeps what it already staged. `cost.json` shows the estimate and the number of calls by type.

**What has been tried live:** OpenAlex search and the metadata-only run (`openalex` without `--download`) were run against the real OpenAlex API with a real key in November 2026: 300 records in uz/ru/en were staged correctly, OpenAlex's live prices matched the built-in cost estimates, and 12 of 100 records OpenAlex labelled Russian were Uzbek Cyrillic and were corrected. **Not yet tried live:** PDF downloads (`--download`) and CORE, because the development network could not reach publisher sites or CORE. Start with `--max-records 20` and check `review.csv` and a few PDFs by hand.

**How much is available** (OpenAlex counts, November 2026, using OpenAlex's own language labels): Uzbek-language works with a reusable licence 110,482 (47,734 with an OpenAlex PDF copy); Russian-language 460,073; Russian-language by Uzbekistan-based authors 13,914; English by Uzbekistan-based authors 103,155. At about 0.17 MB of index per article, 100,000 articles would need roughly 17 GB; this size has not been benchmarked yet, so raise `CORPUS_MAX_SOURCES` in steps and re-run the benchmark.

## Part 4 — Checks before you approve the pull request

Use only harmless sample text. Tick each item:

- [ ] In UZ, RU and EN, on a phone and a computer, the page fits the screen with no sideways scrolling.
- [ ] *Compare with my own source*: paste the same 10-word sentence as the document and the reference. The result is 100%, measured in **words**, with the matching passage quoted and highlighted.
- [ ] Upload a 3-word file. A translated "at least 7 words" message appears, not 0%.
- [ ] Upload a `.txt` file saved in Windows-1251. A translated "save as UTF-8" message appears.
- [ ] *Academic discovery*: the consent text names OpenAlex. If nothing usable comes back, the result says **Not evaluated** with a reason.
- [ ] *Corpus* (if set up): import one synthetic text you wrote, then check it. The correct title, author, year, rights basis and passage appear. The percentage uses the same word measure as the other modes, and the corpus version matches `corpus.py --list`. A language with no indexed sources shows **Not evaluated**. The indexed-sources row shows counts per language.
- [ ] Paste a reference, and put the same sentence in your document inside «quotation marks» followed by [1]. Tick **Exclude matches inside quotation marks**. The page must show the lower percentage *and* "Before exclusions: …" with the number of words excluded.
- [ ] Download the PDF in each language. Check the Cyrillic text, "Page X of Y", the scope, the limitations, and that it says it is not a certificate.
- [ ] Open the PDF link in a private/incognito window. It must show "not found". Click **Delete report**; the link must stop working.
- [ ] The engine has no public domain, and no keys appear in GitHub or in the page source.

## Part 5 — Going live, and undoing it

- **Go live:** merge the pull request only when Staging passes. Copy the reviewed variables (Parts 1–2) to the production environment. Set `PUBLIC_ORIGIN` to the production address.
- **Undo (rollback):** Railway → website service → *Deployments* → choose the last good deployment → **Redeploy**. To switch off just the corpus option, delete `ENGINE_URL` from the website. Reference and OpenAlex checks keep working. Keep the engine volume; reports are temporary and disappear on restart.
- **Costs:** two small services (website plus engine) and one small volume. Watch Railway's *Usage* page during the pilot; this project has not measured costs.
- **Monitoring:** Railway's built-in logs and health checks. If you add an error-tracking service, turn **off** request-body capture so manuscripts are never sent to it.

## Running tests (for a technical helper)

```bash
pip install -r requirements-dev.txt
pytest -q
CHROMIUM_PATH=/path/to/chromium python tests/browser_smoke.py
CHROMIUM_PATH=/path/to/chromium python tests/browser_followup.py docs/screenshots
```
