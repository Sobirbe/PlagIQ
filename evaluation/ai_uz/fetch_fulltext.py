"""Full texts of open-access Uzbek works published before ChatGPT (2010-2021), for the full-length part of the study.

Rules: a work is downloaded only if (1) OpenAlex lists it as open access with a Creative Commons licence, (2) the same
licence is stated on the work's own landing page (checked here, not taken on trust), and (3) the site's robots.txt allows
the page and the file for a generic crawler; the site's Crawl-delay is respected (at least 2 s between requests to a
host). Texts are other authors' work: they stay out of the repository (human_full_uz.json is gitignored) and are used only
to measure the detector. Only passages PlagIQ's language detector reads as Uzbek are kept.

    python3 fetch_fulltext.py [max_works]      # resumable; cache and PDFs live in $FULLTEXT_CACHE (default ./cache_fulltext)
"""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request, urllib.robotparser
from html import unescape
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import corpus

UA = "PlagIQ-research"
CACHE = os.environ.get("FULLTEXT_CACHE", "cache_fulltext"); os.makedirs(CACHE, exist_ok=True)
OUT = "human_full_uz.json"
LOG = os.path.join(CACHE, "log.json")
CC = re.compile(r"creativecommons\.org/licenses/(by(?:-nc)?(?:-sa|-nd)?)/(\d\.\d)", re.I)
SKIP_HOSTS = {"vital.lib.tsu.ru",          # Russian papers mislabelled Uzbek by OpenAlex
              "cyberleninka.ru"}           # robots.txt forbids PDFs
_robots, _last = {}, {}

def allowed(url):
    p = urllib.parse.urlparse(url); host = p.netloc
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            raw = urllib.request.urlopen(urllib.request.Request(f"{p.scheme}://{host}/robots.txt", headers={"User-Agent": UA}), timeout=30).read()
            rp.parse(raw.decode("utf-8", "replace").splitlines())
        except Exception:
            rp.parse([])                    # no robots.txt: everything allowed
        _robots[host] = rp
    return _robots[host].can_fetch(UA, url)

def get(url, binary=False, limit=25_000_000):
    host = urllib.parse.urlparse(url).netloc
    rp = _robots.get(host); delay = max(2.0, float((rp and rp.crawl_delay(UA)) or 0))
    wait = _last.get(host, 0) + delay - time.time()
    if wait > 0: time.sleep(wait)
    _last[host] = time.time()
    r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=90)
    data = r.read(limit + 1)
    if len(data) > limit: raise ValueError("file too large")
    return (data, r.geturl()) if binary else (data.decode("utf-8", "replace"), r.geturl())

def candidates():
    out, cursor = [], "*"
    while cursor:
        url = ("https://api.openalex.org/works?filter=language:uz,publication_year:2010-2021,is_oa:true"
               "&select=id,title,publication_year,best_oa_location,type&per_page=200&cursor=" + cursor)
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer proxy-injected", "User-Agent": UA}), timeout=60))
        for w in d["results"]:
            b = w.get("best_oa_location") or {}
            if (b.get("license") or "").startswith("cc") and b.get("landing_page_url"):
                out.append({"id": w["id"], "title": w.get("title") or "", "year": w["publication_year"], "type": w["type"],
                            "license": b["license"], "landing": b["landing_page_url"], "pdf": b.get("pdf_url")})
        cursor = d["meta"].get("next_cursor"); time.sleep(0.2)
    return out

def pdf_links(html, base):
    links = re.findall(r'<meta[^>]+name="citation_pdf_url"[^>]+content="([^"]+)"', html, re.I)
    links += re.findall(r'href="([^"]+/article/download/[^"]+)"', html)
    return [urllib.parse.urljoin(base, unescape(l)) for l in links]

def chunks(text):
    """Passages of ~120-200 words, sentence-aligned, Uzbek only, reference lists and tables dropped."""
    text = re.sub(r"-\n(?=[a-zа-я])", "", text)                       # hyphenated line breaks
    text = re.sub(r"\s*\n\s*", " ", text); text = re.sub(r"\s+", " ", text)
    text = re.split(r"\b(?:ADABIYOTLAR|Adabiyotlar|FOYDALANILGAN ADABIYOTLAR|Foydalanilgan adabiyotlar|REFERENCES|СПИСОК ЛИТЕРАТУРЫ|Литература)\b", text)[0]
    out, cur = [], []
    for s in re.split(r"(?<=[.!?])\s+", text):
        cur.append(s)
        if len(" ".join(cur).split()) >= 120:
            p = " ".join(cur); cur = []
            digits = sum(c.isdigit() for c in p) / len(p)
            if len(p.split()) <= 260 and digits < 0.08 and corpus.detect_language(p) == "uz":
                out.append(p)
    return out

