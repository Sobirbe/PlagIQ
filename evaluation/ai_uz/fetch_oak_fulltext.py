"""Pre-ChatGPT Uzbek full texts from OAK journals that publish under an open licence (training data, human class).

Journals: the ilmiz.uz list of OAK journals with a working OAI-PMH feed (/api/journals?oai_only=true), leaving out
those founded in 2023 or later. Records: each journal's OAI-PMH feed with until=2022-11-30, so only records that
existed and were last changed before ChatGPT's release (30 November 2022) are seen, whatever date they print
(basis "datestamp"). Many feeds have had every change date reset since (a software upgrade or re-indexing) and return
nothing; for those the article's own publication date (dc:date) must be before 30 November 2022 (basis "dc:date", a
weaker test: an article added to an old issue later would pass). Each kept article records its basis.
Kept: records that state Creative Commons BY / BY-SA / CC0 (harvest.classify_licence, "accept" only), are marked
Uzbek, and have an article page on the journal's own host. Full text: the article page must state the same licence;
the PDF is the one it declares (citation_pdf_url) on the same host. harvest.Fetcher enforces HTTPS, robots.txt, a crawl
delay of at least 3 s per host, size caps and same-host redirects. Passages are cut as for human_full_uz.json
(fetch_fulltext.chunks: sentence-aligned, 120-260 words, Uzbek only, reference lists dropped).

The texts are other authors' work: oak_*.json stay out of the repository and are used only to train and measure.

    OAK_HOSTS=a.uz,b.uz python3 fetch_oak_fulltext.py survey   # only journals on these hosts
    python3 fetch_oak_fulltext.py survey            # writes oak_candidates.json (resumable)
    python3 fetch_oak_fulltext.py text [per_journal]  # writes oak_full_uz.json (resumable; default 40 per journal)
"""
import json, os, subprocess, sys, tempfile, time, urllib.error
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))
import harvest                                                    # noqa: E402
from xml.etree import ElementTree                                 # noqa: E402
from fetch_fulltext import chunks                                 # noqa: E402

UNTIL = "2022-11-30"
CAND, FULL = HERE / "oak_candidates.json", HERE / "oak_full_uz.json"
MAX_PAGES = 20                                                    # OAI pages per journal (about 100 records each)
fetcher = harvest.Fetcher(delay=3.0, timeout=60)


def journals():
    body, _ = fetcher.get("https://ilmiz.uz/api/journals?" + urlencode({"limit": 500, "oai_only": "true"}), harvest.MAX_XML)
    items = json.loads(body)
    items = items if isinstance(items, list) else items.get("items", [])
    return [j for j in items if j.get("oaiBaseUrl") and (j.get("founded") or 0) < 2023]


def survey():
    done = json.load(open(CAND)) if CAND.exists() else {}
    only = {h for h in os.environ.get("OAK_HOSTS", "").split(",") if h}       # limit to these hosts (network allowlist)
    js = [j for j in journals() if j["id"] not in done and (not only or urlsplit(j["oaiBaseUrl"]).hostname in only)]
    print(len(js), "journals to survey", flush=True)
    for j in js:
        endpoint, host, kept, seen, note = j["oaiBaseUrl"], urlsplit(j["oaiBaseUrl"]).hostname, [], 0, ""
        basis = "datestamp"
        try:
            for basis in ("datestamp", "dc:date"):
                token = None
                for _ in range(MAX_PAGES if basis == "datestamp" else 4 * MAX_PAGES):
                    params = {"verb": "ListRecords", "resumptionToken": token} if token else \
                        {"verb": "ListRecords", "metadataPrefix": "oai_dc", **({"until": UNTIL} if basis == "datestamp" else {})}
                    body, _ = fetcher.get(endpoint + "?" + urlencode(params), harvest.MAX_XML)
                    records, token = harvest.parse_records(body)
                    for r in records:
                        seen += 1
                        decision, _, label = harvest.classify_licence(r["rights"])
                        url = harvest.article_url(r, host)
                        published = min((d[:10] for d in r["dates"] if d[:4].isdigit()), default="9999")
                        if (decision == "accept" and harvest.language_of(r) == "uz" and url and r["title"]
                                and (basis == "datestamp" or published < UNTIL)):
                            kept.append({"oai_id": r["oai_id"], "datestamp": r["datestamp"], "published": published,
                                         "basis": basis, "title": r["title"][:300], "year": harvest.year_of(r),
                                         "licence": label, "url": url})
                    if not token:
                        break
                if seen:
                    break        # the feed's change dates reach back before ChatGPT: use them, the stronger test
        except (harvest.HarvestError, urllib.error.URLError, OSError, ValueError, ElementTree.ParseError) as exc:
            note = str(exc)[:160]
        done[j["id"]] = {"journal": j["name"], "endpoint": endpoint, "seen": seen, "kept": kept, "note": note, "basis": basis}
        json.dump(done, open(CAND, "w"), ensure_ascii=False)
        print(f"{j['id'][:45]:45s} seen {seen:5d}  open-licence Uzbek {len(kept):4d}  {note[:60]}", flush=True)
    print(sum(len(v["kept"]) for v in done.values()), "candidates from",
          sum(1 for v in done.values() if v["kept"]), "journals")


