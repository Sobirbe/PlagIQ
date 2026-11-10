# PlagIQ · Academic similarity pilot (v0.2)

PlagIQ is a **working experimental** multilingual source-comparison website deployed from GitHub to Railway.

Live test site: https://plagiq-production.up.railway.app

## What actually works

- Uzbek (Latin/Cyrillic), Russian, and English language modes.
- TXT, DOCX, and readable PDF uploads, up to 2 MB and 40 PDF pages.
- **Mode 1:** compare your manuscript with one supplied reference document or pasted text.
- **Mode 2:** with explicit consent, send a short search excerpt to [OpenAlex](https://openalex.org/), discover academic publication metadata and available abstracts, then compute exact seven-token matches **against returned abstracts only**.
- Clearly display source title, URL, whether abstract/metadata only was available, query status, and bounded overlap percentage.
- Create downloadable scope-disclosure PDF.
- Basic scan throttling (12 requests per IP per hour in memory), upload limits, and test suite.

**Crucial limitations:** This is not an exhaustive plagiarism check. OpenAlex metadata discovery is not full-text search. A zero match cannot establish originality. Matching seven normalized words is not proof of plagiarism. The service cannot detect paraphrases, translated copying, or AI-generated writing. OpenAlex queries can return zero relevant results, fail or be rate limited. Uzbek Cyrillic normalization is experimental and may miss nuances.

## Privacy and safety

In academic-discovery mode, the server sends a **short excerpt** to OpenAlex only after explicit consent. Never upload confidential or unpublished sensitive manuscripts to the public pilot.

The site has no accounts, encryption-at-application-level, persistent ownership checks, or production data protections. Scan summaries remain briefly in server memory, and pilot PDF report URLs are not authenticated. There is no production accreditation or payment integration.

## Run locally

Python 3.12 recommended:

```bash
pip install -r requirements.txt
pytest -q
uvicorn app:app --reload
```

Open http://localhost:8000 and http://localhost:8000/docs.

Optional `OPENALEX_API_KEY` server-side environment variable can be set in Railway if required by the provider. Never put API keys in GitHub or browser code.

## Railway deployment (nontechnical owner)

1. Open https://railway.com/dashboard.
2. Open your PlagIQ project linked to `Sobirbe/PlagIQ`.
3. Railway deploys GitHub pushes using the supplied Dockerfile and `railway.json`.
4. Check **Deployments** for the newest deployment status.
5. Check `/health` and open the homepage. Version should be `0.2`.
6. Test with an ordinary, nonconfidential sample manuscript.
7. In the form's second panel, select **Academic discovery / Ilmiy qidiruv**, consent, then scan.
8. Inspect the report's **Scope of Examination**, academic URLs, and whether matches refer only to abstracts.

## Code structure

- `app.py`: FastAPI, upload handling, text matching and pilot PDFs.
- `discovery.py`: bounded OpenAlex metadata search and reconstructed abstracts.
- `index.html`: UZ/RU/EN client interface and results.
- `test_app.py`: automated regression tests.
- `.github/workflows/test.yml`: Python tests on GitHub pushes.

## Roadmap

1. Secure accounts and proper per-user report access and retention policies.
2. Authorised, licensed full-text indexing for Uzbek/Russian/English.
3. Better language normalization, citation filtering, paragraph alignment, and evaluation on real benchmarks.
4. Separate, calibrated AI-writing assessment only after language-specific validation.
5. Verifiable professional certificate; then Payme/Click sandbox and production payment integration after merchant onboarding.

Do not claim a plagiarism-free percentage or AI-authorship verdict from this pilot.
