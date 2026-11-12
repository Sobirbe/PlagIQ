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
   | `TRUSTED_PROXY_HOPS` | `1` | Lets PlagIQ see each visitor's real address. Without it, **all signed-out visitors share one 12-checks-per-hour allowance.** Signed-in users are counted per account (`CHECKS_PER_HOUR_ACCOUNT`, default 30), so students behind one university address do not share. |
   | `PRIVATE_PILOT_USER` | a reviewer name | Optional login for the pilot. |
   | `PRIVATE_PILOT_PASSWORD` | a unique password of 16+ characters from a password manager | If set too short, the site deliberately locks everyone out (fails closed). |

   `.env.example` in the repository lists every setting with an explanation.
6. Click **Deploy**. When it finishes, open `https://<staging>/health`. You should see `"status":"ok","version":"0.5"`.
7. Open the staging homepage, sign in, and do the checks in Part 4.

**At this point the "Indexed academic corpus" option is greyed out.** That is expected: the private engine is not set up yet. Your own-reference and OpenAlex checks work without it.

## Part 2 — Set up the private engine and the automatic import (about 20 minutes)

The engine is a second copy of PlagIQ that holds the library of full texts. Visitors never reach it directly; only your website talks to it. Once set up, it collects open-licence Uzbek works from OpenAlex by itself, a batch each day. **No command line is needed.**

**A. Create the engine service**
1. Open your Railway project and choose the environment (**production**, or **Staging** if you want to try it there first).
2. Click **+ New** (or **Create**) → **GitHub Repo** → `Sobirbe/PlagIQ`, branch **main**.
3. Click the new service → **Settings** → rename it to exactly `engine`.
4. **Settings → Deploy → Custom Start Command**, paste:
   ```sh
   sh -c 'uvicorn engine_service:app --host :: --port ${PORT:-8000} --workers 1'
   ```
5. **Settings → Networking**: if there is a public domain, delete it. The engine must have **no public domain**.
6. Right-click the engine (or **+ New → Volume**) → **Volume**, attach it to `engine`, mount path `/data`. On the Hobby plan this is a few GB, which is enough for thousands of articles; see "Disk space" below.

**B. Engine variables** (engine → **Variables** → **New Variable** for each)

