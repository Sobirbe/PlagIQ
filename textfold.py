"""Uzbek/Russian script folding with original-character offset maps.

Adapted for PlagIQ from the user-supplied noplag integration bundle.
For source matching only; not a substitute for translation detection.
"""
from dataclasses import replace
import re
import unicodedata

APOSTROPHES = frozenset("'ʻʼ‘’\u0060´ʹ′")
O_CANON = "\ue000"
G_CANON = "\ue001"
BREVES = frozenset("\u0306\u0311")
VOWELS = frozenset("аеёиоуэюяўы")
CYR = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","ё":"yo","ж":"j",
    "з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n",
    "о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
    "х":"x","ч":"ch","ш":"sh","щ":"sh","ъ":"'","ы":"i","ь":"",
    "э":"e","ю":"yu","я":"ya","қ":"q","ҳ":"h","ў":O_CANON,
    "ғ":G_CANON,"ŏ":O_CANON,"ğ":G_CANON,
}

# Invisible format characters that commonly appear inside words in PDF/DOCX
# extraction (soft hyphen, zero-width space/joiners, word joiner, BOM).
IGNORABLE = frozenset("\u00ad\u200b\u200c\u200d\u2060\ufeff")
CYR["\u020f"] = O_CANON  # o with inverted breve (NFC of o + U+0311)


# Latin and Cyrillic letters that look identical once lower-cased. Mixed-script
# words (common in OCR/metadata, and a known way to evade text matching) are
# converted to the word's majority script, one letter for one letter.
LAT2CYR = dict(zip("abcehkmoptxy", "авсенкмортху"))
CYR2LAT = {**{v: k for k, v in LAT2CYR.items()}, "і": "i", "ј": "j", "ѕ": "s"}
_WORD_RUN = re.compile(r"[^\W\d_]+")


def _script(c):
    if "\u0400" <= c <= "\u04ff":
        return "cyr"
    if c.isascii() or "\u00c0" <= c <= "\u024f":
        return "lat"
    return None


def _unify_word(word, prefer=None, whole=False):
    """Pick the script from letters that exist in only one alphabet (қ, д, f, g...);
    otherwise the majority; on a tie the document's preferred script. With
    whole=True a word made only of lookalikes in the other script is converted
    too (Russian/English documents; never Uzbek, whose Latin words such as
    "ko'p" consist of lookalike letters). Words with distinctive letters of both
    scripts are left unchanged."""
    scripts = [_script(c) for c in word]
    distinct = {s for c, s in zip(word, scripts) if s and c not in LAT2CYR and c not in CYR2LAT}
    if "cyr" not in scripts or "lat" not in scripts:
        other = "lat" if prefer == "cyr" else "cyr"
        if not (whole and prefer and not distinct and other in scripts):
            return word
        target = prefer
    elif len(distinct) == 1:
        target = distinct.pop()
    elif distinct:
        return word
    elif scripts.count("cyr") != scripts.count("lat"):
        target = "cyr" if scripts.count("cyr") > scripts.count("lat") else "lat"
    elif prefer:
        target = prefer
    else:
        return word
    table = LAT2CYR if target == "cyr" else CYR2LAT
    return "".join(table.get(c, c) for c in word)


def unify_scripts(text: str, prefer=None, whole=False) -> str:
    """Same length as the input; only lookalike letters change."""
    return _WORD_RUN.sub(lambda m: _unify_word(m.group(), prefer, whole), text)


def normalized_chars(text: str, keep_marks=frozenset(), prefer=None, whole=False):
    """Lower-cased NFC characters, each mapped to its original [start, end).

    Each base character and its following combining marks are composed with
    NFC, so decomposed input (common in PDF text) tokenizes like composed
    input. Marks that do not compose are dropped unless listed in keep_marks;
    invisible format characters are dropped. Offsets always refer to the
    caller's original string.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and unicodedata.combining(text[j]):
            j += 1
        for ch in unicodedata.normalize("NFC", text[i:j]):
            for lower in ch.lower():
                if lower in IGNORABLE or (unicodedata.combining(lower) and lower not in keep_marks):
                    continue
                out.append((lower, i, j))
        i = j
    unified = unify_scripts("".join(c for c, _, _ in out), prefer, whole)
    return [(u, start, end) for u, (_, start, end) in zip(unified, out)]


def fold_with_map(text: str):
    lowered = normalized_chars(text, keep_marks=BREVES)
    transliterated = []
    for i,(c,start,end) in enumerate(lowered):
        prev = lowered[i-1][0] if i else ""
        if c in APOSTROPHES:
            output = "'"
        elif c == "е":
            initial = not prev or not (prev.isalpha() or prev in APOSTROPHES)
            output = "ye" if initial or prev in VOWELS or prev in "ъь" else "e"
        elif c == "ц":
            output = "ts" if prev in VOWELS else "s"
        else:
            output = CYR.get(c,c)
        transliterated.extend((x,start,end) for x in output)
    collapsed = []
    i = 0
    while i < len(transliterated):
        c,start,end = transliterated[i]
        if c in "og" and i+1 < len(transliterated):
            next_char,_,next_end = transliterated[i+1]
            if next_char == "'" or next_char in BREVES:
                collapsed.append((O_CANON if c=="o" else G_CANON,start,next_end))
                i += 2
                continue
        if c not in BREVES:
            collapsed.append((c,start,end))
        i += 1
    return ("".join(c for c,_,_ in collapsed),
            [start for _,start,_ in collapsed],
            [end for _,_,end in collapsed])

def fold(text: str) -> str:
    return fold_with_map(text)[0]

def to_original_span(start, end, starts, ends):
    if not starts:
        return (0,0)
    start = max(0,min(int(start),len(starts)-1))
    end = max(start+1,min(int(end),len(ends)))
    return starts[start],ends[end-1]

def align_folded(align_fn,query_chunk_id,query_text,candidates,**kwargs):
    fq,qstarts,qends = fold_with_map(query_text)
    folded = []
    offsets = {}
    for cid,text in candidates:
        fc,cs,ce = fold_with_map(text)
        folded.append((cid,fc))
        offsets[cid] = (cs,ce)
    result = []
    for passage in align_fn(query_chunk_id,fq,folded,**kwargs):
        cs,ce = offsets[passage.candidate_chunk_id]
        q0,q1 = to_original_span(passage.query_start,passage.query_end,qstarts,qends)
        c0,c1 = to_original_span(passage.candidate_start,passage.candidate_end,cs,ce)
        result.append(replace(passage,query_start=q0,query_end=q1,
                              candidate_start=c0,candidate_end=c1))
    return result
