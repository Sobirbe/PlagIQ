"""Stage open-licence full texts found through OpenAlex or CORE (owner-run; never indexes).

Only works whose licence is CC BY, CC BY-SA, CC0 or public domain are staged;
NC/ND/unknown licences go to the review sheet. OpenAlex grants no rights to
the full texts it caches, so the licence of each work is always checked.
CORE texts are accepted only when the work's DOI resolves in OpenAlex to an
accepted licence. Output matches harvest.py: manifest.json, review.csv,
files/ and a state file for incremental runs. Import with corpus.py.

Examples:
  OPENALEX_API_KEY=... python oa_import.py openalex --language uz --out /data/staging/openalex-uz --download
  OPENALEX_API_KEY=... python oa_import.py openalex --country UZ --since 2020 --out /data/staging/uz-inst --download
  CORE_API_KEY=... OPENALEX_API_KEY=... python oa_import.py core --query "language.code:ru" --out /data/staging/core-ru

Costs: OpenAlex bills per call (see PRICES below; check help.openalex.org for
current prices). --max-cost-usd stops the run before the estimate is exceeded.
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.error
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from corpus import UZ_ONLY, detect_language
from urllib.parse import urljoin
from harvest import MAX_PAGE, Fetcher, HarvestError, MAX_PDF, _PageMeta, classify_licence

# classify_licence() labels -> OpenAlex licence strings
LICENCE_CODES = {"by": "cc-by", "by-sa": "cc-by-sa", "zero": "cc0", "mark": "public-domain"}

OPENALEX = "https://api.openalex.org"
OPENALEX_CONTENT = "https://content.openalex.org"
CORE = "https://api.core.ac.uk/v3"
UNPAYWALL = "https://api.unpaywall.org/v2"
# Estimated USD per call as published by OpenAlex in 2026 (list/filter, search, content file, single lookup).
PRICES = {"list": 0.0001, "search": 0.001, "content": 0.01, "single": 0.0}
LICENCES = {"cc-by": ("open_license", "CC BY"), "cc-by-sa": ("open_license", "CC BY-SA"),
            "cc0": ("public_domain", "CC0"), "public-domain": ("public_domain", "Public domain")}
REVIEW_LICENCES = {"cc-by-nc", "cc-by-nc-sa", "cc-by-nd", "cc-by-nc-nd"}
LANGS = ("uz", "ru", "en")
MAX_TEXT = 1_000_000


class Budget:
    def __init__(self, limit):
        self.limit, self.spent, self.calls = limit, 0.0, {}

    def charge(self, kind):
        cost = PRICES[kind]
        if self.spent + cost > self.limit + 1e-9:
            raise HarvestError(f"cost limit ${self.limit:.2f} reached (estimated ${self.spent:.3f} spent)")
        self.spent += cost
        self.calls[kind] = self.calls.get(kind, 0) + 1


def bearer(key):
    """OpenAlex key as an Authorization header (never in the URL, so it stays out of logs)."""
    return {"Authorization": "Bearer " + key} if key else {}


def licence_of(value):
    """Normalise an OpenAlex/Unpaywall licence string ('cc-by', 'cc-by-nc', ...)."""
    value = (value or "").strip().lower().replace("_", "-")
    return {"cc-zero": "cc0", "pd": "public-domain"}.get(value, value)


def decide(licence):
    if licence in LICENCES:
        return "accept"
    if licence in REVIEW_LICENCES:
        return "review"
    return "reject"


class Stage:
    """Staging folder shared by every source: manifest, review sheet, files, state."""

    def __init__(self, out):
        self.out = Path(out)
        (self.out / "files").mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "oa-state.json"
        self.state = json.loads(self.state_path.read_text("utf-8")) if self.state_path.exists() else {"done": {}}
        manifest = self.out / "manifest.json"
        self.manifest = json.loads(manifest.read_text("utf-8")) if manifest.exists() else []
        self.rows = []

    def row(self, key, title, **fields):
        row = {"id": key, "title": (title or "")[:200], "language": "", "licence": "", "status": "", "reason": "",
               "url": "", "file": "", **fields}
        self.rows.append(row)
        return row

    def add(self, key, entry, filename, data):
        (self.out / "files" / filename).write_bytes(data)
        self.manifest.append({**entry, "text_file": "files/" + filename})
        self.state["done"][key] = "files/" + filename

    def save(self, budget):
        (self.out / "manifest.json").write_text(json.dumps(self.manifest, ensure_ascii=False, indent=1), "utf-8")
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")
        with open(self.out / "review.csv", "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.rows[0]) if self.rows else ["id"])
            writer.writeheader()
            writer.writerows(self.rows)
        (self.out / "cost.json").write_text(json.dumps({"estimated_usd": round(budget.spent, 4), "calls": budget.calls}), "utf-8")


def safe_name(key, ext):
    return re.sub(r"[^A-Za-z0-9]+", "_", key)[-80:] + ext


def correct_language(language, title):
    """OpenAlex often labels Uzbek Cyrillic as Russian. Correct only when the
    title's letters are unambiguous; the importer re-checks the full text."""
    lowered = (title or "").lower()
    if language == "ru" and set(lowered) & UZ_ONLY:
        return "uz"
    if language == "uz" and "ы" in lowered and not set(lowered) & UZ_ONLY:
        return "ru"
    return None


