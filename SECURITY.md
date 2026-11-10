# Security and privacy — what is done, and what is still needed

**Short version for the owner:** PlagIQ v0.5 is suitable for a *supervised private pilot with non-confidential or synthetic documents*. It is **not yet approved for confidential, unpublished manuscripts.** Part B lists what must be in place first. None of Part B can be completed by code alone.

## A. Controls implemented in this repository (tested)

| Area | What PlagIQ does | Evidence |
|---|---|---|
| Manuscript never indexed | The engine only *reads* the source index. Manuscripts are fingerprinted in memory and never written to SQLite. | `test_search_never_writes_manuscript` checks the database file bytes. |
| Manuscript never on disk (web app) | Uploads are held in memory (multipart spool raised above the 5 MB request cap). Before v0.5, files over 1 MB were spooled to a temporary file. | `test_uploads_are_kept_in_memory` |
| No manuscript in caches or reports | The report cache stores only the summary (file name, SHA-256, counts and **source** excerpts), never the manuscript or highlighted text. The PDF is built from that summary. Responses are `Cache-Control: no-store`. No browser storage is used except the chosen interface language. | `test_report_ownership_expiry_delete`, code review |
| No manuscript echoed in errors | Validation errors (HTTP 422) no longer reflect submitted form values. | `test_validation_error_does_not_echo_input` |
| Report ownership and expiry | A random HttpOnly, SameSite=Strict session cookie must accompany report download and deletion. The report URL alone returns 404. Reports expire after 15 minutes, are swept every 30 s, and are capped at 50 in memory. Restarts erase them. | `test_report_ownership_expiry_delete` |
| Cross-site requests | Foreign `Origin` and `Sec-Fetch-Site: cross-site` POST/DELETE requests are rejected. The SameSite=Strict cookie is also required. | `test_cross_site_and_large_request` |
| Request and parser bounds | 5 MB per request (enforced before parsing), 2 MB per file, 300,000 extracted characters, 40 PDF pages, DOCX entity resolution disabled, and DOCX limits of ≤1,000 members and ≤20 MB expanded. Extraction runs in a worker thread so one large file cannot freeze the site. | `test_invalid_uploads_have_codes`, `test_docx_expansion_rejected` |
| Concurrency | At most `SCAN_CONCURRENCY` (default 2) scans at once on the website. Further requests get a translated "busy" message. Uvicorn caps open connections at 64. The engine processes one scan at a time and returns "busy" otherwise. | `test_rejects_when_scan_slots_full` |
| Rate limit behind Railway | 12 scans per client per hour (in memory). With `TRUSTED_PROXY_HOPS=1`, the client address is the right-most `X-Forwarded-For` entry added by Railway, which clients cannot forge. Previously every user shared the proxy's single allowance. | `test_rate_limit_uses_trusted_proxy_hop`, `test_proxy_headers_ignored_by_default` |
| Private engine | Bearer secret of at least 32 characters, compared in constant time. The URL allowlist covers only internal hostnames, uses HTTP inside Railway's private network, follows no redirects and ignores environment proxies. Job paths are validated. | `test_private_service_requires_auth`, `test_engine_paths_reject_traversal` |
| Untrusted engine output | All engine fields are type-checked, bounded and rebuilt from an allowlist. The percentage is recomputed from merged spans and never taken from the engine. | `test_summary_sanitizes_untrusted_fields`, `test_summary_rejects_malformed` |
| Malicious source text | All titles, excerpts and file names are inserted as text (never HTML) in the browser and escaped in the PDF. The highlighted manuscript is server-escaped and contains only `<mark>` and `<br>` tags. Control characters and paths are removed from file names. | `test_markup_escapes_user_text`, `test_filename_control_characters_and_paths_removed` |
| Unsafe URLs | Only `https://` source links are rendered, and they open with `noopener noreferrer`. PlagIQ never fetches user-supplied URLs. OpenAlex responses are capped at 5 MB. | code review |
| Headers | CSP, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, no server banner, and API docs disabled. | `test_private_service_requires_auth` |
| Third-party processing consent | OpenAlex (a ≤14-word excerpt) and private-corpus checks each require their own consent tick, which resets whenever the mode changes. | browser tests |
| Rights-gated corpus | Import requires citation, language, an HTTPS URL, a rights basis and a permission reference. There is no automatic downloading, and the pilot is capped at 100 sources. | `test_rights_required_and_idempotent` |

## B. Required before confidential manuscripts (not done; needs infrastructure, policy or contracts)

1. **Individual accounts.** The optional shared Basic login is a gate, not an account system. Per-user sign-in, revocation and audit logs are needed.
2. **Perimeter controls.** Add request-rate, body-size and concurrency limits at the edge (Railway or a CDN/WAF). The in-memory limit resets on restart and applies per replica.
3. **Parser isolation.** PDF/DOCX parsing runs in the web process. For untrusted uploads at scale, run extraction in a separate sandboxed worker with CPU/memory/time limits, and keep PyMuPDF patched.
4. **Malware scanning** of uploads if files are ever stored or forwarded. Today they are not stored.
5. **Encryption at rest and backups** for the engine volume: Railway volume encryption, an encrypted backup schedule and restore drills. The corpus contains licensed texts.
6. **Logging hygiene.** Disable request-body capture in any APM or error-tracking tool. Keep access logs short-lived. PlagIQ itself does not log manuscript content.
7. **Legal.** Data-processing agreements, a privacy notice, and a review against Uzbekistan's personal-data law (Law No. ZRU-547 and its data-localization requirements) by qualified counsel before public launch. OpenAlex's own retention cannot be revoked by deleting a PlagIQ report.
8. **Operations.** Separate staging and production, run one replica per service (state is in memory), have an incident-response contact, and document a deletion policy covering logs and backups.

## C. What PlagIQ does not claim

- No AI-writing detection is performed. The result is always "Not performed".
- No paraphrase, translation or web-wide detection. Matching is exact seven-word overlap after normalization, including between Uzbek Latin and Cyrillic.
- Quotations and bibliography are **not** excluded, because no tested exclusion method exists yet. Reports state this.
- The PDF is a scope-limited evidence report, not an accredited originality certificate.
- No payment processing is included or changed.

Report vulnerabilities privately to the repository owner; do not open a public issue with exploit details.
