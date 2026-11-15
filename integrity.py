"""Writing-integrity signals: verifiable indicators worth a supervisor's review.

This is NOT an AI detector and never produces an AI probability or percentage.
No method can reliably label a text as AI-written, "humanized" or partly AI
(detectors misjudge non-native writers and are untested for Uzbek). Instead, each
signal points to concrete places a person can check:

  chatbot_phrases     leftover assistant phrases ("As an AI language model", "Certainly! Here is")
  placeholders        unfilled template slots ("[Insert citation]", "(Author, Year)")
  hidden_characters   zero-width, bidirectional-control and tag characters (used to fool checkers)
  mixed_script        words mixing Latin and Cyrillic look-alike letters (used to fool checkers)
  markdown            leftover chat formatting (## headings, **bold**, ``` fences)
  doi_not_found       DOIs in the reference list that do not exist (doi.org)
  doi_mismatch        DOIs that exist but belong to a different work than the one cited
  reference_not_found English journal-style references WITHOUT a DOI whose title OpenAlex cannot find (at least 2).
                      Invented references are the most common trace of chatbot-written theses, but a reference can
                      also be missing from OpenAlex for honest reasons, so this asks the reader to check, nothing more.
                      Uzbek and Russian references are not looked up this way: OpenAlex covers much of the local
                      literature poorly, and "not found" would wrongly suggest invention.

Chatbot phrases are written once in Uzbek Latin (UZ_CHATBOT); the Uzbek Cyrillic forms are generated from them by
uzcyr.py, so both scripts are always covered alike.

The reference checks send only DOIs and reference titles (never the manuscript) to OpenAlex (single-work lookups are
free; a title search costs about $0.001, at most MAX_TITLES per check) and, for DOIs OpenAlex does not know, to
doi.org. They run only with the user's consent (academic mode).
Settings: INTEGRITY_DOI_CHECK=0 turns both reference checks off; INTEGRITY_TITLE_CHECK=0 only the title check.
"""
import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import quote

import citations

_A = r"[ʼʻ'’‘`´]"
CHATBOT = [re.compile(p, re.IGNORECASE) for p in (
    # English
    r"\bas an ai(?: language model| assistant| model)?\b", r"\bas a (?:large )?language model\b",
    r"\bi(?:'m| am) (?:just )?an ai\b", r"\bi (?:cannot|can't|am unable to|don't have the ability to) "
    r"(?:browse|access) (?:the internet|real-time|external)", r"\bmy (?:knowledge|training)(?: data)? cut-?off\b",
    r"\bas of my last (?:knowledge )?update\b", r"\bregenerate response\b",
    r"\b(?:certainly|sure|absolutely|of course)[!,.] here(?:'s| is| are)\b",
    r"\bhere(?:'s| is) (?:a |an |the )?(?:revised|rewritten|improved|polished|humani[sz]ed|paraphrased|refined) "
    r"(?:version|text|paragraph)\b", r"\bi hope this helps\b", r"\blet me know if you(?:'d| would)? (?:like|need|want)\b",
    # Russian
    r"\bкак (?:языковая модель|ии|искусственный интеллект|модель искусственного интеллекта)\b",
    r"\bя не могу (?:просматривать|получить доступ к) (?:интернет|актуальн)", r"\bконечно[!,.] вот\b",
    r"\bвот (?:переработанн|исправленн|улучшенн|перефразированн|отредактированн)\w* (?:вариант|версия|текст)\b",
    r"\bнадеюсь,? это (?:поможет|было полезно)\b", r"\bдайте знать,? если\b",
    # Uzbek (Latin and Cyrillic)
    rf"\bsun{_A}?iy intellekt(?: modeli)? sifatida\b", r"\balbatta[!,.] mana\b",
    r"\bumid qilamanki,? bu (?:sizga )?(?:yordam beradi|foydali bo)",
    r"\bmana (?:qayta ishlangan|tahrirlangan|yaxshilangan|qayta yozilgan) (?:variant|matn|versiya)\b",
    r"\bсунъий интеллект(?: модели)? сифатида\b", r"\bалбатта[!,.] мана\b", r"\bумид қиламанки,? бу (?:сизга )?ёрдам беради\b",
    # more English and Russian assistant phrases (the assistant talking to its user)
    r"\bgreat question\b", r"\bif you have any (?:other |further |more )?questions\b", r"\bfeel free to ask\b",
    r"\bi(?:'d| would) be happy to help\b", r"\bi don't have (?:real-time|access to real-time)\b",
    r"\bесли у вас (?:есть|возникнут|появятся) (?:ещё |еще |другие |дополнительные )?вопросы\b",
    r"\bчем (?:ещё|еще) (?:я )?могу (?:вам )?помочь\b", r"\bотличный вопрос\b",
    r"\bя\s*(?:—|-)?\s*(?:языковая модель|ии-ассистент|виртуальный ассистент)\b",
    r"\bу меня нет доступа к (?:интернету|актуальн)", r"\bконечно[!,.] (?:ниже|давайте)\b",
    r"\bниже представлен\w* (?:переработанн|улучшенн|исправленн|отредактированн)",
)]
# Uzbek assistant phrases, written once in Latin with ʻ/ʼ; the Cyrillic forms come from uzcyr.py. Only phrases in
# which the assistant speaks to its user, so that human academic prose does not match them.
UZ_CHATBOT = (
    "men sunʼiy intellektman", "men sunʼiy intellekt modeliman", "men til modeliman", "men sunʼiy intellekt yordamchisiman",
    "albatta, quyida", "albatta! quyida", "albatta, bu mavzu boʻyicha",
    "soʻrovingiz boʻyicha tayyorlangan", "mana sizning soʻrovingiz boʻyicha", "siz soʻragan matn", "quyida siz soʻragan",
    "agar qoʻshimcha savollaringiz boʻlsa", "agar yana savollaringiz boʻlsa", "savollaringiz boʻlsa, bemalol soʻrang",
    "sizga yana qanday yordam bera olaman", "yana nima bilan yordam bera olaman", "yana qanday yordam kerak boʻlsa",
    "ajoyib savol", "matnni qayta yozib berdim", "matnni tahrirlab berdim",
    "internetga kirish imkoniyatim yoʻq", "real vaqt rejimidagi maʼlumotlarga ega emasman",
)


