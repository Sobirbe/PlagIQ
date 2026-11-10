"""Owner-run harvester for open-licence journal articles via OAI-PMH (e.g. OJS journals).

It never indexes anything. It reads each record's licence statement, keeps only
records whose stated licence clearly allows reuse, optionally downloads the
journal's own PDF, and writes a manifest plus a review sheet into a staging
folder. A person reviews the sheet, then imports with:

    python corpus.py staging/manifest.json --check
    python corpus.py staging/manifest.json

Usage:
    python harvest.py https://journal.example.uz/index.php/j/oai --out staging/j [--from 2024-01-01] [--download]

Rules enforced: HTTPS only; robots.txt and crawl-delay respected; PDFs only from
the journal's own host and only the file the article page declares
(citation_pdf_url); size limits; no redirects to other hosts; no DOCTYPE/
entity processing in XML.
"""
import argparse
import csv
import html.parser
import ipaddress
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from xml.etree import ElementTree

USER_AGENT = "PlagIQ-harvester/0.5 (owner-run; licence-checked; respects robots.txt)"
MAX_XML = 10 * 1024 * 1024
MAX_PAGE = 2 * 1024 * 1024
MAX_PDF = 25 * 1024 * 1024
NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "dc": "http://purl.org/dc/elements/1.1/"}
LANG = {"uz": "uz", "uzb": "uz", "ru": "ru", "rus": "ru", "en": "en", "eng": "en"}

# Licences whose terms allow storing, indexing and quoting short excerpts, including commercially.
ACCEPT = {"by": "open_license", "by-sa": "open_license", "zero": "public_domain", "mark": "public_domain"}
REVIEW = {"by-nc", "by-nc-sa", "by-nd", "by-nc-nd"}
CC_URL = re.compile(r"creativecommons\.org/(?:licenses/(by(?:-nc)?(?:-sa|-nd)?)/(\d\.\d)|publicdomain/(zero|mark)/(\d\.\d))", re.I)
CC_TEXT = re.compile(r"\b(?:CC[\s-]*(BY(?:[\s-]*NC)?(?:[\s-]*(?:SA|ND))?)(?:[\s-]*(\d\.\d))?|(CC0))\b", re.I)
# Written-out names: "Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International".
CC_NAME = re.compile(r"creative\s+commons\s+attribution((?:[\s-]+(?:non[\s-]?commercial|share[\s-]?alike|no[\s-]?deriv\w*))*)", re.I)


def _name_code(parts):
    parts = parts.lower()
    return "by" + ("-nc" if re.search(r"non[\s-]?commercial", parts) else "") + \
        ("-sa" if re.search(r"share[\s-]?alike", parts) else "") + ("-nd" if re.search(r"no[\s-]?deriv", parts) else "")


class HarvestError(RuntimeError):
    pass


