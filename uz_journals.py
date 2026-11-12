"""Uzbek journals: find the ones with an open-licence OAI-PMH feed, then harvest them.

Step 1, survey (writes journals.csv):
    python uz_journals.py survey --out staging/uz-journals
  * the journal list comes from OpenAlex (sources with country_code UZ: 252 in November 2026,
    about 203,000 articles); most run Open Journal Systems, whose feed is <homepage>/oai;
  * each feed's first page is read (robots.txt respected) and its records' licences counted
    with harvest.classify_licence. A journal is "ok" when at least half of the sample states
    CC BY, CC BY-SA or CC0, and "no_licence" otherwise.

Step 2, harvest (imports the accepted articles of every "ok" journal, a batch per journal per run):
    python uz_journals.py harvest --out staging/uz-journals --batch 50 [--import]
  Uses harvest.harvest with PDF download: licence confirmed on the article page, PDF only from
  the journal's own host, robots.txt and crawl delay respected. Resumes where it stopped.

Automatic on the engine: AUTO_IMPORT_UZ_JOURNALS=1 (survey once, then a harvest batch every
AUTO_IMPORT_HOURS), UZ_JOURNALS_BATCH=50 articles per journal per run.
"""
import argparse
import csv
import json
import os
import re
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from xml.etree.ElementTree import ParseError

import harvest
from harvest import Fetcher, HarvestError, classify_licence, parse_records, MAX_XML

OPENALEX = "https://api.openalex.org/sources"
FIELDS = ["name", "endpoint", "works", "sample", "open_licence", "share", "licences", "languages", "status", "reason"]


def openalex_journals(fetch=None, country="UZ"):
    """[(name, homepage, works)] for a country's journals in OpenAlex (paged with a cursor)."""
    import urllib.request
    key = os.environ.get("OPENALEX_API_KEY")

    def default_fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": "PlagIQ-research-pilot/0.5",
                                                       **({"Authorization": "Bearer " + key} if key else {})})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(MAX_XML))
    fetch = fetch or default_fetch
    out, cursor = [], "*"
    while cursor:
        data = fetch(OPENALEX + "?" + urlencode({"filter": f"country_code:{country}", "per_page": 200, "cursor": cursor,
                                                  "select": "display_name,homepage_url,works_count"}))
        for item in data.get("results", []):
            homepage = item.get("homepage_url") or ""
            if homepage.startswith(("https://", "http://")):
                out.append((item.get("display_name") or "", homepage.rstrip("/"), int(item.get("works_count") or 0)))
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not data.get("results"):
            break
    return out


def endpoint_for(homepage, keep_http=False):
    """OJS feed address for a journal homepage (…/index.php/<journal>/oai), or None.
    Always HTTPS (keep_http only for tests against a local server)."""
    parts = urlsplit(homepage)
    match = re.match(r"(/index\.php/[^/]+)", parts.path)
    if not match:
        return None
    scheme = parts.scheme if keep_http else "https"
    return f"{scheme}://{parts.netloc}{match.group(1)}/oai"


def probe(endpoint, fetcher):
    """(sample size, open-licence records, licences seen, languages seen) from the feed's first page."""
    body, _ = fetcher.get(endpoint + "?" + urlencode({"verb": "ListRecords", "metadataPrefix": "oai_dc"}), MAX_XML)
    records, _ = parse_records(body)
    licences, languages, accepted = {}, {}, 0
    for record in records:
        decision, _, label = classify_licence(record["rights"])
        licences[label if decision != "reject" else "none"] = licences.get(label if decision != "reject" else "none", 0) + 1
        accepted += decision == "accept"
        for lang in record["languages"] or ["?"]:
            languages[lang] = languages.get(lang, 0) + 1
    return len(records), accepted, licences, languages


