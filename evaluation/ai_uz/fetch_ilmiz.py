"""Pre-ChatGPT Uzbek abstracts from ilmiz.uz, the index of journals on the Higher Attestation Commission (OAK) list.

ilmiz.uz keeps metadata only (title, authors, journal, date, language, abstract) and links each article to the
publisher's page; its terms (ilmiz.uz/shartlar) make search and metadata free for everyone and leave rights with the
authors and publishers. robots.txt allows /api/ (only /api/admin, /api/auth, /api/citations and /qidiruv are closed).
The owner asked for it to be used for training. This script reads the public /api/articles listing year by year
(2010-2022), two seconds apart, and keeps abstracts the index marks Uzbek and published before ChatGPT (30 November
2022). Abstracts are other authors' work: ilmiz_*.json stay out of the repository and are used only to train and
measure. No full text is taken here; full texts come only from publishers' sites under an open licence (uz_journals.py).

    python3 fetch_ilmiz.py            # resumable; writes ilmiz_abstracts_uz.json
"""
import json, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
API = "https://ilmiz.uz/api/articles"
UA = {"User-Agent": "PlagIQ-research", "Accept": "application/json"}
OUT, STATE = HERE / "ilmiz_abstracts_uz.json", HERE / "ilmiz_state.json"
YEARS = range(2010, 2023)
UNTIL = "2022-11-30"
PAGE, DELAY = 500, 2.0
_last = [0.0]


def get(url, tries=4):
    for attempt in range(tries):
        wait = _last[0] + DELAY * (1 + 2 * attempt) - time.time()
        if wait > 0: time.sleep(wait)
        _last[0] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return json.load(r)
        except (ConnectionError, TimeoutError, urllib.error.URLError, ValueError):
            if attempt == tries - 1: raise


def keep(a):
    date = str(a.get("publicationDate") or "")
    text = " ".join(str(a.get("abstract") or "").split())
    return (a.get("language") == "O‘zbek" and date[:4].isdigit() and date[:10] < UNTIL and len(text.split()) >= 40
            and not text.endswith("…"))


def main():
    out = json.load(open(OUT)) if OUT.exists() else {}
    state = json.load(open(STATE)) if STATE.exists() else {}
    for year in YEARS:
        offset = state.get(str(year), 0)
        while offset is not None:
            page = get(f"{API}?year={year}&limit={PAGE}&offset={offset}")
            for a in page:
                if keep(a):
                    out[a["id"]] = {"id": a["id"], "title": a.get("title"), "journal": a.get("journalId"),
                                    "date": a["publicationDate"][:10], "year": a.get("year"), "fields": a.get("fields"),
                                    "url": a.get("landingUrl"), "abstract": " ".join(a["abstract"].split())}
            offset = offset + PAGE if len(page) == PAGE else None
            state[str(year)] = offset
            json.dump(out, open(OUT, "w"), ensure_ascii=False); json.dump(state, open(STATE, "w"))
        print(year, sum(1 for x in out.values() if x["year"] == year), "kept", flush=True)
    print(len(out), "Uzbek pre-ChatGPT abstracts from", len({x["journal"] for x in out.values()}), "journals")


if __name__ == "__main__":
    main()
