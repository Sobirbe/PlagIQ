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

The DOI check sends only the DOIs to OpenAlex (free single-work lookups) and, for
DOIs OpenAlex does not know, to doi.org. It runs only with the user's consent.
Settings: INTEGRITY_DOI_CHECK=0 turns the DOI check off.
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

_A = r"[ʼʻ'’`]"
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
)]
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
    report = {"dois": len(dois), "checked": 0, "unchecked": 0, "not_found": [], "mismatch": []}
    if not dois:
        return report
    pool = ThreadPoolExecutor(max_workers=4)
    futures = {pool.submit(check_doi, doi, entry): doi for doi, entry in dois}
    done, pending = wait(futures, timeout=seconds)
    for future in done:
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
    report["unchecked"] += len(pending)
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
    return {"status": "review" if signals else "none", "signals": signals, "references": references}, examples