def fetch(c):
    m = re.match(r"https?://doi\.org/10\.5281/zenodo\.(\d+)", c["landing"], re.I)
    if m: c = dict(c, landing=f"https://zenodo.org/records/{m.group(1)}")   # Zenodo DOIs: go to the record directly
    if re.match(r"https?://doi\.org/10\.17223/", c["landing"]): return "skipped host"   # Tomsk: Russian papers
    host = urllib.parse.urlparse(c["landing"]).netloc
    if host in SKIP_HOSTS or (c["pdf"] and urllib.parse.urlparse(c["pdf"]).netloc in SKIP_HOSTS): return "skipped host"
    if not allowed(c["landing"]): return "robots: landing page"
    html, final = get(c["landing"])
    if urllib.parse.urlparse(final).netloc in SKIP_HOSTS: return "skipped host"
    if not allowed(final): return "robots: landing page"
    m = CC.search(html)
    if not m: return "no CC licence on landing page"
    lic = f"cc-{m.group(1).lower()}-{m.group(2)}"
    if "zenodo.org" in final:
        rec = re.search(r"/records?/(\d+)", final).group(1)
        files_url = f"https://zenodo.org/api/records/{rec}/files"
        if not allowed(files_url): return "robots: files"
        entries = json.loads(get(files_url)[0])["entries"]
        pdfs = [e["links"]["content"] for e in entries if e["key"].lower().endswith(".pdf")]
    else:
        pdfs = ([c["pdf"]] if c["pdf"] else []) + pdf_links(html, final)
    pdfs = [p for p in dict.fromkeys(pdfs) if allowed(p)]
    if not pdfs: return "no allowed PDF link"
    data, _ = get(pdfs[0], binary=True)
    if not data.startswith(b"%PDF"):                               # OJS "download" pages sometimes wrap the file
        inner = re.findall(rb'href="([^"]+/download/[^"]+)"', data)
        if inner:
            u = urllib.parse.urljoin(pdfs[0], unescape(inner[0].decode()))
            if not allowed(u): return "robots: file"
            data, _ = get(u, binary=True)
    if not data.startswith(b"%PDF"): return "not a PDF"
    path = os.path.join(CACHE, re.sub(r"\W", "_", c["id"].rsplit("/", 1)[-1]) + ".pdf")
    open(path, "wb").write(data)
    text = subprocess.run(["pdftotext", "-layout", "-l", "60", path, "-"], capture_output=True, text=True, timeout=120).stdout
    text = subprocess.run(["pdftotext", "-l", "60", path, "-"], capture_output=True, text=True, timeout=120).stdout or text
    ch = chunks(text)
    if not ch: return "no Uzbek passages"
    return {"id": c["id"], "title": c["title"], "year": c["year"], "type": c["type"], "license": lic, "url": final, "chunks": ch}

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10**9
    cands = candidates(); print(len(cands), "candidates with a CC licence in OpenAlex", flush=True)
    done = json.load(open(OUT)) if os.path.exists(OUT) else []
    log = json.load(open(LOG)) if os.path.exists(LOG) else {}
    have = {d["id"] for d in done}
    for c in cands[:limit]:
        if c["id"] in have or c["id"] in log: continue
        try: r = fetch(c)
        except Exception as e: r = f"error: {type(e).__name__}: {str(e)[:80]}"
        if isinstance(r, dict):
            done.append(r); json.dump(done, open(OUT, "w"), ensure_ascii=False); r = f"kept {len(r['chunks'])} passages"
        log[c["id"]] = r; json.dump(log, open(LOG, "w"), ensure_ascii=False, indent=0)
        print(c["id"].rsplit("/", 1)[-1], urllib.parse.urlparse(c["landing"]).netloc, "->", r, flush=True)
    from collections import Counter
    print(Counter(v if not v.startswith("kept") else "kept" for v in log.values()).most_common())
    print(len(done), "works,", sum(len(d["chunks"]) for d in done), "Uzbek passages")
