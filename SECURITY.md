# Security and privacy boundaries

This is a bounded private-pilot candidate, not approval to process confidential manuscripts in production.

## Implemented

- Private internal engine address allowlist; no user-controlled URL fetches or public ingestion API.
- Shared engine bearer secret (minimum 32 characters), constant-time comparison, no redirect following or environment proxy forwarding for private manuscript traffic. Trust the private Railway network; external service traffic requires a separate reviewed TLS configuration.
- Optional HTTPS browser private-pilot Basic login, with fail-closed partial settings. Separate random browser session cookie (HttpOnly, SameSite=Strict, Secure with HTTPS origin) restricts report download/deletion to its submitting browser. The random report URL alone is insufficient.
- 15-minute frontend report validity; background cleanup every 30 seconds while running, lazy cleanup on report/check access, capacity 50 summaries. Frontend cache omits manuscript/highlight HTML. Engine reports expire after 180 seconds, are swept every 30 seconds and deleted on retrieval. Reports and rate limits disappear at restart. Source excerpts in reports are from the authorized corpus. No manuscript goes into SQLite or source index. Runtime libraries/OS can temporarily hold bytes in memory or multipart temporary files; this is not a secure-erasure guarantee.
- 5 MB total raw request cap before multipart parsing, 2 MB per file, 300,000 extracted characters, 40 PDF pages, DOCX expanded-size/member bounds. Twelve scan requests per client IP/hour in memory; do not blindly trust forwarded IP headers.
- Origin/cross-site request checks, non-cacheable responses, frame denial, MIME sniffing prevention, referrer suppression, escaped user/source text and bounded evidence fields.
- Source fingerprint index requires citation, language, rights basis and permission evidence reference. No bundled publisher corpus or rights inferred from public URLs.

## Remaining gates for real manuscripts

An owner and technical administrator must establish individual user access/revocation, rate/concurrency controls at the trusted ingress, durable per-user report policy if persistence is needed, encrypted volumes/backups and managed key access, private-network boundaries, incident response, provider/data-processing agreements and a deletion policy that includes logs/backups. Disable body capture in Railway integrations/APM and retain only necessary operational logs. Separate staging from production. Parsing untrusted PDF/DOCX still needs resource isolation/time limits at the container boundary and security updates; byte/page checks are not a complete parser sandbox.

The engine is single-worker and serializes scans; deploy one replica. SQLite source indexing and in-memory reports are suitable for small supervised trials only. No scale/accuracy claim has been measured. The shared reviewer password does not establish manuscript ownership across devices or provide account recovery. HTTPS is required for browser access; Basic auth must not be used over public HTTP. The operator must keep the engine private and enforce infrastructure access controls.

OpenAlex receives a short excerpt only with explicit consent and has its own retention policy; that cannot be revoked by deleting a PlagIQ report. Scope reports are evidence summaries, not signed or accredited originality certificates. AI detection is not performed. No production payment processing is included or modified.