| Name | Value |
|---|---|
| `ENGINE_API_KEY` | a random secret of at least 32 characters (use your password manager's generator) |
| `PORT` | `8000` |
| `CORPUS_DB` | `/data/corpus.sqlite3` |
| `OPENALEX_API_KEY` | your OpenAlex key (the same one as on the website) |
| `AUTO_IMPORT` | `uz` (later `uz,ru,en` for all three languages) |
| `AUTO_IMPORT_BATCH` | `300` (new works per language per day; raise later) |
| `CORPUS_MAX_SOURCES` | `5000` to start |

Optional:
- `AUTO_IMPORT_MAX_COST`: OpenAlex spending cap per language per day, default `0.20` (USD).
- `AUTO_IMPORT_OPENALEX_PDF=1`: if a PDF can't be downloaded from the journal, use OpenAlex's copy (about $0.01 each, always within the cap).
- `UNPAYWALL_EMAIL`: your email, to double-check every licence with Unpaywall.
- `AUTO_IMPORT_HOURS`: hours between runs, default `24`.

**C. Website variables** (website service → **Variables**)

| Name | Value (type exactly; Railway fills in the engine's values itself) |
|---|---|
| `ENGINE_API_KEY` | `${{engine.ENGINE_API_KEY}}` |
| `ENGINE_URL` | `http://${{engine.RAILWAY_PRIVATE_DOMAIN}}:8000` |

**D. Deploy and check**
1. Deploy the engine first, then the website (Railway shows **Deploy** or **Apply changes** after variable edits).
2. Engine → **Logs**: within a few minutes you should see lines like `import uz: 37 imported, 4 failed, corpus 37 sources`. The first run starts right after deployment.
3. On your website, in "What PlagIQ checks", the **PlagIQ library** card now shows e.g. "Currently indexed: UZ 37 · RU 0 · EN 0 · total 37 · last import 2026-11-11".
4. The **Indexed academic corpus** option in the checker is no longer greyed out. Try it with a sentence copied from one of the imported Uzbek articles: it should find the article, quote the passage and link to it.

**What the automatic import does (and doesn't)**
- **Accepts** only works whose licence OpenAlex lists as CC BY, CC BY-SA, CC0 or public domain. Non-commercial and no-derivatives works are skipped.
- **Downloads** each PDF from the publisher's link, or from the PDF that the journal's article page names (after checking the page doesn't show a different licence).
- **Checks** each text with the importer's rules: language from the text itself, a minimum length, required details.
- **Indexes** the text, then deletes the PDF to save disk space.
- **Remembers** where it stopped, and continues the next day.
- **Never** indexes users' manuscripts.
- A failed article is skipped with its reason; it doesn't stop the others.

**Turning it off:** delete `AUTO_IMPORT` (or set it empty) and redeploy. Imported texts stay in the library.

**E. Add Wikipedia (recommended)**
Students often copy from Wikipedia. Its text is licensed CC BY-SA 4.0, which allows indexing and quoting short excerpts with attribution. PlagIQ records "Wikipedia contributors", the article link, the revision and the licence for every article. On the engine, add:

| Name | Value |
|---|---|
| `AUTO_IMPORT_WIKIPEDIA` | `uz,ru` (Uzbek and Russian) |
| `WIKIPEDIA_BATCH` | `2000` (articles per day) |
| `WIKIPEDIA_MIN_WORDS` | `300` (shorter "stub" articles are skipped) |
| `WIKIPEDIA_MAX_ARTICLES` | `2000` per language (the default): a cap so Wikipedia doesn't crowd out academic sources |

- **How it works:** the engine streams Wikimedia's official download file. It never sends many requests to Wikipedia's live site.
- **Measured (November 2026):** 500 Uzbek articles imported in about 30 seconds with no failures. A passage copied from one of them was traced to the correct article.
- **Coverage:** Uzbek Wikipedia's file is about 300 MB, and many of its articles are short stubs. Russian (`ru`) and English (`en`) work too, but their files are many gigabytes and each daily run reads from the start of the file. Use them only with a larger volume and a higher `CORPUS_MAX_SOURCES`.
- **Updates:** changed articles replace their older version the next time the file is read through. Progress appears in the PlagIQ library card.
- **Limits:** imported sources (OpenAlex, Wikipedia, CyberLeninka, journals) share `CORPUS_MAX_SOURCES`. Uzbek Wikipedia alone has tens of thousands of articles over 300 words and imports fastest, so it is capped per language by `WIKIPEDIA_MAX_ARTICLES` (default 2,000). When the cap is reached, the import stops and the library card says so. **Archive deposits** by users and universities have their own limit, `ARCHIVE_MAX_SOURCES` (default 5,000), so bulk imports can never block a student's or a university's deposit.
- **Russian Wikipedia:** the file is about 6 GB compressed, and each run streams it from where the last run stopped. With the 2,000-article cap it imports the first 2,000 long articles, ordered by page number, which are mostly the older and most-read topics. The streamed file is not stored.
- **Library card:** the website now shows the library by source (Uzbek and other journals, CyberLeninka, Wikipedia, university archives, authors' archive).

**Disk space and limits** (measured on a test machine, article-sized sources; see `evaluation/BENCHMARKS.md`):

| Sources | Disk | Search (repeated) | Memory |
|---|---|---|---|
| 1,000 | about 170 MB | about 0.05 s | about 38 MB |
| 5,000 | about 860 MB | about 0.07 s | about 38 MB |

`CORPUS_MAX_SOURCES=5000` fits a 1 GB volume with room to spare. Raise it, and `AUTO_IMPORT_BATCH`, step by step, while watching the volume usage on the engine's **Metrics** tab.

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
4. CORE: `python oa_import.py core --query "language.code:ru AND (Uzbekistan OR Узбекистан)" --out /data/staging/core-ru`
   **What to expect from CORE** (measured live, November 2026): CORE returns full texts but **no licence information at all**, and few records have a DOI to look the licence up (2 in a sample of 100 Russian records; 11 of 100 Uzbek). So most CORE records are rejected unless you trust their repository:
   - Open the repository's website and find its licence or open-access policy (for example "all content is published under CC BY 4.0"). Save a screenshot or PDF with the date.
   - Find the repository's CORE ID in `review.csv`, or on core.ac.uk. Examples seen live: Uzbekistan Research Online = 14935; Publishing House of the National University of Uzbekistan = 25822.
   - Run with `--trusted-provider 14935 --trusted-note "CC BY 4.0 policy at <page>, checked 2026-11-10, screenshot saved"`. Your note is stored with every text as the permission evidence. An explicit non-commercial licence found for a paper still sends it to review.
   - Only trust a repository after reading its policy yourself. PlagIQ cannot verify it for you.
5. Import as usual: `python corpus.py /data/staging/openalex-uz/manifest.json --check`, then without `--check`.

**Spending limit:** every run stops before its estimated OpenAlex cost passes `--max-cost-usd` (default $0.50) and keeps what it already staged. `cost.json` shows the estimate and the number of calls by type.

**What has been tried live:** CORE (with a real key, November 2026): 40 records searched, 5 CC BY Russian full texts staged with their language corrected from the text, and all 5 passed `corpus.py --check`. OpenAlex search and the metadata-only run (`openalex` without `--download`) were run against the real OpenAlex API with a real key in November 2026: 300 records in uz/ru/en were staged correctly, OpenAlex's live prices matched the built-in cost estimates, and 12 of 100 records OpenAlex labelled Russian were Uzbek Cyrillic and were corrected. **Not yet tried live:** PDF downloads (`--download`), because the development network could not reach publisher sites. Start with `--max-records 20` and check `review.csv` and a few PDFs by hand.

**How much is available** (OpenAlex counts, November 2026, using OpenAlex's own language labels): Uzbek-language works with a reusable licence 110,482 (47,734 with an OpenAlex PDF copy); Russian-language 460,073; Russian-language by Uzbekistan-based authors 13,914; English by Uzbekistan-based authors 103,155. At about 0.17 MB of index per article, 100,000 articles would need roughly 17 GB; this size has not been benchmarked yet, so raise `CORPUS_MAX_SOURCES` in steps and re-run the benchmark.

## Part 3f — QR code verification on PDF reports

Every PDF report now opens with a one-page **Certificate of check** (in Uzbek, Russian or English). It shows the certificate number, the document name, date, words, language, fingerprint, the result, the sources compared, any exclusions, the AI and manipulation indicators, the scope, and a large QR code. It certifies that PlagIQ checked the document with that result, **not** that the work is original. The evidence pages follow it.

**The QR code appears only after you set `VERIFY_SECRET`** (below). Until then the certificate shows an empty box and "Online verification is not enabled on this server".

Every PDF report can carry a QR code. A supervisor or editor scans it and sees a PlagIQ page confirming the report's date, result, number of words, sources compared, language and the start of the document's fingerprint (SHA-256). If anyone changes those figures, the page says **not valid**.

- **No storage needed.** The facts are signed with a secret only your server knows, so the codes keep working after the report itself is deleted (15 minutes) and after restarts. The link contains no file name and no text of the manuscript.
- **To switch it on:** website service → **Variables** → add `VERIFY_SECRET` = a random secret of at least 40 characters (use your password manager's generator) → redeploy. Make sure `PUBLIC_ORIGIN` is your real address (e.g. `https://plagiq-production.up.railway.app`) so the QR code points there.
- **Keep the secret unchanged.** Changing it makes all earlier QR codes show "not valid". Never share it.
- Without `VERIFY_SECRET`, reports say "Online verification: not set up on this server".
- The verification page works even when the private-pilot login is on, because it shows only the signed facts.

## Part 3g — Internet search with Yandex and/or Brave (optional, off until you add a key)

Antiplagiat-style services also search the open web. With a Yandex key, PlagIQ can do the same for **Russian and Uzbek web pages** (and English through Yandex's international search). **Brave Search API** is simpler to set up and covers English well. You can use either one or both. The user must tick a separate box, "Also search the web with …" (it names the services you set up); it is off by default and appears only in **Find academic publications** mode.

**What happens:** short excerpts (at most 14 words each) spread across the whole document are searched: the title, plus the most distinctive sentence from each part of the text (one excerpt per 500 words, at least 3 and at most 20; the bibliography is skipped). Each is searched as an exact phrase in quotes; the first 3 are searched again without quotes if nothing is found. With both services set up, Russian and Uzbek manuscripts go to Yandex first and English ones to Brave first; the other service is used for an excerpt only if the first one fails (for example, a rate limit). Up to 10 result pages (web pages or PDFs) are downloaded, pages found by several excerpts first, compared word by word with the same seven-word matcher, and discarded. The report marks them "Web page (compared, not stored)" and shows a line such as "Web (Yandex): 10 excerpts, 12 searches, 10 pages tried, 7 compared".

**Set-up for Brave (about 5 minutes):**
1. Sign up at api-dashboard.search.brave.com and choose a **Search** plan (check its current price and monthly request limit there). A PlagIQ check makes about one search per 500 words, **at most 23** (20 excerpts, plus up to 3 retries).
2. **API keys → Add API key**; copy it once. It is a secret.
3. Railway → **website** service → **Variables**: add `BRAVE_API_KEY` = the key → redeploy.
4. If your plan allows more than one request per second, you may set `BRAVE_INTERVAL=0`; the default `1` keeps to the basic plan's limit (a check then takes up to ~6 seconds longer).

**Set-up in Yandex Cloud (about 15 minutes):**
1. Sign in at console.yandex.cloud and create a **billing account** (Yandex Search API is a paid service; check the current price per 1,000 requests on its pricing page). A PlagIQ check makes about one search per 500 words, **at most 23** (20 excerpts, plus up to 3 retries).
2. Open your **folder** (e.g. `default`). Its **folder ID** (starts with `b1g…`) is shown at the top; copy it. It is not secret.
3. **Service accounts → Create**: name `plagiq-search`, role **`search-api.webSearch.user`**.
4. Open the service account → **Create new key → API key**, scope **`yc.search-api.execute`**. Copy the key once; it is a secret.
5. Railway → **website** service → **Variables**: add `YANDEX_API_KEY` = the key and `YANDEX_FOLDER_ID` = the folder ID → redeploy.
6. Check `https://<your site>/health` shows `"web_search": true` and the services in `"web_providers"`, then run an academic check with the box ticked.

| Name (website service) | Default | Meaning |
|---|---|---|
| `BRAVE_API_KEY` | — | Brave Search API key (secret; only in Railway Variables) |
| `BRAVE_INTERVAL` | `1` | seconds between Brave requests (0–5) |
| `YANDEX_API_KEY` | — | service-account API key (secret; only in Railway Variables) |
| `YANDEX_FOLDER_ID` | — | Yandex Cloud folder ID |
| `WEB_SEARCH` | `1` | `0` hides the option even when a key is set |
| `WEB_MAX_QUERIES` | `20` | most excerpts searched per document (3–40); one per `WEB_WORDS_PER_EXCERPT` (500) words, so a 5,000-word paper uses 10. Each excerpt is one search (the first 3 may need two), so cost grows with document length |
| `WEB_MAX_PAGES` | `10` | result pages downloaded per check (1–20); pages found by several excerpts come first |
| `WEB_SECONDS` | `20` | time limit for those downloads (5–60) |

**Privacy note:** the tick box tells users that excerpts go to the named search services as well as OpenAlex. Do not tick it for confidential manuscripts.

## Part 3h — AI-writing and manipulation indicators (on by default)

Every report now has a section **"AI-writing and manipulation indicators"**. It does **not** give an AI percentage, because no tool can measure that reliably, least of all in Uzbek. Instead it lists checkable signs, each with the exact place in the text:

- chatbot phrases left in the text ("As an AI language model", "Certainly! Here is…", also in Russian and Uzbek);
- unfilled placeholders ("[Insert citation]", "(Author, Year)");
- hidden characters and look-alike letter swaps (Latin "a" ↔ Cyrillic "а"), which "humanizer" tools use to fool checkers;
- leftover chat formatting (##, **);
- in **Find academic publications** mode, with the user's consent: reference-list DOIs that do not exist or that belong to a different work. Only the DOIs are sent, to OpenAlex (free) and doi.org.

The result is "Review suggested" or "No indicators found". Neither is a verdict: a supervisor should look at the listed places. On 900 human-written Wikipedia articles, 3 were flagged (details in `evaluation/INTEGRITY.md`). The text excerpts appear only on the results page; the PDF shows counts and DOIs.

To switch the DOI lookups off, set `INTEGRITY_DOI_CHECK=0` on the website service.

## Part 3i — Sign-in (Telegram, phone number, email)

People can sign in in three ways, all under the "Sign in" button at the top of the site:
- **Telegram:** "Sign in with Telegram" on the website (steps 1–5 below).
- **Phone number through the Telegram bot** (needs step 6): the person opens the bot, presses "📱 Share my number", and the bot replies with a one-time sign-in link (valid 10 minutes). Only the sender's *own* number is accepted (Telegram tells the bot whose number it is). The link opens the site in Telegram's built-in browser; people who prefer their usual browser can choose "Open in browser".
- **Email and password:** on by default (`EMAIL_LOGIN=0` turns it off). **No email is sent and the address is not verified**: the email is only a login name. The password is stored only as a scrypt hash, and after 10 wrong passwords that email and that connection must wait 15 minutes. If someone forgets their password, the account cannot be recovered; they create a new one. Email-only sites (no Telegram bot) need `SESSION_SECRET` (32+ characters) to sign sessions.

**Without an account:** plagiarism checks of texts up to `ANON_MAX_WORDS` words (default **1000**, the Free plan) need no sign-in. Longer texts, the AI detector, the archive and paid checks always need an account. `ANON_MAX_WORDS=0` makes every check need sign-in.

PlagIQ stores the Telegram ID, name and username; the phone number only if the person shares it with the bot; the email and password hash for email accounts; and a list of checks (date, mode, word count, result and AI estimate). It never stores documents or file names. Users can delete their account from the menu.

1. In Telegram, open **@BotFather** and send `/newbot`. Name it, for example `PlagIQ`, with a username such as `plagiq_uz_bot`. BotFather replies with a **token**, which is a secret.
2. Still in @BotFather, send `/setdomain`, choose your bot, and send your site address without `https://`, for example `plagiq-production.up.railway.app`. Without this step Telegram refuses the sign-in.
3. Railway → **website** service → **Settings → Volumes** → add a volume mounted at **`/data`**. Accounts are kept there, so they survive redeploys. Without a volume, accounts are lost at each deploy.
4. Railway → **website** service → **Variables**: add `TELEGRAM_BOT_TOKEN` = the token, and `TELEGRAM_BOT_USERNAME` = the bot username without @. Deploy.
5. Open the site. "Sign in with Telegram" appears at the top. Checks need sign-in by default; set `LOGIN_REQUIRED=0` to make it optional.
6. Recommended: make the bot answer people who open it and press **/start**, and turn on sign-in by phone number (otherwise the bot stays silent, and people think sign-in is broken). Add `TELEGRAM_BOT_REPLIES=1` on the website service and redeploy. At start-up the site registers its address (`PUBLIC_ORIGIN` + `/telegram/webhook`) with Telegram. From then on the bot replies, in the user's Telegram language, with how to sign in and a "📱 Share my number" button, and the site shows "With my phone number (Telegram bot)" in the sign-in menu. /admin shows "Bot answers /start" ✅ once Telegram has accepted it. The bot stores only the phone numbers people choose to share. Do not turn this on if the same bot is already used by another program, because a bot can have only one webhook.

The QR verification page (`/verify`) stays public, so supervisors can scan certificates without an account.

## Part 3j — AI detector ("Hujjatni AI detektoriga tekshirish")

The **AI detector** mode, plus an optional "Also check with the AI detector" box in the other modes, sends the **full text** to Pangram (USA), with its own consent tick. It shows Pangram's estimate of the AI-written, AI-assisted and human shares, with flagged passages. **Russian and English only:** Pangram does not support Uzbek, so Uzbek texts are not sent and nothing is charged. The page, PDF and certificate present the figures as Pangram's estimate, not proof.

1. Sign up at **pangram.com**, open the **API dashboard**, create an **API key** and add credits. Consider the Professional plan (see Part 3d on costs).
2. Railway → **website** service → **Variables**: add `PANGRAM_API_KEY` = the key. Deploy.
3. `/health` should show `"ai_detector": {"enabled": true, ...}`, and "Hujjatni AI detektoriga tekshirish" appears among the modes.

| Name | Default | Meaning |
|---|---|---|
| `PANGRAM_API_KEY` | — | secret; turns the AI detector on |
| `AI_MAX_WORDS` | `15000` | longest document sent (cost cap; one 15,000-word check costs about $7.50 at the list price) |
| `AI_SECONDS` | `90` | time limit per check |
| `PANGRAM_MODEL` | `pangram-4` | model selector |

**Before going live,** read Pangram's terms on data use and retention, add them to your privacy notice, and have your lawyer confirm that sending full manuscripts abroad is acceptable under Law ZRU-547.

## Part 3m — CyberLeninka (Russian open-access articles)

CyberLeninka is the largest open collection of Russian-language scientific articles, and the source that Russian-language checkers rely on most. PlagIQ can import its **openly licensed** articles into the private library:

- The article list comes from CyberLeninka's OAI-PMH feed. The **licence is read from each article page** (its "CC BY" badge). CC BY, CC BY-SA and CC0 are imported. CC BY-NC and CC BY-ND go to `review.csv` for your decision. Articles without a licence badge are skipped.
- CyberLeninka's robots.txt **forbids downloading PDFs**, so the full text is taken from the article page, which robots.txt allows. At least 3 seconds pass between requests.
- In a live test (November 2026), 8 of the first 10 articles were imported. The other 2 had no authors or no readable text. A passage copied from an imported article was found.

**Automatic import on the engine:** add `AUTO_IMPORT_CYBERLENINKA=1` to the engine service. Each run examines `CYBERLENINKA_BATCH` (200) articles, about 10–15 minutes at 3 seconds per request, and continues where the last run stopped, every `AUTO_IMPORT_HOURS`. Optional filters:
- `CYBERLENINKA_SET=vak` limits the import to journals on the VAK list (recommended, since these are the articles students cite most);
- `CYBERLENINKA_FROM=2018-01-01` takes only records changed since that date.

**Size:** CyberLeninka has millions of articles, and you cannot index all of them. `CORPUS_MAX_SOURCES` (default 1,000) stops the import when the limit is reached. Raise it step by step while watching the volume size: 5,000 sources is about 0.9 GB (see `evaluation/BENCHMARKS.md`).

**Manual run** (engine shell): `python cyberleninka.py --out staging/cyberleninka --max 200 --set vak --import`

Before you import at scale, read CyberLeninka's terms of use and have your lawyer confirm them. The imported articles are under open licences, but bulk collection from an aggregator may have its own conditions.
## Part 3n — Uzbek journals (open-licence articles)

Many Uzbek journals publish on Open Journal Systems, which has an OAI-PMH feed, and state a CC BY licence in every record. PlagIQ can find them and import their articles automatically.

**What was measured (November 2026):** OpenAlex lists 252 Uzbek journals. For 193 of them PlagIQ can derive a feed address, and those hold about **167,000 articles**. Of the three large journals sampled live:
- inlibrary.uz ("Современная наука и исследования") and inscience.uz ("Общество и инновации") state CC BY 4.0 in all usable records;
- erus.uz has a malformed feed and states no open licence, so it is skipped.

Two articles were then harvested and imported as a live test.

**Automatic on the engine:** add `AUTO_IMPORT_UZ_JOURNALS=1` (and optionally `UZ_JOURNALS_BATCH=50`, articles per journal per run).
1. The first run surveys every journal and writes `staging/uz-journals/journals.csv`, with status, licence share and languages for each.
2. Every run then harvests a batch from each journal marked `ok`: at least half of its records state CC BY, CC BY-SA or CC0.

Articles are imported only when their own record and article page state an open licence, and the PDF comes from the journal's own host. NC/ND licences go to `review.csv`. robots.txt and crawl delays are respected.

**Your control:** open `journals.csv` on the engine volume and change a journal's `status` to exclude or include it. Raise `CORPUS_MAX_SOURCES` gradually (5,000 sources ≈ 0.9 GB).

**Manual:**
```bash
python uz_journals.py survey --out staging/uz-journals
python uz_journals.py harvest --out staging/uz-journals --batch 50 --import
```

Also fixed: `harvest.py` now stops at `--max` within a feed page. Before, it always processed the whole page of up to 100 records.
## Part 3k — Protected archive of checked works (opt-in)

Signed-in users can tick **"Add this document to the PlagIQ protected archive"** when they run a check. The full text then goes into the private library, so **later copies of it are found**, even though it was never published. This is what antiplag.uz calls its "internal archive".

- **Who sees what:** other users see only the title, author and year of an archived work, plus the matching passage in *their own* document. They never see the archived text.
- **Self-checks:** checking the identical document again does not match itself. If the author checks a document that copies their own archived work, it is labelled "Your own archived work".
- **Removal:** users see their works in the menu under their name ("My archived works") and can remove them at any time. Deleting the account removes them too.
- **Consent:** the user must confirm they are the author or have the author's permission. That confirmation and its time are stored with the work.
- **Requirements:** the private engine (Part 2) and Telegram sign-in (Part 3i). Without both, the option is hidden.
- **Academic checks** now also search the private library (imported publications and the archive) when the engine is set up, so archived works are found in the default mode, not only in "PlagIQ library" mode.

**Before switching it on publicly,** add the archive to your privacy notice (what is kept, why, for how long, how to remove it), and have your lawyer confirm the consent text under ZRU-547. The archive grows with use, so watch the engine volume's size (see `evaluation/BENCHMARKS.md`; 5,000 sources ≈ 0.9 GB) and raise `CORPUS_MAX_SOURCES` when needed.

## Part 3l — University contributions to the archive

Partner universities can deposit their past theses, diploma and course works. They are matched like any source, but **their text is never shown**: reports give the title, author, year and "University deposit (TDIU)".

1. **Sign an agreement** with the university that allows indexing for plagiarism detection. Your lawyer should prepare it. Note its number and date.
2. Ask the university's responsible staff for their **Telegram user IDs**. They can get them from @userinfobot.
3. Railway → **website** service → **Variables**: add `INSTITUTIONS`, for example:
   ```json
   [{"name":"TDIU","agreement":"No. 12/2026, 01.11.2026","admins":[123456789,987654321]}]
   ```
4. Redeploy. When those staff sign in, they see a **"University deposit"** section. They choose files (any number; each is sent separately, 20 MB maximum) and press "Deposit files". A file named `Author - Title.docx` fills in the author and title, and the language is detected automatically. They can also list and remove their university's works.

**Whole archives (hundreds of files):** on the engine service, open a shell (Railway → engine → the "⋯" menu → Shell, or `railway run`). Upload the folder and a `works.csv` with the columns `file,title,authors,year,language` (language may be left empty), then run:
```bash
python institutions.py --institution TDIU --agreement "No. 12/2026" --csv works.csv --folder ./files --check
python institutions.py --institution TDIU --agreement "No. 12/2026" --csv works.csv --folder ./files
```
The first command only validates. The import is all-or-nothing. The agreement is stored with every work. Raise `CORPUS_MAX_SOURCES` first if needed (Part 2).

## Part 3e — Full-text comparison during academic checks (on by default)

In **Find academic publications** mode, PlagIQ now downloads the free full texts (PDFs) of the most relevant papers it finds and compares the manuscript against the **whole articles**, not only their abstracts. The texts are used for that one check and then discarded; they are not stored. Your lawyer has confirmed this use.

In a live sample (November 2026), most papers found came with a free full text: English 22 of 24, Russian 20 of 24, Uzbek 16 of 16. The report marks each paper as "Full text (compared, not stored)" or "Abstract".

Optional settings on the **website** service:

| Name | Default | Meaning |
|---|---|---|
| `FULLTEXT_FETCH` | `1` | `0` turns it off (abstracts only) |
| `FULLTEXT_MAX` | `8` | papers downloaded per check (1–20) |
| `FULLTEXT_SECONDS` | `30` | time limit for all downloads in one check (5–90); each single download gets at most 10 s; slower ones are skipped and counted |
| `FULLTEXT_OPENALEX_PDF` | off | `1` = try OpenAlex's own PDF copy **first** (most reliable; publisher sites often block or are slow), then publisher links. About $0.01 per paper, so up to $0.08 per check at the default 8. The free $1/day OpenAlex allowance covers about 12 such checks a day; set `FULLTEXT_MAX=5` to stretch it, or add a prepaid balance at openalex.org |

**If reports show "0 full texts compared":** the page and PDF now list why each download failed (timed out, refused by the website, no PDF link…). If most say "timed out" or "refused", set `FULLTEXT_OPENALEX_PDF=1`.

**Language:** PlagIQ now detects the manuscript's language from its text. If it clearly differs from the menu choice, the detected language is used and the report says so (e.g. "EN (detected from the text; selected: UZ)").

A check now takes a little longer (usually a few seconds, at most `FULLTEXT_SECONDS`). Watch the website's memory on Railway's **Metrics** tab after the change.

## Part 3d — Prices shown on the website

The website shows the word count and the **planned** price as soon as a file is chosen, plus a price table and calculator. **No money is taken** while payments are off (the default), and the page says checks are free during the pilot. Payments in the providers' **test mode** are described in Part 3o.

Default plans: Free up to 1,000 words (0 UZS), Researcher 1,001–3,500 words (15,000 UZS), Extended 3,501–15,000 words (25,000 UZS), Thesis 15,001–40,000 words (35,000 UZS), PhD/DSc dissertation 40,001–150,000 words (39,000 UZS). Uploads may be up to 20 MB per file and 500 PDF pages, so a whole PhD or DSc dissertation can be checked before submission to the OAK. Larger documents are shown as "contact us". Other services shown: AI detector 3 UZS per word (minimum 6,000), certificate with QR code included free, packages "10 checks → +2 free" and "20 checks → +5 free". For comparison, antiplag.uz (November 2026) charges 46,000 UZS per plagiarism check, 4 UZS per word for its AI detector (minimum 10,000), 6,000 UZS for a certificate, and gives +1 free for 10 checks and +3 for 20.

**AI detector cost:** Pangram's list price is $0.05 per 100 words, which is about 6 UZS per word at 12,500 UZS/$. At 3 UZS per word you cover the cost only with Pangram's **Professional** plan ($65 a month including $200 of API credits, so about 2 UZS per word while those credits last). On the pay-as-you-go price, every AI check loses money. Set `AI_PRICE_PER_WORD` and `AI_MIN_PRICE` to your own figures, and check the current exchange rate.

To change the plans, add a `PRICING_TIERS` variable to the website service in Railway, for example:

```json
[{"id":"free","max_words":1000,"price_uzs":0},{"id":"researcher","max_words":3500,"price_uzs":15000},{"id":"extended","max_words":15000,"price_uzs":25000},{"id":"thesis","max_words":40000,"price_uzs":35000},{"id":"phd","max_words":150000,"price_uzs":39000}]
```

`PRICING_PACKAGES` changes the packages, for example `[{"checks":10,"bonus":2},{"checks":20,"bonus":5}]`.

Keep the ids `free`, `researcher`, `extended`, `thesis` and `phd`, which have names and descriptions in all three languages. Change only the numbers. If the value is invalid, the site quietly uses the defaults. Before charging real money, measure the real cost per check; switching from test mode to real payments needs a separate, reviewed change (Part 3o).

## Part 3o — Payments with Payme, Click and Uzum Bank (TEST MODE ONLY)

This version takes **sandbox payments only**: it refuses to switch on unless `PAYMENTS_MODE=test`, it sends Payme users to the Payme sandbox (test.paycom.uz), and every page, receipt and the /admin dashboard says "TEST MODE". Real payments need a later, separately reviewed change.

**How it works.** A signed-in user tops up a balance (any amount between `TOPUP_MIN` and `TOPUP_MAX`, or a package: "10 checks + 2 free" means paying 10 × the plan price and getting 12 × the plan price on the balance). Each check costs its plan price (Part 3d), plus the AI-detector price if chosen; Free-plan checks cost nothing. The price is taken before the check and given back automatically if the check fails, or (for the AI part) if the AI detector could not run, for example for Uzbek text. Each paid top-up has a printable receipt in the account menu. This receipt confirms the payment; it is not a fiscal receipt.

**Payme sandbox** (business.paycom.uz → your cashbox → "Песочница"/sandbox; docs: developer.help.paycom.uz):
1. In the Payme cabinet, set the **endpoint URL** to `https://plagiq-production.up.railway.app/payments/payme`, the account field to **`order_id`**, and the account type to **one-time** ("Одноразовый").
2. On the **website** service in Railway → **Variables**, add: `PAYMENTS_ENABLED=1`, `PAYMENTS_MODE=test`, `PAYME_MERCHANT_ID` (the cashbox ID) and `PAYME_TEST_KEY` (the **test** key from the cabinet). Type the key only into Railway; never paste it into a chat or a file.
3. Open the sandbox and run its tests: wrong key (−32504), wrong amount (−31001), non-existent order (−31050), then create, repeat create, perform, cancel. For an order number and amount, start a top-up on the site and copy the order number from the sandbox checkout page.
4. Optional: fiscal (tax) data sent to Payme with each payment. PlagIQ sends it only when both codes are set:
   - `PAYME_IKPU`: the 17-digit IKPU (MXIK) code of the service. Find it on **tasnif.soliq.uz**: search for the service (for example "dasturiy ta'minot" / "программное обеспечение" / information services), open the result that describes it, and copy its code. Pick the entry that describes *what PlagIQ sells*: online access to a software service for checking documents. Entries about multimedia or entertainment content do not fit. Your accountant should confirm the choice; PlagIQ cannot pick it for you.
   - `PAYME_PACKAGE_CODE`: the package (unit) code shown in the details of that same IKPU entry on tasnif.soliq.uz.
   - `PAYME_VAT_PERCENT`: your VAT rate, for example `0` if you are not a VAT payer. Ask your accountant.
   Payme puts these on the fiscal receipt it issues.

**Click** (docs: docs.click.uz/shop-api):
1. Ask Click for a **test service** and give them the Prepare URL `…/payments/click/prepare` and the Complete URL `…/payments/click/complete`.
2. Add `CLICK_SERVICE_ID`, `CLICK_MERCHANT_ID` and `CLICK_SECRET_KEY` to the website service.
3. Use Click's Playground (docs.click.uz/testing/playground) to send Prepare and Complete requests. Click has no separate sandbox website, so **use only the test service's keys**. A live service's keys would take real money even though PlagIQ says "test".

**Uzum Bank** (Merchant API; docs: developer.uzumbank.uz/merchant):
1. Ask Uzum Bank's business team for a **test service** for the Merchant API. They issue the service ID (`serviceId`) and a login and password for the webhooks.
2. Give them the five webhook addresses: `https://plagiq-production.up.railway.app/payments/uzum/check`, `…/create`, `…/confirm`, `…/reverse` and `…/status`. The payment attribute is **`account`**, which is the PlagIQ order number.
3. Add `UZUM_SERVICE_ID`, `UZUM_LOGIN` and `UZUM_PASSWORD` to the website service. Add `UZUM_SERVICE_NAME` too: the service name as it appears in the Uzum Bank app, which PlagIQ shows the user.
4. Uzum has **no payment link**. After "Pay with Uzum Bank", PlagIQ shows the order number and these steps: open the Uzum Bank app → Payments → find the service → enter the order number. Uzum also has no public sandbox, so as with Click, **use only the test service's credentials**.
5. Rules from Uzum's specification, all implemented:
   - every webhook needs HTTP Basic auth;
   - errors are HTTP 400 with `errorCode` 10001–10018;
   - a transaction not confirmed within 30 minutes is treated as failed, and the order can then be paid again;
   - a confirmed top-up can be reversed only while its balance is unspent (otherwise 10017).

**Optional:** `TOPUP_MIN` (default 1000), `TOPUP_MAX` (default 2000000), `SELLER_NAME` and `SELLER_TIN` (shown on receipts).

**What is stored:** orders, provider transactions and the balance ledger, in the accounts database on the website's `/data` volume (it is included in the accounts backup on /admin). **Deleting an account deletes all of its payment records** (orders, Payme and Click transactions, ledger), and any unspent balance is lost; the confirmation says so and shows the balance. Deletion is refused while a payment is still in progress (an open Payme transaction, or a Click payment prepared in the last hour), so a provider never confirms money for a deleted order. After deletion, Payme's statement (GetStatement) no longer lists that user's transactions; keep your own exports from the Payme and Click cabinets if your accountant needs them. Card details never reach PlagIQ.

**Turning it off:** set `PAYMENTS_ENABLED=0` (or delete it). Checks become free again; balances stay stored.

## Part 5a — Owner dashboard (/admin)

Website service → **Variables**: add `OWNER_TELEGRAM_IDS` = your Telegram user ID (get it from @userinfobot; separate several IDs with commas). Sign in with Telegram, then open `https://<your site>/admin`. Anyone else gets "Not found".

The dashboard shows:
- **Setup checklist:** each feature as ✅ on, ❌ missing (with the exact setting and DEPLOYMENT section), or ➖ optional.
- **Library:** counts by source, the engine's disk use (a red warning above two thirds full) and every automatic import's last run and problem.
- **Usage:** accounts and checks.
- **Backup downloads.**

No secrets are shown, only whether each setting is present.

## Part 5b — Backups

The library's imported texts can be downloaded again from their sources. **Students' and universities' archive deposits and the accounts cannot.**
1. **Download** both from `/admin` → Backups, weekly to start with. Keep them encrypted, for example in a password-protected 7-Zip archive, somewhere only you control. They contain personal data.
2. **Restore** deposits into an engine with: `python corpus.py --db /data/corpus.sqlite3 --restore-archive plagiq-archive-YYYYMMDD.jsonl.gz` (engine shell). To restore accounts, put the downloaded file back on the website volume at `/data/plagiq-users.sqlite3`.
3. **Railway volume backups:** in Railway, open each volume (engine and website), go to **Backups**, and turn on a schedule (daily or weekly) if your plan offers it. Check what your plan includes.

## Part 5c — Measure accuracy on your own library

`evaluation/REAL_RESULTS.md` reports how much copying PlagIQ finds on real texts, and how often it flags text that was not copied (measured in November 2026 on 215 real open-licence texts). To measure **your live library**, open the engine shell (Railway → engine → ⋮ → Shell) and run:
```bash
python evaluation/real_eval.py --db /data/corpus.sqlite3 --out /data/REAL_RESULTS.md
```
The script sets aside 25 library texts per language as "articles PlagIQ has never seen" and hides them from every search during the test. It copies real passages in seven ways (unchanged, reformatted, Latin↔Cyrillic, look-alike letters, light and heavy edits, reordered), and prints a table. It changes nothing in the library and takes a few minutes. Re-run it after large imports and keep the results; universities will ask for them.

**Boilerplate filter:** a seven-word sequence found in 3 or more different library texts (journal headers, licence lines, website banners) is not counted as a match. `BOILERPLATE_SOURCES` (engine and website) changes the number; `0` turns the filter off.

## Part 5d — Library health, source check and the Uzbek relabelling import

### What /admin now shows (owner only)
Under **Library details**:
- **Searchable full-text coverage** per language:
  - indexed texts, full texts (300+ words), short texts, searchable words, and the number of seven-word fingerprints;
  - everything in this table is held in full by the engine. OpenAlex results that have only metadata or an abstract are never stored there; in academic checks they are compared by their abstract only (or not at all when only metadata exists), and reports say so.
- **By collection and language**, with a warning when Wikipedia is more than half of the library.
- **By provider and rights basis:** OpenAlex, journal OAI feeds, CyberLeninka, Wikipedia, CORE, author and university deposits, and owner manifests; then open licence, public domain, written permission and deposits.
- **Indexing freshness:** newest and oldest import date per collection, and how many texts were added in the last 7 and 30 days.
- **Limits:** `CORPUS_MAX_SOURCES` and `ARCHIVE_MAX_SOURCES` usage. For Wikipedia, the actual count per language next to `WIKIPEDIA_MAX_ARTICLES`; the cap stops *new* imports only, so a language can hold more than the cap if it was lowered later.
- **Import history:**
  - last attempt, **last success**, the error and when it happened, and the main failure reasons of staged works (for example "download is not a PDF ×12");
  - a disk error from an earlier run is marked "at that time; there is enough free space now" when the disk is fine today, so old failures are not mistaken for a current outage.

### Source check (DOI or title)
At the bottom of /admin, enter the DOI (or at least 8 characters of the title) of a source you know is licensed and indexed, and press **Check source**. PlagIQ shows:
- whether the source is in the library, with its collection, provider, rights basis, word count and import date;
- whether its fingerprints exist ("searchable");
- whether a search finds it: from 60 words taken from the middle of the indexed text, or from a passage you paste;
- its rank, and how many other sources also matched.

The check never shows any source text: not the sampled words, and certainly not the text of an unpublished deposit. It runs as one search, queued like any other.

**Recommended routine:** after every large import, check three known sources, one each in Uzbek, Russian and English, and keep a note of the results.

### Uzbek works that OpenAlex labels Russian (engine)
With `uz` in `AUTO_IMPORT`, each run now also lists open-licence works that OpenAlex labels **Russian** but whose authors are at **Uzbek institutions**:
- titles containing the Russian-only letter "ы" are skipped before any download;
- each downloaded text's language is detected; Uzbek texts are imported as Uzbek, and the permission record notes the correction;
- Russian texts are imported only if `ru` is also in `AUTO_IMPORT`; otherwise they are refused with a stated reason;
- licence rules are the same as for every OpenAlex import: CC BY, CC BY-SA, CC0 and public domain are imported; NC and ND go to the review sheet.

/admin shows this pass as **uz-from-ru**. Settings, on the **engine**:
- `AUTO_IMPORT_UZ_FROM_RU`: default on; `0` turns it off;
- `AUTO_IMPORT_UZ_FROM_RU_BATCH`: default 100 works per run;
- `AUTO_IMPORT_UZ_FROM_RU_MAX_COST`: default 0.05 USD per run.

### Academic searches: chapters and duplicates (website)
- Long documents get more OpenAlex excerpts (3 up to 3,000 words, then one more per 3,000, at most `OPENALEX_MAX_QUERIES`, default 8).
- When the text has headings ("1. Kirish", "2-BOB", "ГЛАВА 2", "Introduction", "Xulosa"…), the excerpts are taken **one per chapter**. A table of contents and very short sections are merged with the next part.
- Results are **de-duplicated** by DOI and by title (for example a 2001 article and its 2017 reprint), keeping the copy that has an abstract.

### Library composition and disk
In November 2026 Wikipedia made up about 80% of the library (1,824 of 2,288 texts). For academic checks, journals, CyberLeninka and OpenAlex works matter more. With a larger engine volume:
1. **Back up first:** /admin → Backups (archive deposits and accounts). Turn on Railway volume backups.
2. **Enlarge the volume**, for example to 5 GB: Railway → engine → the volume → Settings. Live resizing needs a paid plan; never delete or replace the volume.
3. **Then, on the engine**, a starting point (raise step by step while /admin shows the disk under two-thirds full):
   - `CORPUS_MAX_SOURCES=5000`
   - `AUTO_IMPORT=uz` (add `ru` if you want Russian OpenAlex works too)
   - `AUTO_IMPORT_UZ_JOURNALS=1`
   - `AUTO_IMPORT_CYBERLENINKA=1`
   - leave `AUTO_IMPORT_WIKIPEDIA` empty, which stops new Wikipedia imports and keeps the existing articles
   - keep `AUTO_IMPORT_MAX_COST` set.
4. **Verify:** after the next run, /admin shows a new "Last success", growing counts outside Wikipedia, and source checks that pass.

### Test on staging before production
The repository cannot deploy for you. Use a separate Railway **staging environment**, so nothing touches the live site, the live library or real accounts:
1. Railway → project → environment menu (top left, "production") → **New Environment**. Choose to duplicate production so that the website and engine services are copied.
   - Staging gets its **own** variables and its **own, empty** volumes.
   - Railway may label these menus differently; look for "Environments".
2. In the staging environment, on **both** services:
   - set a **new** `ENGINE_API_KEY`, different from production and identical on staging's website and engine;
   - check `ENGINE_URL` points to staging's engine (`http://${{engine.RAILWAY_PRIVATE_DOMAIN}}:8000`).
3. On staging's website:
   - remove or leave empty `PAYMENTS_ENABLED`, `TELEGRAM_BOT_REPLIES` and the Payme/Click/Uzum keys;
   - leave `TELEGRAM_BOT_TOKEN` out, or use a separate test bot: one bot can have one sign-in domain and one webhook, and they belong to production;
   - set `SESSION_SECRET` (40+ random characters), so sign-in by **email and password** works;
   - set `PUBLIC_ORIGIN` to the staging address;
   - to open /admin on staging, a separate test bot and `OWNER_TELEGRAM_IDS` are needed, because email accounts can never open /admin.
4. On staging's engine:
   - `CORPUS_DB=/data/corpus.sqlite3` with a small volume;
   - `AUTO_IMPORT=uz`, `AUTO_IMPORT_BATCH=20`, `AUTO_IMPORT_MAX_COST=0.05`.
   - Never copy production's archive deposits to staging: they are other people's unpublished works.
5. Point staging at the pull-request branch: Railway → staging → each service → Settings → Source → branch. Deploy.
6. **Verification on staging** (tick each):
   - [ ] `/health` → `corpus_engine_configured: true`; `/api/library` → `available: true`.
   - [ ] /admin → "Private engine connected" ✅; **Library details** shows coverage, collections, providers, freshness, limits and import history.
   - [ ] After the first import run, "uz" and "uz-from-ru" appear under Import history with a "Last success" time.
   - [ ] **Source check** on an imported work's DOI → "✅ found (rank 1 …)" and "(searchable)". A made-up DOI → "Not in the library".
   - [ ] An academic check of a long document with chapters: the result line shows more searches than 3, and the PDF still shows the certificate, scope and evidence pages as before.
   - [ ] The short-text check without sign-in, email sign-in, the archive, and the PDF/QR all work as before. Payments stay off.
7. Only then merge the pull request (production deploys from `main`), redeploy the **engine** too, and repeat the first four checks on production.

### Railway variables used by this change
| Service | Variable | Default | Purpose |
|---|---|---|---|
| engine | `AUTO_IMPORT_UZ_FROM_RU` | on when `uz` is in `AUTO_IMPORT` | Uzbek works labelled Russian (0 = off) |
| engine | `AUTO_IMPORT_UZ_FROM_RU_BATCH` | 100 | works listed per run in that pass |
| engine | `AUTO_IMPORT_UZ_FROM_RU_MAX_COST` | 0.05 | USD cap per run for that pass |
| engine | `ENGINE_QUEUE_SECONDS` | 60 | how long a search (or source check) waits for its turn |
| website | `OPENALEX_MAX_QUERIES` | 8 | upper bound of OpenAlex excerpts per academic check (3–12) |
| both | `ENGINE_API_KEY`, `ENGINE_URL` | — | unchanged: the private-network connection and its key |

No payment, sign-in or report variables change.

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
CHROMIUM_PATH=/path/to/chromium python tests/browser_websearch.py
```