def survey(out, journals=None, fetcher=None, keep_http=False):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    fetcher = fetcher or Fetcher(delay=1.0, timeout=30)
    rows = []
    for name, homepage, works in journals if journals is not None else openalex_journals():
        endpoint = endpoint_for(homepage, keep_http)
        row = {"name": name[:120], "endpoint": endpoint or "", "works": works, "sample": 0, "open_licence": 0,
               "share": 0.0, "licences": "", "languages": "", "status": "", "reason": ""}
        rows.append(row)
        if not endpoint:
            row.update(status="no_oai", reason=f"homepage is not an Open Journal Systems address: {homepage}")
            continue
        try:
            sample, accepted, licences, languages = probe(endpoint, fetcher)
        except ParseError as exc:
            row.update(status="bad_feed", reason=f"feed is not valid XML ({exc})"[:200])
            continue
        except (HarvestError, OSError, ValueError) as exc:
            row.update(status="unreachable", reason=str(exc)[:200])
            continue
        share = accepted / sample if sample else 0.0
        row.update(sample=sample, open_licence=accepted, share=round(share, 2),
                   licences="; ".join(f"{k} {v}" for k, v in sorted(licences.items())),
                   languages="; ".join(f"{k} {v}" for k, v in sorted(languages.items())),
                   status="ok" if sample and share >= 0.5 else "no_licence",
                   reason="" if share >= 0.5 else "fewer than half of the sampled records state an open licence")
    with open(out / "journals.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def approved(out):
    path = Path(out) / "journals.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [row for row in csv.DictReader(handle) if row.get("status") == "ok" and row.get("endpoint")]


def harvest_all(out, batch=50, do_import=False, db_path=None, fetcher_factory=None):
    """One harvest batch per approved journal; returns {"journals", "downloaded", "imported", "errors"}."""
    import corpus
    out = Path(out)
    totals = {"journals": 0, "downloaded": 0, "imported": 0, "errors": 0}
    import staging
    for row in approved(out):
        if do_import and staging.low_disk(db_path):
            totals["stopped"] = staging.low_disk(db_path)
            break
        slug = re.sub(r"[^A-Za-z0-9]+", "_", urlsplit(row["endpoint"]).netloc + urlsplit(row["endpoint"]).path)[:80]
        folder = out / slug
        manifest = folder / "manifest.json"
        before = len(json.loads(manifest.read_text("utf-8"))) if manifest.exists() else 0
        totals["journals"] += 1
        try:
            harvest.harvest(row["endpoint"], folder, download=True, max_records=batch,
                            fetcher=fetcher_factory() if fetcher_factory else None)
        except (HarvestError, OSError, ValueError, ParseError):
            totals["errors"] += 1
            continue
        entries = json.loads(manifest.read_text("utf-8"))[before:] if manifest.exists() else []
        totals["downloaded"] += len(entries)
        if do_import:
            import staging
            result = staging.import_and_clear(folder, staging.leftover(folder), db_path)   # new and earlier leftovers
            totals["imported"] += result["imported"]
            totals["errors"] += result["failed"]
            if result["stopped"]:
                totals["stopped"] = result["stopped"]
                break
    return totals


def settings():
    try:
        batch = min(max(int(os.environ.get("UZ_JOURNALS_BATCH", "50")), 1), 1000)
    except ValueError:
        batch = 50
    return os.environ.get("AUTO_IMPORT_UZ_JOURNALS", "") == "1", batch


def run(batch=50, db_path=None, root=None):
    """Engine run: survey once (when journals.csv is missing), then one harvest batch per journal."""
    import import_job
    root = Path(root or import_job.staging_root())
    out = root / "uz-journals"
    status = {"started": date.today().isoformat(), "error": None}
    try:
        if not (out / "journals.csv").exists():
            rows = survey(out)
            status["surveyed"] = len(rows)
            status["approved"] = sum(r["status"] == "ok" for r in rows)
        totals = harvest_all(out, batch, True, db_path)
        status.update({k: v for k, v in totals.items() if k != "stopped"})
        if totals.get("stopped"):
            status["error"] = totals["stopped"]
    except (HarvestError, OSError, ValueError, ParseError) as exc:
        status["error"] = str(exc)[:300]
    status["finished"] = date.today().isoformat()
    import_job.write_status(root, "uz-journals", status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description="Survey and harvest open-licence Uzbek journals (OAI-PMH).")
    parser.add_argument("action", choices=["survey", "harvest"])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--batch", type=int, default=50, help="articles per journal in one harvest run")
    parser.add_argument("--import", dest="do_import", action="store_true")
    parser.add_argument("--db", default=os.environ.get("CORPUS_DB", "corpus.sqlite3"))
    args = parser.parse_args(argv)
    if args.action == "survey":
        rows = survey(args.out)
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        print(f"{len(rows)} journals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
              + f". Details: {args.out}/journals.csv (edit the status column to exclude or include journals).")
    else:
        totals = harvest_all(args.out, args.batch, args.do_import, args.db)
        print(f"{totals['journals']} journals harvested: {totals['downloaded']} new articles"
              + (f", {totals['imported']} imported" if args.do_import else "") + f", {totals['errors']} errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