def public_host(host):
    """True if every address of host is a public internet address (no internal/loopback targets)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


class _Redirect(urllib.request.HTTPRedirectHandler):
    """Same-host redirects only, unless cross-host is allowed: then HTTPS public hosts only."""

    def __init__(self, cross_host, allow_http_hosts):
        super().__init__()
        self.cross_host, self.allow_http_hosts = cross_host, allow_http_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.hostname != old.hostname or new.scheme != old.scheme:
            local_test = new.hostname in self.allow_http_hosts
            if not self.cross_host or not (local_test or (new.scheme == "https" and public_host(new.hostname))):
                raise HarvestError(f"refused redirect to another host: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """HTTPS GETs with robots.txt, crawl delay, size caps and same-host redirects."""

    def __init__(self, delay=2.0, allow_http_hosts=()):
        self.delay, self.allow_http_hosts = delay, set(allow_http_hosts)
        self.robots, self.last = {}, {}
        self.openers = {flag: urllib.request.build_opener(_Redirect(flag, self.allow_http_hosts)) for flag in (False, True)}

    def _check_url(self, url):
        parts = urlsplit(url)
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in self.allow_http_hosts):
            raise HarvestError(f"HTTPS required: {url}")
        if not parts.hostname or parts.username or parts.password:
            raise HarvestError(f"invalid URL: {url}")
        return parts

    def _raw(self, url, limit, cross_host=False, headers=None):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with self.openers[cross_host].open(request, timeout=30) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise HarvestError(f"response over {limit // (1024 * 1024)} MB: {url}")
            return body, response.headers.get("Content-Type", "")

    def _robots(self, parts):
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                body, _ = self._raw(origin + "/robots.txt", 512 * 1024)
                rp.parse(body.decode("utf-8", "replace").splitlines())
            except urllib.error.HTTPError as exc:
                rp.parse([] if 400 <= exc.code < 500 and exc.code not in (401, 403) else ["User-agent: *", "Disallow: /"])
            except (urllib.error.URLError, OSError):
                rp.parse(["User-agent: *", "Disallow: /"])          # unreachable robots.txt: do not crawl
            self.robots[origin] = rp
        return self.robots[origin]

    def get(self, url, limit, robots=True, cross_host=False, headers=None):
        """robots=False only for documented APIs; cross_host=True for publisher links found via an API."""
        parts = self._check_url(url)
        if cross_host and parts.hostname not in self.allow_http_hosts and not public_host(parts.hostname):
            raise HarvestError(f"refused non-public host: {parts.hostname}")
        wait = self.delay
        if robots:
            rp = self._robots(parts)
            if not rp.can_fetch(USER_AGENT, url):
                raise HarvestError(f"robots.txt disallows {url}")
            wait = max(wait, float(rp.crawl_delay(USER_AGENT) or 0))
        elapsed = time.monotonic() - self.last.get(parts.netloc, -1e9)
        if elapsed < wait:
            time.sleep(wait - elapsed)
        try:
            return self._raw(url, limit, cross_host, headers)
        finally:
            self.last[parts.netloc] = time.monotonic()


def classify_licence(statements):
    """('accept', rights_basis, label) | ('review', None, label) | ('reject', None, reason)."""
    found = set()
    for text in statements:
        for m in CC_URL.finditer(text):
            found.add((m.group(1) or m.group(3)).lower())
        for m in CC_TEXT.finditer(text):
            found.add("zero" if m.group(3) else re.sub(r"[\s-]+", "-", m.group(1).lower()))
        for m in CC_NAME.finditer(text):
            found.add(_name_code(m.group(1)))
    if not found:
        return "reject", None, "no recognised open licence in the record"
    if len(found) > 1:
        return "review", None, "several different licences stated: " + ", ".join(sorted(found))
    (licence,) = found
    if licence in ACCEPT:
        return "accept", ACCEPT[licence], licence
    if licence in REVIEW:
        return "review", None, f"{licence}: non-commercial or no-derivatives terms need a human decision"
    return "reject", None, f"unrecognised licence {licence}"


def parse_records(body):
    if b"<!DOCTYPE" in body[:2048].upper():
        raise HarvestError("XML with DOCTYPE refused")
    root = ElementTree.fromstring(body)
    error = root.find("oai:error", NS)
    if error is not None:
        if error.get("code") == "noRecordsMatch":
            return [], None
        raise HarvestError(f"OAI-PMH error {error.get('code')}: {(error.text or '').strip()}")
    records = []
    for rec in root.iterfind(".//oai:ListRecords/oai:record", NS):
        header = rec.find("oai:header", NS)
        if header is None or header.get("status") == "deleted":
            continue
        dc = lambda tag: [(e.text or "").strip() for e in rec.iterfind(f".//dc:{tag}", NS) if (e.text or "").strip()]
        records.append({"oai_id": header.findtext("oai:identifier", "", NS).strip(),
                        "datestamp": header.findtext("oai:datestamp", "", NS).strip(),
                        "title": (dc("title") or [""])[0], "creators": dc("creator"), "dates": dc("date"),
                        "languages": dc("language"), "identifiers": dc("identifier"), "rights": dc("rights"),
                        "source": (dc("source") or [""])[0]})
    token = root.find(".//oai:resumptionToken", NS)
    return records, (token.text.strip() if token is not None and token.text and token.text.strip() else None)


class _PageMeta(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.pdf, self.licences = None, []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and a.get("name", "").lower() == "citation_pdf_url" and not self.pdf:
            self.pdf = a.get("content")
        if tag == "meta" and a.get("name", "").lower() in ("dc.rights", "dcterms.license"):
            self.licences.append(a.get("content", ""))
        if tag in ("a", "link") and "license" in a.get("rel", "").lower().split():
            self.licences.append(a.get("href", ""))


def article_url(record, host):
    urls = [u for u in record["identifiers"] if u.startswith(("https://", "http://"))]
    same = [u for u in urls if urlsplit(u).hostname == host]
    view = [u for u in same if "/article/view/" in u]
    return (view or same or [None])[0]


def year_of(record):
    for value in record["dates"]:
        m = re.search(r"\b(1[0-9]{3}|2[01][0-9]{2})\b", value)
        if m:
            return int(m.group(1))
    return None


def language_of(record):
    codes = {LANG.get(v.lower().replace("_", "-").split("-")[0]) for v in record["languages"]} - {None}
    return codes.pop() if len(codes) == 1 else None


def harvest(endpoint, out, since=None, set_spec=None, download=False, max_records=500, fetcher=None):
    fetcher = fetcher or Fetcher()
    out = Path(out)
    (out / "files").mkdir(parents=True, exist_ok=True)
    host = urlsplit(endpoint).hostname
    state_path = out / "harvest-state.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"done": {}, "last_from": None}
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else []
    review, seen, token = [], 0, None
    today = date.today().isoformat()
    while seen < max_records:
        params = {"verb": "ListRecords", "resumptionToken": token} if token else \
            {"verb": "ListRecords", "metadataPrefix": "oai_dc", **({"from": since} if since else {}),
             **({"set": set_spec} if set_spec else {})}
        body, _ = fetcher.get(endpoint + "?" + urlencode(params), MAX_XML)
        records, token = parse_records(body)
        for record in records:
            seen += 1
            row = {"oai_id": record["oai_id"], "title": record["title"][:200], "language": "", "licence": "",
                   "status": "", "reason": "", "url": "", "file": ""}
            review.append(row)
            if record["oai_id"] in state["done"]:
                row.update(status="skipped", reason="already harvested", file=state["done"][record["oai_id"]])
                continue
            decision, basis, label = classify_licence(record["rights"])
            row["licence"] = label if decision != "reject" else ""
            language, year, url = language_of(record), year_of(record), article_url(record, host)
            row.update(language=language or "", url=url or "")
            problems = [p for p, bad in (("language missing or not uz/ru/en", not language), ("no year", not year),
                                         ("no article page on the journal's host", not url), ("no title", not record["title"]),
                                         ("no authors", not record["creators"])) if bad]
            if decision != "accept" or problems:
                row.update(status="review" if decision == "review" and not problems else "rejected",
                           reason="; ".join(([] if decision == "accept" else [label]) + problems))
                continue
            entry = {"title": record["title"], "authors": "; ".join(record["creators"]), "year": year,
                     "url": url.replace("http://", "https://", 1) if url.startswith("http://") else url,
                     "language": language, "rights_basis": basis,
                     "permission_reference": f"Licence {label} stated in OAI-PMH record {record['oai_id']} "
                                             f"({endpoint}); harvested {today}"}
            if not download:
                row.update(status="accepted", reason="metadata only; re-run with --download to fetch the PDF")
                continue
            try:
                page, _ = fetcher.get(url, MAX_PAGE)
                meta = _PageMeta()
                meta.feed(page.decode("utf-8", "replace"))
                page_decision, _, page_label = classify_licence(meta.licences) if meta.licences else ("accept", None, label)
                if page_decision != "accept" or page_label != label:
                    raise HarvestError(f"article page states a different licence ({page_label})")
                if not meta.pdf:
                    raise HarvestError("article page declares no citation_pdf_url")
                pdf_url = urljoin(url, meta.pdf)
                if urlsplit(pdf_url).hostname != host:
                    raise HarvestError("PDF is hosted elsewhere; not downloaded")
                pdf, _ = fetcher.get(pdf_url, MAX_PDF)
                if not pdf.startswith(b"%PDF"):
                    raise HarvestError("download is not a PDF")
            except (HarvestError, urllib.error.URLError, OSError) as exc:
                row.update(status="review", reason=str(exc))
                continue
            name = re.sub(r"[^A-Za-z0-9]+", "_", record["oai_id"])[-80:] + ".pdf"
            (out / "files" / name).write_bytes(pdf)
            manifest.append({**entry, "text_file": "files/" + name, "permission_reference":
                             entry["permission_reference"] + f"; licence confirmed on {url}" * bool(meta.licences)})
            state["done"][record["oai_id"]] = "files/" + name
            row.update(status="downloaded", file="files/" + name)
        if not token:
            break
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    state["last_from"] = today
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    with open(out / "review.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(review[0]) if review else ["oai_id"])
        writer.writeheader()
        writer.writerows(review)
    return review


def main():
    parser = argparse.ArgumentParser(description="Harvest open-licence journal records for review (nothing is indexed).")
    parser.add_argument("endpoint", help="OAI-PMH base URL, e.g. https://journal.example/index.php/j/oai")
    parser.add_argument("--out", required=True, type=Path, help="staging folder (reused for incremental runs)")
    parser.add_argument("--from", dest="since", help="only records changed since YYYY-MM-DD (default: last run)")
    parser.add_argument("--set", dest="set_spec", help="OAI set, e.g. one journal section")
    parser.add_argument("--download", action="store_true", help="download PDFs of accepted records")
    parser.add_argument("--max-records", type=int, default=500)
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between requests (robots.txt may require more)")
    args = parser.parse_args()
    since = args.since
    state = args.out / "harvest-state.json"
    if not since and state.exists():
        since = json.loads(state.read_text("utf-8")).get("last_from")
    try:
        rows = harvest(args.endpoint, args.out, since, args.set_spec, args.download, args.max_records,
                       Fetcher(delay=args.delay))
    except (HarvestError, urllib.error.URLError, ElementTree.ParseError, OSError) as exc:
        sys.exit(f"ERROR: {exc}")
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("Records:", len(rows), "·", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"Review {args.out / 'review.csv'} before importing. Next: python corpus.py {args.out / 'manifest.json'} --check")


if __name__ == "__main__":
    main()
