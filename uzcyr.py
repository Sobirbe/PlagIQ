"""Uzbek Latin -> Uzbek Cyrillic for the interface, reports and certificate ("uzc" = Ўзбекча, кирилл).

The Cyrillic interface is generated from the Uzbek Latin texts, so the two never drift apart. Rules (the 1995
alphabet, read in reverse): oʻ -> ў, gʻ -> ғ, sh -> ш, ch -> ч, yo/yu/ya/ye -> ё/ю/я/е, e at the start of a word or
after a vowel -> э, ts -> ц at the start of a word and before iya/io/ia (тс otherwise: muvaffaqiyatsiz, o‘tsa), the tutuq belgisi (ʼ) -> ъ, q -> қ, h -> ҳ, x -> х, j -> ж, y -> й. Russian loanwords that
Uzbek Cyrillic writes with ь or я (сентябрь, файл ...) come from EXCEPTIONS. Left as they are: brand and service names,
abbreviations in capitals (PDF, DOCX, QR, SHA-256), links, e-mail addresses, {placeholders} and HTML tags.

index.html carries the same rules in JavaScript (uzCyr); test_uz_cyrillic_ui.py checks that both give the same text.
"""
import re

APOS = "'ʻʼ‘’`´"
SINGLE = {"a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ", "i": "и", "j": "ж", "k": "к", "l": "л",
          "m": "м", "n": "н", "o": "о", "p": "п", "q": "қ", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "x": "х",
          "y": "й", "z": "з", "c": "с", "w": "в"}
VOWELS = set("aeiouаеиоуўэюяё")
# Loanwords whose Uzbek Cyrillic spelling cannot be derived from the Latin one (ь, я, ц). Lower case, word stems:
# the ending of the Latin word is kept (sentabrda -> сентябрда is wrong, so whole words are listed with endings
# handled by STEM_ENDINGS below).
EXCEPTIONS = {"sentabr": "сентябрь", "oktabr": "октябрь", "noyabr": "ноябрь", "dekabr": "декабрь", "yanvar": "январь",
              "fevral": "февраль", "aprel": "апрель", "iyun": "июнь", "iyul": "июль", "parol": "пароль", "film": "фильм",
              "model": "модель", "kontrol": "контроль", "rol": "роль", "stil": "стиль", "kompyuter": "компьютер",
              "obyekt": "объект", "subyekt": "субъект", "konferensiya": "конференция", "funksiya": "функция",
              "aksiya": "акция", "provinsiya": "провинция", "sotsial": "социал", "kvitansiya": "квитанция", "dotsent": "доцент",
              "kontsert": "концерт", "kontsern": "концерн"}
MONTHS = {"sentabr", "oktabr", "noyabr", "dekabr", "yanvar", "fevral", "aprel", "iyun", "iyul"}
# Common endings after a listed loanword (stem + ending keeps the loanword's spelling).
ENDINGS = ("", "lar", "ni", "ning", "ga", "da", "dan", "dagi", "i", "si", "lari", "larni", "larning", "larga", "larda",
           "laridan", "ingiz", "imiz", "im", "ni", "dir", "ning", "lik", "li")
# Kept as written: {placeholders}, HTML, links, e-mail addresses, names of services and products, settings names
# (VERIFY_SECRET), and foreign abbreviations, also when an Uzbek ending follows (PDFda -> PDFда). Uzbek abbreviations
# (OAK, AQSH) are not in the list and are transliterated (ОАК, АҚШ).
FOREIGN = ("PDF|DOCX|DOC|TXT|CSV|OCR|QR|DOI|URL|API|HTML|JSON|UZS|USD|AI|ID|IP|PhD|DSc|ORCID|ISSN|ISBN|SHA-256|UTF-8|"
           "UTF-16|CC BY|BOM|SMS")
KEEP = re.compile(r"(\{[^}]*\}|<[^>]+>|&[a-z#0-9]+;|https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.[\w.]+|robots\.txt|"
                  r"(?<![A-Za-z])(?:PlagIQ|PLAGIQ|OpenAlex|Telegram|Pangram|Payme|Click|Uzum|CyberLeninka|Zenodo|Wikipedia|Yandex|"
                  r"Brave|Railway|Google|Excel|Word|Turnitin|Antiplagiat|Scopus|Unpaywall|CORE|humanizer)(?![A-Za-z])|"
                  r"(?<![A-Za-z])(?:PlagIQ|PLAGIQ|OpenAlex|Telegram|Pangram|Payme|Click|Uzum|CyberLeninka|Zenodo|Wikipedia|Yandex|"
                  r"Brave|Railway|Google|Excel|Word|Turnitin|Antiplagiat|Scopus|Unpaywall)"
                  r"(?=['‘ʼ’]?(?:dagi|dan|da|ga|ka|ning|ni|lar)(?![A-Za-z]))|"
                  r"(?<![A-Za-z])[Ee]-?mail|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|"
                  rf"(?<![A-Za-z])(?:{FOREIGN})(?![A-Za-z0-9])|(?<![A-Za-z])(?:{FOREIGN})(?=[a-z‘ʼ'’]))")