def _uz_pattern(phrase):
    """A regex for a Latin phrase: any apostrophe form (or none), any whitespace between words."""
    out = []
    for ch in phrase:
        if ch in "ʻʼ'’‘`":
            out.append(_A + "?")
        elif ch.isspace():
            out.append(r"\s+")
        else:
            out.append(re.escape(ch))
    return re.compile(r"(?<!\w)" + "".join(out) + r"(?!\w)", re.IGNORECASE)


def _uz_patterns():
    import uzcyr
    pats = []
    for phrase in UZ_CHATBOT:
        pats.append(_uz_pattern(phrase))
        cyr = uzcyr.to_cyrillic(phrase)
        pats.append(re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in cyr.split()) + r"(?!\w)", re.IGNORECASE))
    return pats


CHATBOT += _uz_patterns()
PLACEHOLDER = re.compile(
    r"\[(?:insert|your|author|citation needed|add |source|reference|name|date|вставьте|ваше|укажите|автор|источник"
    r"|muallif|manba|ism|kiriting)[^\]\n]{0,50}\]"
    r"|\((?:author|автор|muallif|муаллиф),? (?:year|год|yil|йил)\)", re.IGNORECASE)
HIDDEN = re.compile("[​‌‍⁠⁡⁢⁣⁤﻿‪-‮⁦-⁩\U000e0000-\U000e007f]")
WORD = re.compile(r"[^\W\d_]+(?:[ʼʻ'’`][^\W\d_]+)*")       # hyphen splits: "IT-компания" is fine
LATIN = re.compile(r"[A-Za-z]")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
MARKDOWN = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S|\*\*[^*\n]{2,80}\*\*|^[ \t]*```", re.MULTILINE)
DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>{}|\\^\[\]]+", re.IGNORECASE)
# Look-alike letters only: Cyrillic letters that look Latin and the reverse. Letters such as ӯ, ь or
# ž in a word of the other script are transliteration, not disguise, and are ignored.
CYR_LOOKALIKE = set("аеорсухіјѕԁԛԝӏАВЕКМНОРСТХІЈЅԚԜ")
LAT_LOOKALIKE = set("aeopcyxijsdqwlABEKMHOPCTXIJSQW")
# Thresholds were set on 900 Wikipedia articles (evaluation/INTEGRITY.md): stray zero-width
# characters and keyboard-layout slips occur in genuine text, disguise tools insert many.
THRESHOLDS = {"chatbot_phrases": 1, "placeholders": 1, "hidden_characters": 10, "mixed_script": 5, "markdown": 3}
MAX_DOIS = 25
MAX_TITLES = 12                 # English references without a DOI looked up by title, per check
TITLE_SIGNAL_MIN = 2
OPENALEX_SEARCH = "https://api.openalex.org/works"
MAX_EXAMPLES = 5
OPENALEX = "https://api.openalex.org/works/doi:"
HANDLE = "https://doi.org/api/handles/"
STOP = {"the", "and", "for", "with", "from", "into", "that", "this", "their", "über", "und",
        "для", "при", "как", "что", "это", "или", "uchun", "bilan"}


def _context(text, start, end, width=50):
    left = text[max(0, start - width):start]
    right = text[end:end + width]
    piece = text[start:end]
    shown = "".join(f"⟦U+{ord(c):04X}⟧" if HIDDEN.match(c) else c for c in piece)
    clean = lambda s: " ".join(HIDDEN.sub("", s).split())
    edge = lambda s, side: (" " if s[side:][:1].isspace() or s[side - 1:][:1].isspace() else "") if s else ""
    return (("…" if start > width else "") + clean(left) + edge(left, -1) + "»" + shown + "«" + edge(right, 0)
            + clean(right) + ("…" if end + width < len(text) else ""))


def local_signals(text):
    """{signal: (count, [examples])} for the signals that need no network."""
    found = {}
    hits = []
    for begin, end in sorted((m.start(), m.end()) for p in CHATBOT for m in p.finditer(text)):
        if hits and begin <= hits[-1][1]:                   # overlapping phrases count once
            hits[-1] = (hits[-1][0], max(end, hits[-1][1]))
        else:
            hits.append((begin, end))
    found["chatbot_phrases"] = (len(hits), [_context(text, s, e) for s, e in hits[:MAX_EXAMPLES]])
    hits = [(m.start(), m.end()) for m in PLACEHOLDER.finditer(text)]
    found["placeholders"] = (len(hits), [_context(text, s, e) for s, e in hits[:MAX_EXAMPLES]])
    hits = [m.start() for m in HIDDEN.finditer(text) if not (m.start() == 0 and m.group() == "﻿")]
    shown = []                                              # one example per cluster of nearby characters
    for pos in hits:
        if not shown or pos - shown[-1] > 60:
            shown.append(pos)
    found["hidden_characters"] = (len(hits), [_context(text, s, s + 1, 30) for s in shown[:MAX_EXAMPLES]])
    mixed = [m for m in WORD.finditer(text) if _disguised(m.group())]
    found["mixed_script"] = (len(mixed), [_scripts(m.group()) for m in mixed[:MAX_EXAMPLES]])
    hits = [(m.start(), m.end()) for m in MARKDOWN.finditer(text)]
    found["markdown"] = (len(hits), [_context(text, s, e, 30) for s, e in hits[:MAX_EXAMPLES]])
    return found


def _disguised(word):
    """A word mostly in one script with look-alike letters from the other."""
    lat, cyr = LATIN.findall(word), CYRILLIC.findall(word)
    if not lat or not cyr:
        return False
    minority, lookalike = (cyr, CYR_LOOKALIKE) if len(cyr) <= len(lat) else (lat, LAT_LOOKALIKE)
    return all(c in lookalike for c in minority)


def _scripts(word):
    """The word with its minority-script letters in ⟨ ⟩, e.g. 'P⟨а⟩per' (Cyrillic а in a Latin word)."""
    cyr = len(CYRILLIC.findall(word))
    minority = CYRILLIC if cyr <= len(LATIN.findall(word)) else LATIN
    return "".join(f"⟨{c}⟩" if minority.match(c) else c for c in word)


# ----------------------------------------------------------------- references

def reference_dois(text):
    """[(doi, entry text)] from the reference list (or the whole text if none is found)."""
    span = citations.bibliography_span(text)
    part = text[span[0]:span[1]] if span else text
    out, seen = [], set()
    for m in DOI.finditer(part):
        doi = m.group().rstrip(".,;:)]}>'\"”’").lower()
        doi = re.sub(r"(?:\.|,)?(?:pdf|html?)$", "", doi) if doi.endswith((".pdf", ".html", ".htm")) else doi
        if doi in seen:
            continue
        seen.add(doi)
        line_start = part.rfind("\n", 0, m.start()) + 1
        line_end = part.find("\n", m.end())
        entry = part[max(line_start, m.start() - 500):line_end if line_end != -1 else len(part)]
        out.append((doi, entry))
        if len(out) >= MAX_DOIS:
            break
    return out


NUMBERED = re.compile(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[.)])\s+")
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
JOURNAL_LIKE = re.compile(r"\b\d{1,4}\s*\(\d{1,4}\)|\bvol(?:ume)?\.?\s*\d|\bpp?\.\s*\d|\b\d{1,5}\s*[–-]\s*\d{1,5}\b", re.I)
APA_TITLE = re.compile(r"\((?:19|20)\d{2}[a-z]?\)\.?\s*(.+?)[.?!](?:\s|$)")
YEAR_TITLE = re.compile(r"\b(?:19|20)\d{2}[a-z]?\.\s+(.+?)[.?!](?:\s|$)")
INITIALS = re.compile(r"\b[A-Z]\.")


def reference_entries(text):
    """Entries of the reference list: numbered items (joined across wrapped lines), else one per line."""
    span = citations.bibliography_span(text)
    if not span:
        return []
    lines = [l.strip() for l in text[span[0]:span[1]].splitlines()[1:] if l.strip()]
    numbered = sum(1 for l in lines if NUMBERED.match(l))
    entries = []
    for line in lines:
        if numbered >= 2 and not NUMBERED.match(line) and entries:
            entries[-1] += " " + line                      # a wrapped line of the previous item
        else:
            entries.append(line)
    return [NUMBERED.sub("", e) for e in entries if 20 <= len(e) <= 800]


UZ_ENDINGS = ("ning", "lari", "larni", "larning", "dagi", "ligi", "lik", "chi", "ganligi", "moqda")


def _english_reference(entry):
    """A short reference written in English: Latin script, English function words, and no Uzbek letters (oʻ, gʻ),
    Uzbek function words or typical Uzbek endings (corpus.detect_language needs far longer texts)."""
    import corpus
    cyrillic, latin = corpus.script_shares(entry)
    if latin < 0.9 or re.search(r"[og][ʻ‘'`’]", entry.lower()):
        return False
    words = re.findall(r"[a-z]+", entry.lower())
    en = sum(w in corpus.EN_WORDS for w in words)
    uz = sum(w in corpus.UZ_WORDS for w in words) + sum(len(w) > 6 and w.endswith(UZ_ENDINGS) for w in words)
    return en >= 2 and uz == 0


