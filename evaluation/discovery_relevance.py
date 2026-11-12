"""Calibration of discovery.MIN_RELEVANCE: for each topic, a long test document is built from 25 real
OpenAlex abstracts, discovery is run on it, and every result is printed with its relevance score.
Usage: python evaluation/discovery_relevance.py OUT.json   (needs network access to api.openalex.org)
Results: evaluation/DISCOVERY.md."""
import sys, json, os, random
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import discovery
key = os.environ.get("OPENALEX_API_KEY")
topics = [("en", "pricing to market exchange rate pass-through"), ("en", "teacher professional development rural schools"),
          ("en", "antibiotic resistance hospital surveillance"), ("en", "DevOps continuous delivery software teams"),
          ("ru", "инфляция денежно-кредитная политика"), ("ru", "компетентностный подход высшее образование"),
          ("uz", "ta'lim tizimi o'qituvchi"), ("uz", "qishloq xo'jaligi iqtisodiyot")]
rows = []
for lang, q in topics:
    params = {"search": q, "filter": f"language:{lang},has_abstract:true", "per_page": "25", "select": "id,display_name,abstract_inverted_index"}
    req = Request("https://api.openalex.org/works?" + urlencode(params), headers={"User-Agent": "PlagIQ-calibration", **({"Authorization": "Bearer " + key} if key else {})})
    works = json.load(urlopen(req, timeout=30))["results"]
    parts = [w["display_name"] + ". " + discovery.reconstruct_abstract(w["abstract_inverted_index"]) for w in works]
    doc = q.capitalize() + "\n\n" + "\n\n".join(parts)
    own = {w["id"] for w in works}
    stems = discovery.document_stems(doc, lang)
    found = discovery.discover(doc, 8, lang)
    print(f"\n=== {lang} '{q}': {len(doc.split())} words, {found['queries']} queries, {len(found['sources'])} results", flush=True)
    for s in found["sources"]:
        r = discovery.relevance(s, stems, lang)
        rows.append({"topic": q, "lang": lang, "title": s["title"], "score": r, "abstract": bool(s.get("abstract"))})
        print(f"  {str(r):>6}  {'A' if s.get('abstract') else '-'}  {s['title'][:95]}")
json.dump(rows, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