def work_metadata(work):
    """Fields the importer needs, from an OpenAlex work; returns (entry, problems)."""
    location = work.get("best_oa_location") or {}
    authors = [a.get("author", {}).get("display_name") for a in work.get("authorships") or []]
    authors = [a for a in authors if a]
    year = work.get("publication_year")
    language = work.get("language")
    url = work.get("doi") or location.get("landing_page_url") or ""
    problems = [p for p, bad in (("language is not uz/ru/en", language not in LANGS), ("no year", type(year) is not int),
                                 ("no authors", not authors), ("no title", not work.get("display_name")),
                                 ("no https landing page or DOI", not str(url).startswith("https://"))) if bad]
    entry = {"title": work.get("display_name") or "", "authors": "; ".join(authors[:30]), "year": year,
             "url": url, "language": language}
    return entry, problems


def openalex_filter(args):
    parts = ["best_oa_location.license:" + "|".join(LICENCES), "open_access.is_oa:true", "type:article|book-chapter|dissertation|book"]
    if args.language:
        parts.append("language:" + args.language)
    if args.country:
        parts.append("authorships.institutions.country_code:" + args.country.upper())
    if args.since:
        parts.append(f"from_publication_date:{args.since}-01-01" if len(args.since) == 4 else f"from_publication_date:{args.since}")
    if args.source_id:
        parts.append("primary_location.source.id:" + args.source_id)
    if args.pdf_from == "openalex":
        parts.append("has_content.pdf:true")
    return ",".join(parts)


def run_openalex(args, fetcher, budget, stage, api=OPENALEX, content=OPENALEX_CONTENT):
    key = os.environ.get("OPENALEX_API_KEY", "")
    resume, max_new = getattr(args, "resume", False), getattr(args, "max_new", None)
    cursor = (stage.state.get("cursor") if resume else None) or "*"
    seen = new = 0
    # Whole pages are processed before stopping, so a resumed run never skips works.
    while cursor and seen < args.max_records and (max_new is None or new < max_new):
        params = {"filter": openalex_filter(args), "per-page": "100", "cursor": cursor,
                  "select": "id,doi,display_name,publication_year,language,authorships,best_oa_location,open_access,has_content"}
        if args.query:
            params["search"] = args.query
        budget.charge("search" if args.query else "list")
        body, _ = fetcher.get(api + "/works?" + urlencode(params), 20 * 1024 * 1024, robots=False, headers=bearer(key))
        data = json.loads(body)
        next_cursor = (data.get("meta") or {}).get("next_cursor")
        if not data.get("results"):
            next_cursor = None
        for work in data.get("results") or []:
            if seen >= args.max_records and not resume:
                break
            seen += 1
            before = len(stage.manifest)
            stage_work(work, args, fetcher, budget, stage, key, content)
            new += len(stage.manifest) - before
        cursor = next_cursor
        if resume:
            stage.state["cursor"] = cursor          # None: the listing is finished; next run starts again
            stage.state["listing_complete"] = cursor is None
            stage.save(budget)