def english_title(entry):
    """The title of an English journal-style reference without a DOI, or None (not such a reference, or the title
    cannot be told apart from the authors and journal)."""
    if DOI.search(entry) or not YEAR.search(entry) or not JOURNAL_LIKE.search(entry):
        return None
    if not _english_reference(entry):
        return None
    m = APA_TITLE.search(entry) or YEAR_TITLE.search(entry)
    title = m.group(1).strip() if m else None
    if not title:
        parts = [p.strip() for p in re.split(r"(?<=[a-z0-9])\.\s+", entry) if p.strip()]
        title = next((p for p in parts[1:] if len(p.split()) >= 4 and len(INITIALS.findall(p)) <= 1), None)
    if not title or not 4 <= len(title.split()) <= 40:
        return None
    return title


def check_title(title, entry):
    """'ok' when OpenAlex has a work whose title matches the reference, 'not_found', or 'unchecked'."""
    words = [w for w in re.findall(r"[^\W\d_]{3,}", unicodedata.normalize("NFKC", title).lower()) if w not in STOP][:10]
    if len(words) < 3:
        return "unchecked"
    key = os.environ.get("OPENALEX_API_KEY")
    url = (os.environ.get("INTEGRITY_OPENALEX_SEARCH", OPENALEX_SEARCH) + "?filter=title.search:"
           + quote(" ".join(words)) + "&per_page=5&select=display_name")
    try:
        found = _get(url, {"Authorization": "Bearer " + key} if key else None)
    except (urllib.error.URLError, OSError, ValueError):
        return "unchecked"
    for work in (found.get("results") or [])[:5]:
        if title_matches(str(work.get("display_name") or ""), entry):
            return "ok"
    return "not_found"


