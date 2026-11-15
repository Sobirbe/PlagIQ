"""Uzbek academic abstracts published before ChatGPT (2010-2021) from OpenAlex, language confirmed by PlagIQ's detector."""
import json, sys, time, urllib.request
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import corpus
out, cursor, pages = [], "*", 0
while cursor and pages < 30:
    url = ("https://api.openalex.org/works?filter=language:uz,publication_year:2010-2021,has_abstract:true"
           "&select=id,title,publication_year,abstract_inverted_index,primary_topic&per_page=200&cursor=" + cursor)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer proxy-injected", "User-Agent": "PlagIQ-research"})
    data = json.load(urllib.request.urlopen(req, timeout=60))
    pages += 1
    for w in data["results"]:
        inv = w.get("abstract_inverted_index") or {}
        pos = {}
        for word, places in inv.items():
            for p in places:
                pos[p] = word
        text = " ".join(pos[k] for k in sorted(pos))
        if len(text.split()) < 60 or corpus.detect_language(text) != "uz":
            continue
        out.append({"id": w["id"], "year": w["publication_year"], "title": w.get("title") or "",
                    "field": ((w.get("primary_topic") or {}).get("field") or {}).get("display_name", ""), "text": text})
    cursor = data["meta"].get("next_cursor")
    time.sleep(0.2)
print(pages, "pages,", len(out), "Uzbek abstracts kept")
# also works by authors at Uzbek institutions that OpenAlex labels Russian or English but whose text is Uzbek
ids = {o["id"] for o in out}
for lang in ("ru", "en"):
    cursor, pages = "*", 0
    while cursor and pages < 25:
        url = (f"https://api.openalex.org/works?filter=language:{lang},authorships.institutions.country_code:UZ,publication_year:2010-2021,"
               "has_abstract:true&select=id,title,publication_year,abstract_inverted_index,primary_topic&per_page=200&cursor=" + cursor)
        data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer proxy-injected"}), timeout=60))
        pages += 1
        for w in data["results"]:
            pos = {p: word for word, places in (w.get("abstract_inverted_index") or {}).items() for p in places}
            text = " ".join(pos[k] for k in sorted(pos))
            if w["id"] in ids or len(text.split()) < 60 or corpus.detect_language(text) != "uz":
                continue
            ids.add(w["id"])
            out.append({"id": w["id"], "year": w["publication_year"], "title": w.get("title") or "",
                        "field": ((w.get("primary_topic") or {}).get("field") or {}).get("display_name", ""), "text": text})
        cursor = data["meta"].get("next_cursor"); time.sleep(0.2)
json.dump(out, open("human_uz.json", "w"), ensure_ascii=False)
from collections import Counter
print(Counter(o["field"] for o in out).most_common(12))
print(Counter(o["year"] for o in out).most_common())
