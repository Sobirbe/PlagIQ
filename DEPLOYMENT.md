# Railway setup for the website owner

This branch is a review candidate. It does not deploy itself to your production environment. Review the pull request first; keep your existing production service attached to `main`. Do not merge until staging checks pass. Payments are outside this change.

## 1. Create a safe test environment

1. In Railway, open the project connected to `Sobirbe/PlagIQ`.
2. Duplicate the environment as **Staging**. Ensure it has no production database, payment keys or customer documents. Railway's interface labels can change.
3. In the staging website service's source settings, select this pull request's development branch. Keep the existing Dockerfile build. The frontend start command remains the Dockerfile default and honors Railway's `PORT`.
4. Add a staging domain. In **Variables**, set `PUBLIC_ORIGIN` to exactly that HTTPS origin, such as `https://your-staging-domain` (no trailing slash).
5. Set `PRIVATE_PILOT_USER` to your chosen reviewer username and `PRIVATE_PILOT_PASSWORD` to a unique password of at least 16 characters. The browser will prompt for these. Use Railway Variables, never GitHub or chat. Without these variables the public sample pilot is available; a partial/short configuration fails closed.
6. Redeploy staging. Visit `/health`: version must be `0.4`. It is intentionally public and contains no secrets. The homepage should ask for your staging credentials.

## 2. Add the private engine

1. Add another service from the same repository/branch, named **engine**, in the same staging environment.
2. Use the same Dockerfile. Override its **Start Command** with:

   ```sh
   sh -c 'uvicorn engine_service:app --host :: --port ${PORT:-8000} --workers 1'
   ```

   IPv6 binding supports Railway private networking; verify your environment's IPv4/IPv6 settings if using a different host. Keep **one worker and one replica** for both services: transient reports and throttling are in process memory.
3. **Do not generate a public domain for the engine.** Remove any inherited domain. Only the website should be public. Do not put `PRIVATE_PILOT_USER` / `PRIVATE_PILOT_PASSWORD` on the engine; they are frontend browser access settings.
4. Generate a random service secret of at least 32 characters in a password manager. Set **the same** `ENGINE_API_KEY` value on both services via Railway Variables. Do not commit it. Rotation requires updating both services and redeploying them together.
5. Add a Railway persistent volume to the engine at `/data`; set `CORPUS_DB=/data/corpus.sqlite3`. Source files, manifests, the database and volume backups are private owner data.
6. On the website service set `ENGINE_URL=http://engine.railway.internal:8000`, adjusting the hostname and port to the engine's actual private service name and listening port. Set the engine's `PORT=8000` if needed. Do not use the public website URL. The adapter permits only the documented internal hostnames and Railway internal DNS.
7. Optional website setting: `ENGINE_TIMEOUT=120` (15–180 seconds). Keep `OPENALEX_API_KEY` only on the website if your OpenAlex account requires it.
8. Redeploy engine, then website. `/health` on the website should show `corpus_engine_configured: true`. This means URL/key settings are present, **not** that connectivity or corpus coverage was verified. A synthetic corpus test below verifies the complete path.

## 3. Import publications you are allowed to index

Ask a technical administrator to perform the initial import inside the engine service/container with the persistent volume attached. Do not upload a corpus to this public GitHub repository. Railway's local CLI shell may only set environment variables; it does not necessarily run inside the remote container or attach its volume. Verify the remote execution/volume path before importing.

Prepare local UTF-8 `.txt` full texts from lawful copies. For each source verify that its license/permission permits full-text storage, indexing and displaying short matching excerpts. Public accessibility or an OpenAlex record alone is not permission. Keep the permission evidence.

Create a private JSON manifest alongside those local files:

```json
[
  {
    "text_file": "article.txt",
    "title": "Actual publication title",
    "authors": "Actual author names",
    "year": 2025,
    "url": "https://publisher.example/actual-article",
    "language": "uz",
    "rights_basis": "written_permission",
    "permission_reference": "Private permission record and date"
  }
]
```

Use language `uz`, `ru` or `en`. Rights basis must be `written_permission`, `open_license` or `public_domain`; record the actual license/authorization reference. The importer checks that fields are present; **a human must verify their truth and scope**. There is no automatic downloading or paywall bypass.

Run in the engine container:

```sh
python corpus.py /private/import/manifest.json --db /data/corpus.sqlite3
```

The importer accepts up to 300,000 characters per text and 100 indexed sources in total. Identical text imports replace that source (SHA-256 identity); the index is a bounded pilot, not a large-scale academic search cluster. Stop scans during imports. To withdraw a source, the administrator can delete its SHA-256 entry from `sources` using `corpus.connect()` (foreign-key cascade removes fingerprints), or rebuild the database from the remaining authorized manifest. Remove withdrawn files/backups according to the permission terms. Never delete a live volume without a reviewed backup/recovery plan.

## 4. Review before release

- In UZ, RU and EN, check a harmless sample at desktop and phone widths. Reference mode should work with engine variables absent.
- In corpus mode, consent must describe the private engine, not OpenAlex. First use a small owner-created synthetic text imported with its rights record. Matching that text should produce observed overlap and the correct citation. An empty corpus should say **Not evaluated**. Confirm per-language counts.
- In academic mode, consent should describe OpenAlex. No abstracts/provider failure should say **Not evaluated**, not 0% originality.
- Download the PDF. Check Unicode, method, source evidence, scope, page numbering and 15-minute expiry. Copy its URL to an incognito browser: it must return 404. Delete a report and verify its URL stops working.
- Confirm engine has no public domain, source files/keys are absent from GitHub and browser code, request-body logging/APM capture is disabled, and the private pilot uses HTTPS.
- Ask an administrator to add perimeter request/rate limits and review access, storage encryption, backup retention and deletion before any real manuscript trial. The current shared pilot login is not a production account system. Do not accept confidential manuscripts until the controls and agreements in SECURITY.md are fulfilled.

## 5. Release and rollback

After review, merge the PR only when you intend the existing main-linked Railway service to deploy. Reapply the reviewed environment variables and private-engine setup in the intended environment. No production deployment has been performed by this change.

If the new release fails, use Railway **Deployments** to redeploy the last known good frontend deployment. Remove `ENGINE_URL` from the frontend to disable corpus mode while keeping reference/academic workflows. Keep the authorized corpus volume intact; coordinate schema changes before rolling engine code back. Reports are ephemeral and will be lost on restart/deploy; tell reviewers to download new reports.
