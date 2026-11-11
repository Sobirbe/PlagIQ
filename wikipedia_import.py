"""Import Wikipedia articles (CC BY-SA 4.0) into the private library from official dumps.

Streams https://dumps.wikimedia.org/<lang>wiki/latest/<lang>wiki-latest-pages-articles.xml.bz2
(or a local copy) without holding it in memory, keeps ordinary articles only
(namespace 0, not redirects), converts wiki markup to plain text, skips stubs,
and indexes each article with attribution: "Wikipedia contributors", the article
URL, the revision ID and the licence. A changed article replaces its previous
version. Progress is saved so daily runs continue where the last one stopped.

Manual run:  python wikipedia_import.py uz --batch 500 [--source /path/to/uzwiki-...xml.bz2]
Engine:      AUTO_IMPORT_WIKIPEDIA=uz  (WIKIPEDIA_BATCH=1000, WIKIPEDIA_MIN_WORDS=300)
"""
import argparse
import bz2
import hashlib
import html
import json
import os
import re
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

from corpus import connect, corpus_version, count_category, detect_language, ingest, inventory, remove
from harvest import USER_AGENT

DUMP_URL = "https://dumps.wikimedia.org/{lang}wiki/latest/{lang}wiki-latest-pages-articles.xml.bz2"
LICENCE = "CC BY-SA 4.0 (Wikipedia text licence: https://creativecommons.org/licenses/by-sa/4.0/)"
# Namespace prefixes for files, images and categories in links: en, ru, uz.
MEDIA = r"(?:file|image|category|файл|изображение|категория|fayl|tasvir|turkum|rasm)"


# ------------------------------------------------------------- wiki markup

def _strip_nested(text, open_, close):
    """Remove balanced {{...}} / {|...|} blocks, innermost first."""
    pattern = re.compile(re.escape(open_) + r"(?:(?!" + re.escape(open_) + r")(?!" + re.escape(close) + r").)*?"
                         + re.escape(close), re.S)
    for _ in range(50):
        text, n = pattern.subn("", text)
        if not n:
            break
    return text


