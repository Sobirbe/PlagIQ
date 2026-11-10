# PlagIQ — multilingual similarity pilot

**A real, intentionally limited pilot**, not an institutional plagiarism detector.

The service compares one uploaded manuscript to **one uploaded reference document or pasted source text**. It performs normalized seven-token-window matching and reports verified overlap **only with that reference**. It can normalize Uzbek Latin/Cyrillic script variants (choose Uzbek for the proper language mapping), and can read English and Russian. It does **not** search the Internet, university collections, journals, or other third-party databases. It does **not** detect AI writing. No payments, accounts, or institutionally certified results exist yet.

## Local run
Install Python 3.12 and execute:

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Visit http://127.0.0.1:8000 ; API docs at /docs.

Run tests: `pytest -q`.

## Railway pilot deployment
1. Railway → New project → Deploy from GitHub repo → `Sobirbe/PlagIQ`.
2. Railway builds the included Dockerfile and starts the FastAPI server.
3. In the service settings, generate a public domain.
4. Open `https://<your-railway-domain>/health` to see `{"status":"ok"}`.
5. Keep the link **private among testers**. There is no authentication or production-grade abuse protection.

The web service is a **public demo** once a public domain is generated; do not upload confidential, unpublished manuscripts there. Files are read into memory, and raw documents are not intentionally saved to disk, but processing still occurs on the hosting provider's servers. Results remain in a short-lived in-memory cache until the app restarts or entries are evicted; it is not suitable for sensitive data.

## What the scoring means
- The score is the fraction of non-overlapping manuscript tokens covered by matching **7-token sequences** found in the provided reference.
- It does not imply plagiarism, authorship, exhaustive source coverage, or AI-generated writing.
- Language and script normalization can create false positives and false negatives; this is an exploratory baseline.
- No references are searched automatically. The PDF states its exact limited scope.

## Next milestones
1. Authentication, private persistence, rate limiting, storage retention and deletion.
2. Permissioned corpus indexing and published database connectors (with clear source coverage).
3. Benchmarked multilingual retrieval, paraphrase and translation candidate identification.
4. AI-writing assessment **only** after separate language-specific accuracy validation.
5. Payment-provider sandbox integrations, security review and production hosting.

No merchant keys or credentials should be committed to GitHub.
