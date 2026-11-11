"""Exact seven-word matcher and the local, rights-gated full-text index.

One matching algorithm is used for every PlagIQ mode (own reference, OpenAlex
abstracts, private corpus). Never downloads source URLs or indexes queries.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
import unicodedata
from textfold import IGNORABLE, fold, fold_with_map, normalized_chars, unify_scripts

TOKEN = re.compile(r"(?:[^\W_]|[\ue000\ue001])+", re.UNICODE)
# Uzbek: the tutuq belgisi (ta'lim, ma'no; Cyrillic ъ) is part of the word.
UZ_TOKEN = re.compile(r"(?:[^\W_]|[\ue000\ue001])+(?:'(?:[^\W_]|[\ue000\ue001])+)*", re.UNICODE)
# Bump when tokenization changes; connect() re-fingerprints stored sources.
NORMALIZATION_VERSION = 4
PREFER = {'ru': 'cyr', 'en': 'lat'}
WINDOW = 7
EXCERPT_LIMIT = 500
MAX_SOURCE_CHARS = 1_000_000
LANGUAGES = ('uz', 'ru', 'en')
RIGHTS = ('public_domain', 'open_license', 'written_permission')


def max_sources():
    """Index size limit. 1,000 article-sized sources measured at ~171 MB and ~0.1 s per
    search (evaluation/BENCHMARKS.md); re-run the benchmark before raising it."""
    try:
        return max(1, int(os.environ.get('CORPUS_MAX_SOURCES', '1000')))
    except ValueError:
        return 1000


def lexical(text, language):
    """Normalized tokens with original-character [start, end) offsets.

    Shared by reference, abstract and corpus matching so every mode uses the
    same rules: NFC composition, invisible-character removal, lower-casing,
    Uzbek Latin/Cyrillic folding, and Russian ё→е.
    """
    if language == 'uz':
        normalized, starts, ends = fold_with_map(text)
    else:
        chars = normalized_chars(text, prefer=PREFER[language], whole=True)
        if language == 'ru':
            chars = [('е' if c == 'ё' else c, s, e) for c, s, e in chars]
        normalized = ''.join(c for c, _, _ in chars)
        starts = [s for _, s, _ in chars]
        ends = [e for _, _, e in chars]
    pattern = UZ_TOKEN if language == 'uz' else TOKEN
    return [(m.group(), starts[m.start()], ends[m.end()-1]) for m in pattern.finditer(normalized)]


_DROP = re.compile('[' + ''.join(sorted(IGNORABLE)) + ''.join(
    chr(c) for c in range(0x300, 0x10000) if unicodedata.combining(chr(c))) + ']')


def tokens(text, language):
    """Token strings only (no offsets). Same rules as lexical(), faster for ru/en."""
    if language == 'uz':
        return UZ_TOKEN.findall(fold(text))
    text = unify_scripts(_DROP.sub('', unicodedata.normalize('NFC', text).lower()), PREFER[language], True)
    if language == 'ru':
        text = text.replace('ё', 'е')
    return TOKEN.findall(text)


SOURCES_SQL = ('CREATE TABLE IF NOT EXISTS sources (num INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, '
               'metadata TEXT NOT NULL, text TEXT NOT NULL)')


def connect(path=None):
    db = sqlite3.connect(path or os.environ.get('CORPUS_DB', 'corpus.sqlite3'), timeout=60)
    # WAL lets searches read while a background import writes.
    db.execute('PRAGMA journal_mode=WAL')
    db.execute(SOURCES_SQL)
    if db.execute('PRAGMA user_version').fetchone()[0] < NORMALIZATION_VERSION:
        reindex(db)
    return db


def reindex(db):
    """Upgrade the schema and rebuild fingerprints from stored authorized texts (no downloads).

    Windows store a small integer source number and live in one clustered
    table, keeping the index to a few hundred bytes per indexed word window.
    """
    with db:
        db.execute('DROP TABLE IF EXISTS windows')
        if 'num' not in [row[1] for row in db.execute('PRAGMA table_info(sources)')]:  # v0.4 layout
            db.execute('ALTER TABLE sources RENAME TO sources_v04')
            db.execute(SOURCES_SQL)
            db.execute('INSERT INTO sources (id, metadata, text) SELECT id, metadata, text FROM sources_v04')
            db.execute('DROP TABLE sources_v04')
        db.execute('CREATE TABLE windows (fingerprint INTEGER NOT NULL, source INTEGER NOT NULL, '
                   'token_index INTEGER NOT NULL, PRIMARY KEY (fingerprint, source, token_index)) WITHOUT ROWID')
        for num, raw, text in db.execute('SELECT num, metadata, text FROM sources').fetchall():
            _store_windows(db, num, text, json.loads(raw)['language'])
        db.execute(f'PRAGMA user_version={NORMALIZATION_VERSION}')


def _store_windows(db, num, text, language):
    db.executemany('INSERT OR IGNORE INTO windows VALUES (?,?,?)',
                   ((f, num, i) for f, i in windows(lexical(text, language))))


def remove(db, identifier):
    """Delete one source and its fingerprints. Returns True if it existed."""
    row = db.execute('SELECT num FROM sources WHERE id=?', (identifier,)).fetchone()
    if not row:
        return False
    db.execute('DELETE FROM windows WHERE source=?', (row[0],))
    db.execute('DELETE FROM sources WHERE num=?', (row[0],))
    return True


def fingerprint(words):
    """64-bit fingerprint of normalized words. Matches are always re-verified
    against the words themselves, so a collision cannot create a false match."""
    return int.from_bytes(hashlib.sha256(' '.join(words).encode()).digest()[:8], 'big', signed=True)


def windows(words):
    """(fingerprint, first-token index) for every seven-word window."""
    strings = [w[0] for w in words]
    return [(fingerprint(strings[i:i+WINDOW]), i) for i in range(max(0, len(words)-WINDOW+1))]


# ---------------------------------------------------------------- matching

def merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def _chains(q, s, hits):
    """Verify window hits word-by-word and join consecutive ones on one alignment.

    Returns (query_first_token, source_first_token, word_count) runs. Runs whose
    query span lies inside a longer run (e.g. a phrase repeated in the source)
    are dropped, so each manuscript passage is reported once per source.
    """
    qw, sw = [w[0] for w in q], [w[0] for w in s]
    verified = sorted({(j - i, i, j) for i, j in hits
                       if qw[i:i+WINDOW] == sw[j:j+WINDOW] and len(sw[j:j+WINDOW]) == WINDOW})
    runs = []
    for diagonal, i, j in verified:
        last = runs[-1] if runs else None
        if last and last[0] == diagonal and i == last[3] + 1:
            last[3] = i
        else:
            runs.append([diagonal, i, j, i])
    runs = sorted(((i0, j0, last - i0 + WINDOW) for _, i0, j0, last in runs), key=lambda r: (-r[2], r[0]))
    kept = []
    for i0, j0, n in runs:
        if not any(k[0] <= i0 and i0 + n <= k[0] + k[2] for k in kept):
            kept.append((i0, j0, n))
    return sorted(kept)


def _excerpt(text, start, end):
    excerpt = text[start:end]
    return excerpt[:EXCERPT_LIMIT] + ('…' if len(excerpt) > EXCERPT_LIMIT else '')


def match_source(q, s, source_text, hits, max_passages=5):
    """Evidence for one source: verified passages and covered manuscript words."""
    runs = _chains(q, s, hits)
    if not runs:
        return None
    covered = set()
    passages = []
    for i0, j0, n in runs:
        covered.update(range(i0, i0 + n))
        passages.append({'query_start': q[i0][1], 'query_end': q[i0+n-1][2],
                         'source_start': s[j0][1], 'source_end': s[j0+n-1][2], 'words': n})
    intervals = merge([[p['query_start'], p['query_end']] for p in passages])
    shown = sorted(sorted(passages, key=lambda p: -p['words'])[:max_passages], key=lambda p: p['query_start'])
    for passage in shown:
        passage['source_excerpt'] = _excerpt(source_text, passage['source_start'], passage['source_end'])
    return {'covered': covered, 'intervals': intervals, 'matched_words': len(covered),
            'matched_chars': sum(e - s for s, e in intervals), 'passage_count': len(passages), 'passages': shown}


def compare_texts(text, sources, language):
    """In-memory comparison against user-supplied texts (reference, abstracts).

    `sources` is a list of source texts. Returns the manuscript tokens, one
    evidence entry (or None) per source, and the union of covered tokens.
    """
    q = lexical(text, language)
    by_fp = {}
    for f, i in windows(q):
        by_fp.setdefault(f, []).append(i)
    results, covered, union = [], set(), []
    for source_text in sources:
        s = lexical(source_text, language) if source_text else []
        hits = [(i, j) for f, j in windows(s) for i in by_fp.get(f, ())]
        evidence = match_source(q, s, source_text, hits) if hits else None
        if evidence:
            covered |= evidence['covered']
            union.extend(evidence['intervals'])
        results.append(evidence)
    return q, results, covered, merge(union)


def words_in_intervals(q, intervals):
    """Manuscript words lying entirely inside validated character intervals."""
    merged, count, k = merge(intervals), 0, 0
    for _, start, end in q:
        while k < len(merged) and merged[k][1] < end:
            k += 1
        if k < len(merged) and merged[k][0] <= start:
            count += 1
    return count


# ---------------------------------------------------------- private corpus

@lru_cache(maxsize=32)
def _source_lexical(identifier, language, text):
    return lexical(text, language)


def corpus_version(db):
    """Short identifier of the exact indexed snapshot (sources + normalization)."""
    ids = sorted(row[0] for row in db.execute('SELECT id FROM sources'))
    return hashlib.sha256((str(NORMALIZATION_VERSION) + ':' + ','.join(ids)).encode()).hexdigest()[:12]


def search(db, text, language, max_passages=5):
    """Read-only lookup. The manuscript is fingerprinted in memory and never written."""
    q = lexical(text, language)
    hits, seen = {}, {}
    for f, i in windows(q):
        if f not in seen:
            seen[f] = db.execute('SELECT source, token_index FROM windows WHERE fingerprint=?', (f,)).fetchall()
        for num, j in seen[f]:
            hits.setdefault(num, []).append((i, j))
    sources, union = [], []
    for num, pairs in hits.items():
        identifier, raw, source_text = db.execute('SELECT id, metadata, text FROM sources WHERE num=?', (num,)).fetchone()
        metadata = json.loads(raw)
        if metadata['language'] != language:
            continue
        evidence = match_source(q, _source_lexical(identifier, language, source_text), source_text, pairs, max_passages)
        if not evidence:
            continue
        union.extend(evidence['intervals'])
        public = {k: metadata.get(k) for k in ('title', 'authors', 'year', 'url', 'language', 'rights_basis', 'sha256')}
        sources.append({**public, 'source_filename': metadata['title'], 'source_url': metadata['url'],
                        'matched_chars': evidence['matched_chars'], 'matched_words': evidence['matched_words'],
                        'passage_count': evidence['passage_count'], 'passages': evidence['passages']})
    counts = {lang: 0 for lang in LANGUAGES}
    for (raw,) in db.execute('SELECT metadata FROM sources'):
        counts[json.loads(raw)['language']] += 1
    return {'query_text_length': len(text), 'unique_passages': merge(union),
            'sources': sorted(sources, key=lambda s: (-s['matched_words'], s['title'])),
            'corpus_sources': sum(counts.values()), 'language_counts': counts,
            'corpus_version': corpus_version(db),
            'coverage': f"Indexed full text only: uz={counts['uz']}, ru={counts['ru']}, en={counts['en']}. Exact seven-word matches; no translation or paraphrase detection."}


# ------------------------------------------------------------------ import

def script_shares(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0, 0.0
    cyrillic = sum('\u0400' <= c <= '\u04ff' for c in letters)
    latin = sum(c.isascii() or '\u00c0' <= c <= '\u024f' for c in letters)
    return cyrillic / len(letters), latin / len(letters)


UZ_ONLY = frozenset("қғўҳ")


def cyrillic_profile(text):
    """'uz' if Uzbek-only Cyrillic letters (қ ғ ў ҳ) are present, 'ru' if Russian ы is
    present without them, else None. Thresholds: 0.5% of Cyrillic letters."""
    letters = [c for c in text.lower() if '\u0400' <= c <= '\u04ff']
    if len(letters) < 20:
        return None
    uz = sum(c in UZ_ONLY for c in letters) / len(letters)
    ru = letters.count('ы') / len(letters)
    if uz >= 0.005:
        return 'uz'
    if ru >= 0.005:
        return 'ru'
    return None


UZ_WORDS = frozenset("va bilan uchun hamda ham bu ushbu esa yoki ekan bo'lib bo'yicha qilish kerak orqali sifatida".split())
EN_WORDS = frozenset("the of and in to is for with that are on as by this from".split())


def detect_language(text):
    """'uz', 'ru' or 'en' from the text itself, or None when unclear.

    Cyrillic texts: Uzbek-only letters (қ ғ ў ҳ) or Russian ы (cyrillic_profile).
    Latin texts: share of very common Uzbek vs English function words.
    """
    cyrillic, latin = script_shares(text)
    if cyrillic >= 0.6:
        return cyrillic_profile(text)
    if latin >= 0.6:
        words = re.findall(r"[a-z']+", text.lower().replace("ʻ", "'").replace("‘", "'").replace("’", "'"))[:20000]
        if len(words) < 50:
            return None
        uz = sum(w in UZ_WORDS for w in words) / len(words)
        en = sum(w in EN_WORDS for w in words) / len(words)
        if en >= 0.05 and en > 3 * uz:
            return 'en'
        if uz >= 0.02 and uz > 3 * en:
            return 'uz'
    return None


def validate(metadata, text):
    """Check one record before anything is written. Returns cleaned metadata."""
    required = ('title', 'url', 'language', 'authors', 'year', 'rights_basis', 'permission_reference')
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise ValueError('Missing citation or rights metadata: ' + ', '.join(missing))
    if metadata['language'] not in LANGUAGES:
        raise ValueError('Unsupported language')
    if metadata['rights_basis'] not in RIGHTS:
        raise ValueError('Explicit full-text indexing rights required')
    if urlsplit(metadata['url']).scheme != 'https' or not urlsplit(metadata['url']).hostname:
        raise ValueError('HTTPS citation URL required')
    if type(metadata['year']) is not int or not 1000 <= metadata['year'] <= 2200:
        raise ValueError('Year must be a whole number such as 2025')
    if isinstance(text, str) and not text.strip():
        raise ValueError('Source has no readable text (scanned PDF without OCR?)')
    if not isinstance(text, str) or len(text) > MAX_SOURCE_CHARS:
        raise ValueError(f'Source must contain 1–{MAX_SOURCE_CHARS} characters')
    words = len(tokens(text, metadata['language']))
    if words < WINDOW:
        raise ValueError('Source has fewer than 7 readable words (scanned PDF without OCR?)')
    cyrillic, latin = script_shares(text)
    if metadata['language'] == 'ru' and cyrillic < 0.5:
        raise ValueError('Declared Russian, but the text is not mostly Cyrillic')
    if metadata['language'] == 'en' and latin < 0.5:
        raise ValueError('Declared English, but the text is not mostly Latin script')
    # Only judge Cyrillic texts this way: a Latin Uzbek text quoting some Russian is still Uzbek.
    script = cyrillic_profile(text) if cyrillic >= 0.5 else None
    if metadata['language'] == 'ru' and script == 'uz':
        raise ValueError('Declared Russian, but the text uses Uzbek Cyrillic letters (қ ғ ў ҳ); set language to "uz"')
    if metadata['language'] == 'uz' and script == 'ru':
        raise ValueError('Declared Uzbek, but the Cyrillic text looks Russian (ы, no қ ғ ў ҳ); set language to "ru"')
    return {**metadata, 'words': words}


def _insert(db, metadata, text):
    identifier = hashlib.sha256(text.encode()).hexdigest()
    metadata = {**metadata, 'sha256': identifier,
                'imported_at': datetime.now(timezone.utc).date().isoformat()}
    remove(db, identifier)
    num = db.execute('INSERT INTO sources (id, metadata, text) VALUES (?,?,?)',
                     (identifier, json.dumps(metadata, ensure_ascii=False), text)).lastrowid
    _store_windows(db, num, text, metadata['language'])
    return identifier


def _check_capacity(db, new_ids):
    existing = {row[0] for row in db.execute('SELECT id FROM sources')}
    if len(existing | set(new_ids)) > max_sources():
        raise ValueError(f'Index limit is {max_sources()} sources (CORPUS_MAX_SOURCES); '
                         'run evaluation/benchmark_corpus.py on the engine before raising it')


def ingest(db, metadata, text):
    metadata = validate(metadata, text)
    _check_capacity(db, [hashlib.sha256(text.encode()).hexdigest()])
    with db:
        return _insert(db, metadata, text)


def load_manifest(manifest):
    """Read and validate every record; raise listing all problems, write nothing."""
    from extract import extract_text, ExtractError
    records, problems, seen = [], [], {}
    base = manifest.parent.resolve()
    for number, record in enumerate(json.loads(manifest.read_text('utf-8')), 1):
        record = dict(record)
        try:
            path = (base / str(record.pop('text_file', ''))).resolve()
            if base not in path.parents or not path.is_file():
                raise ValueError('text_file must be a file inside the manifest folder')
            text = extract_text(path.name, path.read_bytes(), max_pages=5000)
            metadata = validate(record, text)
            digest = hashlib.sha256(text.encode()).hexdigest()
            if digest in seen:
                raise ValueError(f'identical text to record {seen[digest]}')
            seen[digest] = number
            records.append((metadata, text, digest, path.name))
        except (ValueError, ExtractError, OSError) as exc:
            problems.append(f'record {number}: {exc}')
    if problems:
        raise ValueError('Nothing imported. Fix these records:\n  ' + '\n  '.join(problems))
    return records


def import_manifest(db, manifest):
    records = load_manifest(manifest)
    _check_capacity(db, [r[2] for r in records])
    with db:  # all records or none
        for metadata, text, _, name in records:
            _insert(db, {**metadata, 'source_file': name}, text)
    return records


def inventory(db):
    rows = [json.loads(raw) for (raw,) in db.execute('SELECT metadata FROM sources')]
    return sorted(rows, key=lambda m: (m['language'], m.get('title', '')))


def _cli(args, parser):
    if args.manifest and args.check:
        records = load_manifest(args.manifest)
        for metadata, _, digest, name in records:
            print(f"OK  {metadata['language']}  {metadata['words']:>7} words  {digest[:12]}  {name}  {metadata['title'][:70]}")
        print(f'{len(records)} record(s) valid. Nothing was written.')
    elif args.manifest:
        with closing(connect(args.db)) as db:
            records = import_manifest(db, args.manifest)
            print(f'Imported {len(records)} authorized sources. No remote content downloaded. '
                  f'Corpus version {corpus_version(db)}.')
    if args.remove:
        with closing(connect(args.db)) as db, db:
            removed = remove(db, args.remove)
        print('Removed.' if removed else 'No source with that identifier.')
    if args.list:
        with closing(connect(args.db)) as db:
            rows = inventory(db)
            for m in rows:
                print(f"{m['language']}  {m.get('year', '')}  {m.get('rights_basis', ''):<18} {m.get('words', '?'):>7} words  "
                      f"{m['sha256'][:12]}  {m.get('title', '')[:70]}")
            counts = {lang: sum(m['language'] == lang for m in rows) for lang in LANGUAGES}
            print(f"Total {len(rows)} (uz {counts['uz']}, ru {counts['ru']}, en {counts['en']}); corpus version {corpus_version(db)}")
    if not (args.manifest or args.list or args.remove):
        parser.print_help()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Owner-only local corpus tool; verify rights before importing.')
    parser.add_argument('manifest', type=Path, nargs='?', help='JSON array: citation/rights metadata plus local .txt/.docx/.pdf text_file')
    parser.add_argument('--db', default=os.environ.get('CORPUS_DB', 'corpus.sqlite3'))
    parser.add_argument('--check', action='store_true', help='validate the manifest only; write nothing')
    parser.add_argument('--list', action='store_true', help='list indexed sources and coverage by language')
    parser.add_argument('--remove', metavar='SHA256', help='remove one source by its SHA-256 identifier')
    args = parser.parse_args()
    try:
        _cli(args, parser)
    except (ValueError, OSError) as exc:
        raise SystemExit(f'ERROR: {exc}')