def wikitext_to_text(markup):
    """Plain prose from wiki markup: templates, tables, references, files and
    categories removed; links reduced to their visible text."""
    t = re.sub(r"<!--.*?-->", "", markup, flags=re.S)
    t = re.sub(r"<ref[^>/]*/>", "", t, flags=re.I)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.I | re.S)
    t = re.sub(r"<(math|gallery|timeline|syntaxhighlight|source|score|graph)[^>]*>.*?</\1>", "", t, flags=re.I | re.S)
    t = _strip_nested(t, "{{", "}}")
    t = _strip_nested(t, "{|", "|}")
    # Table rows left by unbalanced tables: lines starting with {| |- |+ |} | or !
    t = re.sub(r"^[ \t]*(?:\{\||\|[-+}]?|!).*$", "", t, flags=re.M)
    # Links, innermost first: media/category links vanish, others keep their visible text.
    media = re.compile(r"\[\[\s*" + MEDIA + r"\s*:[^\[\]]*\]\]", re.I)
    link = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]]*)\]\]")
    for _ in range(10):
        before = t
        t = media.sub("", t)
        t = link.sub(r"\1", t)
        if t == before:
            break
    t = re.sub(r"\[https?://[^\s\]]+\s*([^\]]*)\]", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = t.replace("[[", "").replace("]]", "")
    t = re.sub(r"'{2,}", "", t)
    t = re.sub(r"^[ \t]*=+\s*(.*?)\s*=+\s*$", r"\1", t, flags=re.M)
    t = re.sub(r"^[*#:;]+\s*", "", t, flags=re.M)
    t = re.sub(r"__[A-ZА-Я_]+__", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


# ------------------------------------------------------------------ dumps

def open_dump(source):
    """Binary stream of the dump: an https URL (Wikimedia hosts only) or a local file."""
    if str(source).startswith(("https://", "http://")):
        parts = urlsplit(str(source))
        if parts.scheme != "https" or not (parts.hostname or "").endswith(".wikimedia.org"):
            raise ValueError("dump URL must be https on a wikimedia.org host")
        response = urllib.request.urlopen(urllib.request.Request(str(source), headers={"User-Agent": USER_AGENT}), timeout=60)
        raw = response
    else:
        raw = open(source, "rb")
    return bz2.BZ2File(raw) if str(source).endswith(".bz2") else raw


def iter_articles(stream):
    """(page_id, title, revision_id, year, markup) for main-namespace, non-redirect pages."""
    page, root = {}, None
    for event, element in ElementTree.iterparse(stream, events=("start", "end")):
        if root is None:
            root = element                                  # <mediawiki>; emptied below to keep memory flat
        if event == "start":
            continue
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "title" and "title" not in page:
            page["title"] = element.text or ""
        elif tag == "ns":
            page["ns"] = element.text
        elif tag == "id" and "id" not in page:
            page["id"] = element.text
        elif tag == "redirect":
            page["redirect"] = True
        elif tag == "revision":
            page["rev"] = next((c.text for c in element if c.tag.rsplit("}", 1)[-1] == "id"), None)
            stamp = next((c.text for c in element if c.tag.rsplit("}", 1)[-1] == "timestamp"), "") or ""
            page["year"] = int(stamp[:4]) if stamp[:4].isdigit() else None
            page["text"] = next((c.text for c in element if c.tag.rsplit("}", 1)[-1] == "text"), "") or ""
        elif tag == "page":
            if page.get("ns") == "0" and not page.get("redirect") and page.get("id"):
                yield int(page["id"]), page.get("title", ""), page.get("rev"), page.get("year"), page.get("text", "")
            page = {}
            element.clear()
            root.clear()


# ----------------------------------------------------------------- import

def article_url(lang, title):
    return f"https://{lang}.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="()_,'-")


def run(lang, batch=1000, min_words=300, source=None, db_path=None, root=None):
    root = Path(root or os.environ.get("STAGING_DIR") or Path(os.environ.get("CORPUS_DB", "corpus.sqlite3")).resolve().parent / "staging")
    state_path = root / f"wikipedia-{lang}.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"position": 0, "pages": {}}
    source = source or DUMP_URL.format(lang=lang)
    counts = {"imported": 0, "updated": 0, "unchanged": 0, "skipped_short": 0, "skipped_language": 0, "failed": 0}
    error, position, finished, failures = None, 0, False, []
    today = datetime.now(timezone.utc).date().isoformat()
    cap = max_articles()
    try:
        with closing(connect(db_path)) as db, closing(open_dump(source)) as stream:
            have = count_category(db, "wikipedia", lang)
            for position, (page_id, title, rev, year, markup) in enumerate(iter_articles(stream), 1):
                if position <= state["position"]:
                    continue                                   # done in an earlier run
                if have + counts["imported"] >= cap and not state["pages"].get(str(page_id)):
                    error = f"Wikipedia limit reached ({cap} {lang} articles, WIKIPEDIA_MAX_ARTICLES)"
                    position -= 1
                    break
                if counts["imported"] + counts["updated"] >= batch:
                    position -= 1
                    break
                text = wikitext_to_text(markup)
                if len(text.split()) < min_words:
                    counts["skipped_short"] += 1
                    continue
                if detect_language(text) not in (None, lang):
                    counts["skipped_language"] += 1
                    continue
                meta = {"title": f"{title} — Wikipedia", "authors": "Wikipedia contributors", "year": year or int(today[:4]),
                        "url": article_url(lang, title), "language": lang, "rights_basis": "open_license",
                        "permission_reference": f"{LICENCE}; revision {rev}; retrieved {today} from Wikimedia dump",
                        "source_type": "wikipedia"}
                previous = state["pages"].get(str(page_id))
                if previous and previous == hashlib.sha256(text.encode()).hexdigest():
                    counts["unchanged"] += 1
                    continue
                try:
                    with db:
                        if previous:
                            remove(db, previous)
                    identifier = ingest(db, meta, text)
                    state["pages"][str(page_id)] = identifier
                    counts["updated" if previous else "imported"] += 1
                except ValueError as exc:
                    if "Index limit" in str(exc):
                        error = str(exc)
                        position -= 1
                        break
                    counts["failed"] += 1
                    if len(failures) < 10:
                        failures.append(f"{title}: {str(exc)[:120]}")
            else:
                finished = True
            sources, version = len(inventory(db)), corpus_version(db)
    except (OSError, EOFError, ElementTree.ParseError, ValueError) as exc:
        error, sources, version = type(exc).__name__ + ": " + str(exc)[:200], None, None
    if finished:
        state["position"] = 0                              # next run starts again and picks up changed articles
    elif position:
        state["position"] = max(state["position"], position)
    root.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), "utf-8")
    status = {"language": lang, "source": "wikipedia", "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              **counts, "failure_examples": failures, "dump_complete": finished, "error": error, "corpus_sources": sources, "corpus_version": version}
    status_path = root / "import-status.json"
    data = json.loads(status_path.read_text("utf-8")) if status_path.exists() else {}
    data[f"wikipedia-{lang}"] = status
    status_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    return status


def max_articles():
    """Per-language cap, so Wikipedia (fast to import) cannot fill the library meant for academic sources."""
    try:
        return max(0, int(os.environ.get("WIKIPEDIA_MAX_ARTICLES", "2000")))
    except ValueError:
        return 2000


def settings():
    langs = [l.strip() for l in os.environ.get("AUTO_IMPORT_WIKIPEDIA", "").split(",") if l.strip() in ("uz", "ru", "en")]
    def bounded(name, default, low, high):
        try:
            return min(max(int(os.environ.get(name, default)), low), high)
        except ValueError:
            return default
    return langs, bounded("WIKIPEDIA_BATCH", 1000, 1, 20000), bounded("WIKIPEDIA_MIN_WORDS", 300, 50, 5000)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Wikipedia articles (CC BY-SA 4.0) from a Wikimedia dump.")
    parser.add_argument("language", choices=["uz", "ru", "en"])
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--min-words", type=int, default=300)
    parser.add_argument("--source", help="dump URL on dumps.wikimedia.org or a local .xml/.xml.bz2 file")
    a = parser.parse_args()
    print(json.dumps(run(a.language, a.batch, a.min_words, a.source), ensure_ascii=False, indent=1))
