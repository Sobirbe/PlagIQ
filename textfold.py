"""Uzbek/Russian script folding with original-character offset maps.

Adapted for PlagIQ from the user-supplied noplag integration bundle.
For source matching only; not a substitute for translation detection.
"""
from dataclasses import replace

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

def fold_with_map(text: str):
    lowered = []
    for i,ch in enumerate(text):
        for lower in ch.lower():
            lowered.append((lower,i,i+1))
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