def stage_work(work, args, fetcher, budget, stage, key, content, text=None):
    wid = (work.get("id") or "").rsplit("/", 1)[-1]
    location = work.get("best_oa_location") or {}
    licence = licence_of(location.get("license"))
    entry, problems = work_metadata(work)
    from_text = detect_language(text) if text else None      # CORE: we hold the full text itself
    corrected = (from_text if from_text and from_text != entry["language"] else None) or \
        correct_language(entry["language"], entry["title"])
    if corrected:
        problems = [p for p in problems if p != "language is not uz/ru/en"]
    if corrected:
        entry["language"] = corrected
    row = stage.row(wid, entry["title"], language=entry["language"] or "", licence=licence, url=entry["url"])
    if corrected:
        row["reason"] = (f"language label {work.get('language')} corrected to {corrected} "
                         + ("from the full text" if from_text == corrected else "from the title's letters"))
    if wid in stage.state["done"]:
        row.update(status="skipped", reason="already staged", file=stage.state["done"][wid])
        return
    prefer = getattr(args, "prefer", None)
    if prefer == "uz" and entry["language"] == "ru" and "ы" in (entry["title"] or "").lower():
        row.update(status="skipped", reason="title is Russian (letter ы); not downloaded in the Uzbek relabelling pass")
        return
    verdict = decide(licence)
    if args.unpaywall_email and work.get("doi") and verdict == "accept":
        other = unpaywall_licence(work["doi"], args.unpaywall_email, fetcher)
        if other and other != licence:
            problems = problems + [f"Unpaywall states {other}, OpenAlex states {licence}"]
    if verdict == "reject":
        row.update(status="rejected", reason="; ".join([f"licence '{licence or 'none'}' does not allow reuse"] + problems))
        return
    if verdict == "review" or problems:
        reasons = ([f"{licence}: non-commercial or no-derivatives terms need a human decision"] if verdict == "review" else [])
        row.update(status="review", reason="; ".join(reasons + problems))
        return
    basis, label = LICENCES[licence]
    entry.update(rights_basis=basis, permission_reference=(
        f"{label} licence stated by OpenAlex best_oa_location for {work.get('id')} ({location.get('landing_page_url') or entry['url']}); "
        f"staged {date.today().isoformat()}" + ("; Unpaywall agrees" if args.unpaywall_email else "")))
    if text is not None:                                   # CORE full text
        if not text.strip() or len(text) > MAX_TEXT:
            row.update(status="review", reason="CORE full text empty or too long")
            return
        name = safe_name(wid, ".txt")
        stage.add(wid, entry, name, text.encode("utf-8"))
        row.update(status="staged", file="files/" + name)
        return
    if not args.download:
        row.update(status="accepted", reason="; ".join(filter(None, [row["reason"], "metadata only; re-run with --download"])))
        return
    try:
        data = fetch_pdf(work, location, licence, args, fetcher, budget, key, content)
    except (HarvestError, urllib.error.URLError, OSError) as exc:
        if "cost limit" in str(exc):
            raise
        row.update(status="review", reason=str(exc).replace(key, "***") if key else str(exc))
        return
    name = safe_name(wid, ".pdf")
    stage.add(wid, entry, name, data)
    row.update(status="downloaded", file="files/" + name)


def fetch_pdf(work, location, licence, args, fetcher, budget, key, content):
    """Publisher PDF link; else the PDF declared on the article page (citation_pdf_url, as
    OJS journals publish), after confirming the page does not state another licence;
    else, if allowed, OpenAlex's cached copy (billed). Returns PDF bytes."""
    wid = (work.get("id") or "").rsplit("/", 1)[-1]
    if args.pdf_from == "openalex":
        budget.charge("content")
        data, _ = fetcher.get(f"{content}/works/{wid}.pdf", MAX_PDF, robots=False, headers=bearer(key))
        return _pdf(data)
    problems = []
    pdf_url = location.get("pdf_url")
    landing = location.get("landing_page_url") or work.get("doi")
    if not pdf_url and landing:
        try:
            page, _ = fetcher.get(landing, MAX_PAGE, cross_host=True)
            meta = _PageMeta()
            meta.feed(page.decode("utf-8", "replace"))
            if meta.licences:
                stated, _, page_label = classify_licence(meta.licences)
                if stated != "accept" or LICENCE_CODES.get(page_label) != licence:
                    raise HarvestError(f"article page states a different licence ({page_label})")
            if meta.pdf:
                pdf_url = urljoin(getattr(fetcher, "last_url", None) or landing, meta.pdf)
            else:
                problems.append("article page declares no PDF")
        except (HarvestError, urllib.error.URLError, OSError) as exc:
            if "different licence" in str(exc):
                raise
            problems.append(str(exc))
    if pdf_url:
        try:
            return _pdf(fetcher.get(pdf_url, MAX_PDF, cross_host=True)[0])
        except (HarvestError, urllib.error.URLError, OSError) as exc:
            problems.append(str(exc))
    elif not problems:
        problems.append("no PDF link")
    if getattr(args, "openalex_fallback", False) and (work.get("has_content") or {}).get("pdf"):
        budget.charge("content")
        data, _ = fetcher.get(f"{content}/works/{wid}.pdf", MAX_PDF, robots=False, headers=bearer(key))
        return _pdf(data)
    raise HarvestError("; ".join(problems))


