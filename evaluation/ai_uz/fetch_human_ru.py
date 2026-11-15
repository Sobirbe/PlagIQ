"""Russian academic abstracts published before ChatGPT (2010-2021) by authors at Uzbek institutions, from OpenAlex,
language confirmed by PlagIQ's detector. The Russian counterpart of fetch_human.py (same rules; human_ru.json is not kept
in the repository)."""
import json, sys, time, urllib.request
from collections import Counter
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import corpus
out, cursor, pages = [], "*", 0
while cursor and pages < 30:
    url = ("https://api.openalex.org/works?filter=language:ru,authorships.institutions.country_code:UZ,publication_year:2010-2021,"
           "has_abstract:true&select=id,title,publication_year,abstract_inverted_index,primary_topic&per_page=200&cursor=" + cursor)
    data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer proxy-injected", "User-Agent": "PlagIQ-research"}), timeout=60))
    pages += 1
    for w in data["results"]:
        pos = {p: word for word, places in (w.get("abstract_inverted_index") or {}).items() for p in places}
        text = " ".join(pos[k] for k in sorted(pos))
        if len(text.split()) < 60 or corpus.detect_language(text) != "ru":
            continue
        out.append({"id": w["id"], "year": w["publication_year"], "title": w.get("title") or "",
                    "field": ((w.get("primary_topic") or {}).get("field") or {}).get("display_name", ""), "text": text})
    cursor = data["meta"].get("next_cursor"); time.sleep(0.2)
json.dump(out, open("human_ru.json", "w"), ensure_ascii=False)
print(pages, "pages,", len(out), "Russian abstracts kept")
print(Counter(o["field"] for o in out).most_common(10))
print(sorted(Counter(o["year"] for o in out).items()))