def text(per_journal=40):
    cand = json.load(open(CAND))
    out = json.load(open(FULL)) if FULL.exists() else []
    tried = {x["oai_id"] for x in out} | set(json.load(open(HERE / "oak_tried.json"))) if (HERE / "oak_tried.json").exists() else {x["oai_id"] for x in out}
    queues = {jid: [c for c in v["kept"] if c["oai_id"] not in tried][:max(0, per_journal - sum(1 for x in out if x["journal"] == jid))]
              for jid, v in cand.items()}
    while any(queues.values()):                                  # round-robin over journals: hosts' delays overlap
        for jid in list(queues):
            if not queues[jid]: continue
            c = queues[jid].pop(0); tried.add(c["oai_id"])
            try:
                host = urlsplit(c["url"]).hostname
                page, _ = fetcher.get(c["url"], harvest.MAX_PAGE)
                meta = harvest._PageMeta(); meta.feed(page.decode("utf-8", "replace"))
                if meta.licences and harvest.classify_licence(meta.licences)[2] != c["licence"]:
                    raise harvest.HarvestError("article page states a different licence")
                if not meta.pdf: raise harvest.HarvestError("no citation_pdf_url")
                pdf_url = urljoin(c["url"], meta.pdf)
                if urlsplit(pdf_url).hostname != host: raise harvest.HarvestError("PDF on another host")
                pdf, _ = fetcher.get(pdf_url, harvest.MAX_PDF)
                if not pdf.startswith(b"%PDF"): raise harvest.HarvestError("not a PDF")
                with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                    f.write(pdf); f.flush()
                    t = subprocess.run(["pdftotext", "-l", "40", f.name, "-"], capture_output=True, text=True, timeout=120).stdout
                ch = chunks(t)
                if ch:
                    out.append({"oai_id": c["oai_id"], "journal": jid, "title": c["title"], "year": c["year"],
                                "datestamp": c["datestamp"], "published": c.get("published"),
                                "basis": c.get("basis", "datestamp"), "license": c["licence"], "url": c["url"], "chunks": ch})
                    json.dump(out, open(FULL, "w"), ensure_ascii=False)
                result = f"{len(ch)} passages"
            except (harvest.HarvestError, urllib.error.URLError, OSError, ValueError, subprocess.SubprocessError) as exc:
                result = str(exc)[:80]
            json.dump(sorted(tried), open(HERE / "oak_tried.json", "w"))
            print(f"{jid[:35]:35s} {result}", flush=True)
    print(len(out), "articles,", sum(len(x["chunks"]) for x in out), "passages from", len({x["journal"] for x in out}), "journals")


if __name__ == "__main__":
    if sys.argv[1:2] == ["survey"]: survey()
    elif sys.argv[1:2] == ["text"]: text(int(sys.argv[2]) if len(sys.argv) > 2 else 40)
    else: print(__doc__)