def _words(text):
    folded = unicodedata.normalize("NFKC", text).lower()
    return {w for w in re.findall(r"[^\W\d_]{4,}", folded) if w not in STOP}


def title_matches(title, entry):
    """True/False, or None when the title cannot be compared (too short, or the
    entry is written in the other script, e.g. a Russian citation of a work
    OpenAlex lists under its English title)."""
    words = _words(title)
    if len(words) < 3:
        return None
    script = CYRILLIC if CYRILLIC.search(title) else LATIN
    if len(script.findall(DOI.sub("", entry))) < 10:
        return None
    return len(words & _words(entry)) / len(words) >= 0.5


def _get(url, headers=None, timeout=6):
    request = urllib.request.Request(url, headers={"User-Agent": "PlagIQ-research-pilot/0.5 (+reference check)",
                                                   "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(1024 * 1024))


def check_doi(doi, entry):
    """('ok'|'not_found'|'mismatch'|'unchecked', title or None)."""
    key = os.environ.get("OPENALEX_API_KEY")
    try:
        work = _get(os.environ.get("INTEGRITY_OPENALEX_URL", OPENALEX) + quote(doi, safe="/") + "?select=display_name,doi",
                    {"Authorization": "Bearer " + key} if key else None)
        title = str(work.get("display_name") or "")
        verdict = title_matches(title, entry)
        return ("mismatch" if verdict is False else "ok"), title[:300]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            return "unchecked", None
    except (urllib.error.URLError, OSError, ValueError):
        return "unchecked", None
    try:                                                    # not in OpenAlex: ask the DOI system itself
        handle = _get(os.environ.get("INTEGRITY_HANDLE_URL", HANDLE) + quote(doi, safe="/"))
        return ("not_found" if handle.get("responseCode") == 100 else "ok"), None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "not_found", None
        return "unchecked", None
    except (urllib.error.URLError, OSError, ValueError):
        return "unchecked", None


def check_references(text, seconds=12):
    dois = reference_dois(text)
    titled = []
    if os.environ.get("INTEGRITY_TITLE_CHECK", "1") != "0":
        for entry in reference_entries(text):
            title = english_title(entry)
            if title:
                titled.append((title, entry))
            if len(titled) >= MAX_TITLES:
                break
    report = {"dois": len(dois), "checked": 0, "unchecked": 0, "not_found": [], "mismatch": [],
              "titles": len(titled), "titles_checked": 0, "titles_not_found": []}
    if not dois and not titled:
        return report
    pool = ThreadPoolExecutor(max_workers=4)
    futures = {pool.submit(check_doi, doi, entry): doi for doi, entry in dois}
    by_title = {pool.submit(check_title, title, entry): entry for title, entry in titled}
    done, pending = wait(list(futures) + list(by_title), timeout=seconds)
    for future in by_title:
        if future in done and future.result() != "unchecked":
            report["titles_checked"] += 1
            if future.result() == "not_found":
                entry = by_title[future]
                report["titles_not_found"].append(entry[:240] + ("…" if len(entry) > 240 else ""))
    for future in done:
        if future not in futures:
            continue
        doi = futures[future]
        status, title = future.result()
        if status == "unchecked":
            report["unchecked"] += 1
            continue
        report["checked"] += 1
        if status == "not_found":
            report["not_found"].append(doi)
        elif status == "mismatch":
            report["mismatch"].append({"doi": doi, "title": title})
    report["unchecked"] += sum(1 for f in pending if f in futures)
    pool.shutdown(wait=False, cancel_futures=True)
    report["not_found"].sort()
    report["mismatch"].sort(key=lambda m: m["doi"])
    return report


# ----------------------------------------------------------------- assessment

def assess(text, check_dois=False):
    """(summary for the report, examples for the page only).

    The summary holds counts, DOIs and titles of the works they point to; the
    examples hold manuscript excerpts and must never be cached or put in the PDF."""
    local = local_signals(text)
    signals, examples = [], {}
    for name, (count, shown) in local.items():
        if count >= THRESHOLDS[name]:
            signals.append({"id": name, "count": count})
            examples[name] = shown
    references = None
    if check_dois and os.environ.get("INTEGRITY_DOI_CHECK", "1") != "0":
        references = check_references(text)
        if references["not_found"]:
            signals.append({"id": "doi_not_found", "count": len(references["not_found"])})
        if references["mismatch"]:
            signals.append({"id": "doi_mismatch", "count": len(references["mismatch"])})
        if len(references["titles_not_found"]) >= TITLE_SIGNAL_MIN:
            signals.append({"id": "reference_not_found", "count": len(references["titles_not_found"])})
        else:                                   # one missing reference is too common for honest reasons to list
            references["titles_not_found"] = []
    return {"status": "review" if signals else "none", "signals": signals, "references": references}, examples