WORD = re.compile(r"[A-Za-zʻʼ‘’'`´]+")


def _case(src, out):
    """Copy the capitalisation of the Latin word (by its letters, not quote marks) onto the Cyrillic one."""
    letters = [c for c in src if c.isalpha() and c not in APOS]
    if len(letters) > 1 and all(c.isupper() for c in letters):
        return out.upper()
    if letters and letters[0].isupper():
        k = next((i for i, c in enumerate(out) if c.isalpha()), None)
        if k is not None:
            return out[:k] + out[k].upper() + out[k + 1:]
    return out


def _word(word):
    head = re.match(r"[A-Z]{2,}(?=[a-zʻʼ‘’'`]+$)", word)        # an abbreviation with an Uzbek ending: OAKka -> ОАКка
    if head:
        return _letters(head.group()).upper() + _letters(word[head.end():], prev=head.group()[-1].lower())
    lower = word.lower()
    for stem, cyr in EXCEPTIONS.items():
        if lower.startswith(stem) and lower[len(stem):] in ENDINGS:
            ending = lower[len(stem):]
            base = cyr[:-1] if ending and stem in MONTHS and cyr.endswith("ь") else cyr   # сентябрда, февралда
            out = base + _letters(ending, prev=stem[-1])
            return _case(word, out)
    return _case(word, _letters(word))


def _letters(word, prev=""):
    out, i, low = [], 0, word.lower()
    while i < len(low):
        c, nxt = low[i], low[i + 1] if i + 1 < len(low) else ""
        before = next((x[-1] for x in reversed(out) if x), prev)
        if c in "og" and nxt and nxt in APOS:
            out.append("ў" if c == "o" else "ғ")
            i += 2
            continue
        if c in APOS:
            out.append("ъ" if before and nxt.isalpha() else word[i])    # tutuq inside a word; quotes stay as written
            i += 1
            continue
        if c == "s" and nxt == "h":
            out.append("ш"); i += 2; continue
        if c == "c" and nxt == "h":
            out.append("ч"); i += 2; continue
        if c == "t" and nxt == "s" and (i == 0 or (low[i + 2:i + 3] == "i" and low[i + 3:i + 4] in ("y", "o", "a"))):
            out.append("ц"); i += 2; continue        # tsement, natsional, -tsiya; not muvaffaqiyat+siz, o‘t+sa (тс)
        if c == "y" and nxt and nxt in "ouae" and not (nxt == "o" and i + 2 < len(low) and low[i + 2] in APOS):
            out.append({"o": "ё", "u": "ю", "a": "я", "e": "е"}[nxt]); i += 2; continue
        if c == "e":
            out.append("э" if not before or before in VOWELS else "е"); i += 1; continue
        out.append(SINGLE.get(c, c))
        i += 1
    return "".join(out)


def to_cyrillic(text):
    """Uzbek Latin text -> Uzbek Cyrillic, leaving names, codes, links and {placeholders} as they are."""
    if not isinstance(text, str) or not text:
        return text
    parts, pos = [], 0
    for m in KEEP.finditer(text):
        parts.append(WORD.sub(lambda w: _word(w.group()), text[pos:m.start()]))
        parts.append(m.group())
        pos = m.end()
    parts.append(WORD.sub(lambda w: _word(w.group()), text[pos:]))
    return "".join(parts)


def deep(value):
    """The same structure with every string converted (dicts, lists and tuples)."""
    if isinstance(value, str):
        return to_cyrillic(value)
    if isinstance(value, dict):
        return {k: deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(deep(v) for v in value)
    return value


def add_to(table):
    """Give a {"uz": ..., "ru": ..., "en": ...} table its "uzc" entry (generated from "uz")."""
    if "uz" in table and "uzc" not in table:
        table["uzc"] = deep(table["uz"])
    return table
