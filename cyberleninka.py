"""Import open-licence articles from CyberLeninka (cyberleninka.ru), the largest open
Russian-language collection of scientific articles.

How it works:
  * Article list: CyberLeninka's OAI-PMH feed (https://cyberleninka.ru/oai). Its records carry
    no licence, so the licence is read from each article page.
  * Licence: the badge on the article page ("CC BY", "CC BY-SA", ...), classified with the same
    rules as harvest.py: CC BY, CC BY-SA, CC0 and public domain are imported; NC/ND licences go
    to review; articles without a licence badge are skipped.
  * Text: CyberLeninka's robots.txt forbids downloading PDFs (except for Google), so the full
    text is taken from the article page itself (the "articleBody" block), which robots.txt allows.
  * Politeness: robots.txt respected, at least CYBERLENINKA_DELAY seconds (default 3) between
    requests, HTTPS only, size caps.
Language is detected from the text (Russian, Uzbek or English); others are skipped.

Owner-run:
    python cyberleninka.py --out staging/cyberleninka --max 200 [--set vak] [--from 2020-01-01] [--import]
Automatic (engine): AUTO_IMPORT_CYBERLENINKA=1, CYBERLENINKA_BATCH=200 (articles per run), optional
CYBERLENINKA_SET (e.g. "vak" for the VAK list) and CYBERLENINKA_FROM (YYYY-MM-DD).
"""
import argparse
import csv
import html
import json
import os
import re
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import harvest
from harvest import Fetcher, HarvestError, classify_licence, parse_records, MAX_PAGE, MAX_XML

BASE = "https://cyberleninka.ru"


def base():
    return (os.environ.get("CYBERLENINKA_URL") or BASE).rstrip("/")    # override for tests only
MIN_WORDS = 300
BADGE = re.compile(r'class="statitem label-cc"[^>]*>(?:\s*<img[^>]*>)?\s*([^<]{2,40}?)\s*</div>', re.I)
META = re.compile(r'<meta\s+name="(citation_[a-z_]+)"\s+content="([^"]*)"', re.I)


def parse_article(page):
    """{"licence": badge text or "", "title", "authors", "year", "journal", "text"} from an article page."""
    meta = {}
    for name, value in META.findall(page):
        meta.setdefault(name.lower(), []).append(html.unescape(value).strip())
    badge = BADGE.search(page)
    start = page.find('itemprop="articleBody"')
    text = ""
    if start != -1:
        start = page.find(">", start) + 1
        end = page.find('<div class="full', start)
        body = page[start:end if end != -1 else len(page)]
        body = re.sub(r'(?s)<div class="ocr-banner">.*?</div>', " ", body)
        import websearch
        text = websearch.html_text(body.encode(), "text/html; charset=utf-8")
    year = re.match(r"\d{4}", (meta.get("citation_publication_date") or [""])[0])
    return {"licence": " ".join(badge.group(1).split()) if badge else "",
            "title": (meta.get("citation_title") or [""])[0][:300],
            "authors": "; ".join(meta.get("citation_author", []))[:300],
            "year": int(year.group()) if year else None,
            "journal": (meta.get("citation_journal_title") or [""])[0][:200],
            "text": text}


def _article_url(record):
    site = urlsplit(base())
    for value in [record["oai_id"]] + record["identifiers"]:
        parts = urlsplit(value)
        if (parts.scheme, parts.netloc) == (site.scheme, site.netloc) and parts.path.startswith("/article/n/"):
            return value
    return None


