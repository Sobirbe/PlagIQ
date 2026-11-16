"""Pre-ChatGPT Uzbek articles from inLibrary.uz (OJS; Creative Commons BY 4.0 per its licence page, checked per article).

Stage 1 (metadata): the site's OAI-PMH feed, restricted to records last changed on or before 30 November 2022 — a record
that existed then was written before ChatGPT's release, whatever date it prints. Stage 2 (full text): for Uzbek articles
whose landing page states a CC licence, the article PDF (the galley link the landing page gives). robots.txt allows both
for general crawlers and asks for Crawl-delay: 8, which is kept (at least 8 s between requests). Texts are other
authors' work: they stay out of the repository (inlibrary_*.json are gitignored) and are used only to train/measure.

    python3 fetch_inlibrary.py meta            # resumable; writes inlibrary_meta.json
    python3 fetch_inlibrary.py text [limit]    # resumable; writes inlibrary_full_uz.json (passages, like human_full_uz)
"""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from html import unescape
from pathlib import Path

HERE = Path(__file__).parent
UA = {"User-Agent": "PlagIQ-research"}
OAI = "https://inlibrary.uz/index.php/index/oai"
UNTIL = "2022-11-30"
DELAY = 8.5
META, STATE, FULL = HERE / "inlibrary_meta.json", HERE / "inlibrary_state.json", HERE / "inlibrary_full_uz.json"
_last = [0.0]


def get(url, binary=False, limit=25_000_000, tries=4):
    for attempt in range(tries):
        wait = _last[0] + DELAY * (1 + 2 * attempt) - time.time()   # a dropped connection: back off, then retry
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read(limit + 1)
            break
        except (ConnectionError, TimeoutError, urllib.error.URLError) as exc:
            if attempt == tries - 1 or getattr(exc, "code", 0) in (403, 404, 410):
                raise
    if len(data) > limit:
        raise ValueError("file too large")
    return data if binary else data.decode("utf-8", "replace")


def field(rec, tag):
    return [unescape(re.sub(r"\s+", " ", v)).strip() for v in re.findall(rf"(?s)<{tag}[^>]*>(.*?)</{tag}>", rec)]


def meta():
    state = json.load(open(STATE)) if STATE.exists() else {"token": None, "done": False}
    rows = json.load(open(META)) if META.exists() else []
    while not state["done"]:
        url = (f"{OAI}?verb=ListRecords&resumptionToken={urllib.parse.quote(state['token'])}" if state["token"]
               else f"{OAI}?verb=ListRecords&metadataPrefix=oai_dc&until={UNTIL}")
        x = get(url)
        for rec in re.findall(r"(?s)<record>(.*?)</record>", x):
            if 'status="deleted"' in rec:
                continue
            rows.append({"id": field(rec, "identifier")[0], "set": (field(rec, "setSpec") or [""])[0],
                         "datestamp": (field(rec, "datestamp") or [""])[0], "title": (field(rec, "dc:title") or [""])[0][:300],
                         "date": (field(rec, "dc:date") or [""])[0], "language": field(rec, "dc:language"),
                         "rights": field(rec, "dc:rights")[:3], "links": field(rec, "dc:identifier") + field(rec, "dc:relation"),
                         "abstract": (field(rec, "dc:description") or [""])[0][:4000]})
        tok = re.findall(r"<resumptionToken[^>]*>([^<]*)</resumptionToken>", x)
        state = {"token": tok[0] if tok and tok[0] else None, "done": not (tok and tok[0])}
        json.dump(rows, open(META, "w"), ensure_ascii=False); json.dump(state, open(STATE, "w"))
        print(len(rows), "live records so far", flush=True)


def text(limit):
    sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parents[1]))
    import subprocess, corpus
    from fetch_fulltext import chunks, CC
    rows = json.load(open(META))
    done = json.load(open(FULL)) if FULL.exists() else []
    log_path = HERE / "inlibrary_log.json"
    log = json.load(open(log_path)) if log_path.exists() else {}
    have = {d["id"] for d in done}
    cands = [r for r in rows if any(l.startswith(("uz", "uzb")) for l in r["language"]) and r["id"] not in have
             and r["id"] not in log]
    print(len(cands), "Uzbek candidates left", flush=True)
    for r in cands[:limit]:
        try:
            page = next(l for l in r["links"] if re.match(r"https?://inlibrary\.uz/index\.php/[^/]+/article/view/\d+$", l))
            html = get(page)
            m = CC.search(html)
            if not m:
                log[r["id"]] = "no CC licence on page"; continue
            galley = [urllib.parse.urljoin(page, unescape(g)) for g in re.findall(r'href="([^"]*/article/(?:view|download)/\d+/\d+)"', html)]
            if not galley:
                log[r["id"]] = "no galley"; continue
            data = get(galley[0].replace("/article/view/", "/article/download/"), binary=True)
            if not data.startswith(b"%PDF"):
                inner = re.findall(rb'href="([^"]*/article/download/[^"]+)"', data)
                if not inner:
                    log[r["id"]] = "not a PDF"; continue
                data = get(urllib.parse.urljoin(galley[0], unescape(inner[0].decode())), binary=True)
                if not data.startswith(b"%PDF"):
                    log[r["id"]] = "not a PDF"; continue
            tmp = HERE / "_inlibrary_tmp.pdf"; tmp.write_bytes(data)
            t = subprocess.run(["pdftotext", "-l", "40", str(tmp), "-"], capture_output=True, text=True, timeout=120).stdout
            tmp.unlink()
            ch = chunks(t)
            if not ch:
                log[r["id"]] = "no Uzbek passages"; continue
            done.append({"id": r["id"], "set": r["set"], "title": r["title"], "date": r["date"], "datestamp": r["datestamp"],
                         "license": f"cc-{m.group(1).lower()}-{m.group(2)}", "url": page, "chunks": ch})
            log[r["id"]] = f"kept {len(ch)}"
            json.dump(done, open(FULL, "w"), ensure_ascii=False)
        except Exception as e:
            log[r["id"]] = f"error: {type(e).__name__}"
        finally:
            json.dump(log, open(log_path, "w"))
        print(r["id"], log[r["id"]], flush=True)
    print(len(done), "articles,", sum(len(d["chunks"]) for d in done), "passages")


if __name__ == "__main__":
    if sys.argv[1] == "meta":
        meta()
    else:
        text(int(sys.argv[2]) if len(sys.argv) > 2 else 10**9)
