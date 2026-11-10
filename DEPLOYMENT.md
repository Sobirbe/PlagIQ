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

**Upgrading an existing engine:** v0.5 improves Unicode handling (for example `ё`/`е` and decomposed letters from PDFs). On first start, the engine automatically re-fingerprints the texts already stored in its database. It downloads nothing. With 100 sources this takes seconds.

## Part 3 — Add publications you are allowed to index

A technical administrator must run this *inside the engine service*, with its `/data` volume attached. Railway's local CLI may not run inside the remote container, so confirm that first.

1. For each publication, keep written proof that you may store it in full, index it and show short excerpts. Being free to read online, or appearing in OpenAlex, is **not** permission.
2. Save each text as a UTF-8 `.txt` file. Create a private `manifest.json` next to the files:

   ```json
   [{"text_file": "article.txt", "title": "Exact title", "authors": "Author names", "year": 2025,
     "url": "https://publisher.example/article", "language": "uz",
     "rights_basis": "written_permission", "permission_reference": "Agreement #12, 2026-03-01"}]
   ```

   `language` is `uz`, `ru` or `en`. `rights_basis` is `written_permission`, `open_license` or `public_domain`. The importer checks that these fields are filled in, but **a person must check that they are true.**
3. Pause scans, then run: `python corpus.py /private/import/manifest.json --db /data/corpus.sqlite3`
4. Limits: 300,000 characters per text and 100 sources in total (pilot). Importing the same text again replaces it. To withdraw a source, rebuild from a manifest without it, or delete it by its SHA-256 identifier. Never delete the volume without a tested backup.

## Part 4 — Checks before you approve the pull request

Use only harmless sample text. Tick each item:

- [ ] In UZ, RU and EN, on a phone and a computer, the page fits the screen with no sideways scrolling.
- [ ] *Compare with my own source*: paste the same 10-word sentence as the document and the reference. The result is 100% and labelled in **words (tokens)**.
- [ ] Upload a 3-word file. A translated "at least 7 words" message appears, not 0%.
- [ ] Upload a `.txt` file saved in Windows-1251. A translated "save as UTF-8" message appears.
- [ ] *Academic discovery*: the consent text names OpenAlex. If nothing usable comes back, the result says **Not evaluated** with a reason.
- [ ] *Corpus* (if set up): import one synthetic text you wrote, then check it. The correct title, author, year, rights basis and passage appear, labelled in **characters**. A language with no indexed sources shows **Not evaluated**. The indexed-sources row shows counts per language.
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