def _pdf(data):
    if not data.startswith(b"%PDF"):
        raise HarvestError("download is not a PDF")
    return data


def unpaywall_licence(doi, email, fetcher, api=None):
    api = api or UNPAYWALL
    doi = doi.replace("https://doi.org/", "")
    try:
        body, _ = fetcher.get(f"{api}/{quote(doi)}?" + urlencode({"email": email}), 2 * 1024 * 1024, robots=False)
        return licence_of(((json.loads(body).get("best_oa_location") or {}).get("license")))
    except (HarvestError, urllib.error.URLError, OSError, ValueError):
        return None


MIN_CORE_TEXT = 500   # characters; CORE sometimes returns a few stray characters as "full text"


def core_entry(item, args):
    """Importer metadata for a CORE record accepted under a trusted repository policy."""
    lang = (item.get("language") or {}).get("code") if isinstance(item.get("language"), dict) else None
    lang = detect_language(item.get("fullText") or "") or correct_language(lang, item.get("title")) or lang
    urls = [u for u in [item.get("downloadUrl")] + list(item.get("sourceFulltextUrls") or []) if str(u).startswith("https://")]
    authors = [a.get("name") for a in item.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    year = item.get("yearPublished")
    problems = [p for p, bad in (("language is not uz/ru/en", lang not in LANGS), ("no year", type(year) is not int),
                                 ("no authors", not authors), ("no title", not item.get("title"))) if bad]
    entry = {"title": item.get("title") or "", "authors": "; ".join(authors[:30]), "year": year,
             "url": urls[0] if urls else f"https://core.ac.uk/works/{item.get('id')}", "language": lang}
    return entry, problems


def run_core(args, fetcher, budget, stage, api=CORE, openalex=OPENALEX, content=OPENALEX_CONTENT):
    """CORE texts carry no licence field (verified against the live API, Nov 2026).
    Accepted only when (a) the DOI's licence in OpenAlex allows reuse, or (b) the
    record comes from a repository the owner has verified and listed with
    --trusted-provider; an explicit NC/ND licence in OpenAlex always goes to review."""
    core_key, oa_key = os.environ.get("CORE_API_KEY", ""), os.environ.get("OPENALEX_API_KEY", "")
    if not core_key:
        raise HarvestError("CORE_API_KEY is not set (free registration at core.ac.uk/services/api)")
    trusted = set(args.trusted_provider or [])
    if trusted and not (args.trusted_note or "").strip():
        raise HarvestError("--trusted-note is required with --trusted-provider (how and when you verified the policy)")
    offset = 0
    while offset < args.max_records:
        params = {"q": args.query or "*", "limit": "50", "offset": str(offset)}
        # The live API redirects /search/works to /search/works/ (trailing slash required).
        body, _ = fetcher.get(api + "/search/works/?" + urlencode(params), 50 * 1024 * 1024, robots=False,
                              headers={"Authorization": "Bearer " + core_key})
        results = json.loads(body).get("results") or []
        if not results:
            break
        for item in results[: args.max_records - offset]:
            key = "core:" + str(item.get("id"))
            doi = str(item.get("doi") or "").lower().replace("https://doi.org/", "")
            text = item.get("fullText") or ""
            providers = {p.get("id"): p.get("name") for p in item.get("dataProviders") or [] if isinstance(p, dict)}
            provider = next((pid for pid in providers if pid in trusted), None)
            if len(text.strip()) < MIN_CORE_TEXT:
                stage.row(key, item.get("title"), status="rejected", reason="no usable full text in CORE")
                continue
            work, licence = None, None
            if doi:
                budget.charge("single")
                try:
                    work_body, _ = fetcher.get(f"{openalex}/works/doi:{quote(doi)}", 2 * 1024 * 1024, robots=False,
                                               headers=bearer(oa_key))
                    work = json.loads(work_body)
                    licence = licence_of((work.get("best_oa_location") or {}).get("license"))
                except (HarvestError, urllib.error.URLError, OSError, ValueError):
                    work = None
            if work and (decide(licence) in ("accept", "review") or provider is None):
                stage_work(work, args, fetcher, budget, stage, oa_key, content, text=text)
                continue
            if provider is None:
                stage.row(key, item.get("title"), status="rejected" if not doi else "review",
                          reason="no DOI to verify the licence, and the repository is not on your trusted list"
                          if not doi else "licence could not be verified in OpenAlex")
                continue
            entry, problems = core_entry(item, args)
            row = stage.row(key, entry["title"], language=entry["language"] or "", licence="repository policy",
                            url=entry["url"])
            if key in stage.state["done"]:
                row.update(status="skipped", reason="already staged", file=stage.state["done"][key])
                continue
            if problems or len(text) > MAX_TEXT:
                row.update(status="review", reason="; ".join(problems or ["full text too long"]))
                continue
            entry.update(rights_basis=args.trusted_basis, permission_reference=(
                f"Repository policy of CORE data provider {provider} ({providers[provider]}) verified by the owner: "
                f"{args.trusted_note.strip()}; CORE work {item.get('id')}; staged {date.today().isoformat()}"))
            name = safe_name(key, ".txt")
            stage.add(key, entry, name, text.encode("utf-8"))
            row.update(status="staged", file="files/" + name)
        offset += len(results)


def main():
    parser = argparse.ArgumentParser(description="Stage open-licence full texts for review (nothing is indexed).")
    parser.add_argument("source", choices=["openalex", "core"])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--language", choices=LANGS)
    parser.add_argument("--country", help="institution country code, e.g. UZ")
    parser.add_argument("--since", help="publication year or date, e.g. 2020")
    parser.add_argument("--source-id", help="OpenAlex source (journal) ID, e.g. S1234567")
    parser.add_argument("--query", help="OpenAlex search words, or a CORE query")
    parser.add_argument("--download", action="store_true", help="download PDFs of accepted OpenAlex works")
    parser.add_argument("--pdf-from", choices=["publisher", "openalex"], default="publisher",
                        help="publisher: free direct link; openalex: OpenAlex's cached copy (billed per file)")
    parser.add_argument("--trusted-provider", type=int, action="append",
                        help="CORE data-provider ID whose open-licence policy you have verified (repeatable)")
    parser.add_argument("--trusted-note", help="how and when you verified that repository's policy (required with --trusted-provider)")
    parser.add_argument("--trusted-basis", choices=["open_license", "written_permission"], default="open_license")
    parser.add_argument("--unpaywall-email", help="cross-check each licence with Unpaywall (your contact email)")
    parser.add_argument("--max-records", type=int, default=200)
    parser.add_argument("--max-cost-usd", type=float, default=0.50)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()
    fetcher, budget, stage = Fetcher(delay=args.delay), Budget(args.max_cost_usd), Stage(args.out)
    error = None
    try:
        (run_openalex if args.source == "openalex" else run_core)(args, fetcher, budget, stage)
    except (HarvestError, urllib.error.URLError, OSError, ValueError) as exc:
        error = str(exc)
    stage.save(budget)
    counts = {}
    for row in stage.rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("Records:", len(stage.rows), "·", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
          f"· estimated OpenAlex cost ${budget.spent:.3f}")
    print(f"Review {stage.out / 'review.csv'}, then: python corpus.py {stage.out / 'manifest.json'} --check")
    if error:
        sys.exit(f"STOPPED: {error} (progress so far was saved)")


if __name__ == "__main__":
    main()