def harvest_batch(out, max_records=200, set_spec=None, since=None, fetcher=None):
    """Stage up to max_records new articles (resumes where the last run stopped).
    Returns (new manifest entries, review rows)."""
    import corpus
    out = Path(out)
    (out / "files").mkdir(parents=True, exist_ok=True)
    fetcher = fetcher or Fetcher(delay=_delay())
    state_path = out / "state.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"done": {}, "token": None}
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else []
    added, review, seen, today = [], [], 0, date.today().isoformat()
    token = state.get("token")
    while seen < max_records:
        params = {"verb": "ListRecords", "resumptionToken": token} if token else \
            {"verb": "ListRecords", "metadataPrefix": "oai_dc", **({"set": set_spec} if set_spec else {}),
             **({"from": since} if since else {})}
        body, _ = fetcher.get(base() + "/oai?" + urlencode(params), MAX_XML)
        records, token = parse_records(body)
        for record in records:
            if seen >= max_records:
                break
            url = _article_url(record)
            if not url or url in state["done"]:
                continue
            seen += 1
            row = {"url": url, "title": record["title"][:200], "licence": "", "language": "", "status": "", "reason": ""}
            review.append(row)
            try:
                page, _ = fetcher.get(url, MAX_PAGE)
                article = parse_article(page.decode("utf-8", "replace"))
                row["licence"] = article["licence"]
                decision, basis, label = classify_licence([article["licence"]]) if article["licence"] else \
                    ("reject", None, "no licence shown on the article page")
                if decision != "accept":
                    row.update(status="review" if decision == "review" else "skipped", reason=label)
                    state["done"][url] = row["status"]
                    continue
                words = len(article["text"].split())
                language = corpus.detect_language(article["text"]) if words >= MIN_WORDS else None
                row["language"] = language or ""
                problems = [p for p, bad in ((f"under {MIN_WORDS} words of text on the page", words < MIN_WORDS),
                                             ("language not Uzbek, Russian or English", not language),
                                             ("no year", not article["year"]), ("no authors", not article["authors"]),
                                             ("no title", not (article["title"] or record["title"]))) if bad]
                if problems:
                    row.update(status="skipped", reason="; ".join(problems))
                    state["done"][url] = "skipped"
                    continue
            except (HarvestError, OSError, ValueError) as exc:
                row.update(status="error", reason=str(exc)[:200])
                continue
            name = "files/" + re.sub(r"[^A-Za-z0-9]+", "_", urlsplit(url).path.rsplit("/", 1)[-1])[:100] + ".txt"
            (out / name).write_text(article["text"], "utf-8")
            public = BASE + urlsplit(url).path                     # citation: always the public HTTPS address
            entry = {"title": article["title"] or record["title"], "authors": article["authors"], "year": article["year"],
                     "url": public, "language": language, "rights_basis": basis,
                     "permission_reference": f"Licence {label} shown on {public}; harvested {today}",
                     "journal": article["journal"], "text_file": name}
            manifest.append(entry)
            added.append(entry)
            state["done"][url] = name
            row.update(status="staged")
        if not token:
            break
    state["token"] = token
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    state_path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    with open(out / "review.csv", "a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["url", "title", "licence", "language", "status", "reason"])
        if handle.tell() == 0:
            writer.writeheader()
        writer.writerows(review)
    return added, review


def import_entries(out, entries, db_path=None):
    """Index staged entries (all-or-nothing); returns how many were imported."""
    import corpus
    import staging
    if not entries:
        return 0
    return staging.import_and_clear(out, entries, db_path)["imported"]


def _delay():
    try:
        return max(1.0, min(float(os.environ.get("CYBERLENINKA_DELAY", "3")), 30.0))
    except ValueError:
        return 3.0


def settings():
    try:
        batch = min(max(int(os.environ.get("CYBERLENINKA_BATCH", "200")), 1), 5000)
    except ValueError:
        batch = 200
    return (os.environ.get("AUTO_IMPORT_CYBERLENINKA", "") == "1", batch,
            os.environ.get("CYBERLENINKA_SET") or None, os.environ.get("CYBERLENINKA_FROM") or None)


def run(batch=200, set_spec=None, since=None, db_path=None, root=None, fetcher=None):
    """One automatic run on the engine: stage the next batch and import what was accepted."""
    import import_job
    root = Path(root or import_job.staging_root())
    out = root / "cyberleninka"
    status = {"started": date.today().isoformat(), "imported": 0, "staged": 0, "skipped": 0, "error": None}
    import staging
    try:
        retry = staging.import_and_clear(out, staging.leftover(out), db_path)    # files left by a failed run
        if retry["stopped"]:
            raise ValueError(retry["stopped"])
        added, review = harvest_batch(out, batch, set_spec, since, fetcher)
        status["staged"] = len(added)
        status["skipped"] = sum(1 for r in review if r["status"] in ("skipped", "review"))
        result = staging.import_and_clear(out, added, db_path)
        status["imported"] = retry["imported"] + result["imported"]
        if result["stopped"]:
            status["error"] = result["stopped"]
    except (HarvestError, OSError, ValueError) as exc:
        status["error"] = str(exc)[:300]
    status["finished"] = date.today().isoformat()
    import_job.write_status(root, "cyberleninka", status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stage (and optionally import) open-licence CyberLeninka articles.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max", type=int, default=200, help="articles to examine in this run")
    parser.add_argument("--set", dest="set_spec", help='OAI set, e.g. "vak" (VAK list) or "scopus"')
    parser.add_argument("--from", dest="since", help="only records changed since YYYY-MM-DD")
    parser.add_argument("--import", dest="do_import", action="store_true", help="index accepted articles now")
    parser.add_argument("--db", default=os.environ.get("CORPUS_DB", "corpus.sqlite3"))
    args = parser.parse_args(argv)
    added, review = harvest_batch(args.out, args.max, args.set_spec, args.since)
    counts = {}
    for row in review:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(f"Examined {len(review)} article(s): " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if args.do_import:
        print(f"Imported {import_entries(args.out, added, args.db)} article(s) into {args.db}.")
    else:
        print(f"Staged in {args.out}/manifest.json; review {args.out}/review.csv, then: python corpus.py {args.out}/manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
