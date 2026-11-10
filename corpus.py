"""Local, rights-gated academic index. Never downloads source URLs or indexes queries."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
import unicodedata
from textfold import IGNORABLE, fold, fold_with_map, normalized_chars

TOKEN = re.compile(r"(?:[^\W_]|[\ue000\ue001])+", re.UNICODE)
# Bump when tokenization changes; connect() re-fingerprints stored sources.
NORMALIZATION_VERSION = 2
MAX_SOURCES = 100
EXCERPT_LIMIT = 500


def lexical(text, language):
    """Normalized tokens with original-character [start, end) offsets.

    Shared by reference, abstract and corpus matching so every mode uses the
    same rules: NFC composition, invisible-character removal, lower-casing,
    Uzbek Latin/Cyrillic folding, and Russian ё→е.
    """
    if language == 'uz':
        normalized, starts, ends = fold_with_map(text)
    else:
        chars = normalized_chars(text)
        if language == 'ru':
            chars = [('е' if c == 'ё' else c, s, e) for c, s, e in chars]
        normalized = ''.join(c for c, _, _ in chars)
        starts = [s for _, s, _ in chars]
        ends = [e for _, _, e in chars]
    return [(m.group(), starts[m.start()], ends[m.end()-1]) for m in TOKEN.finditer(normalized)]


_DROP = re.compile('[' + ''.join(sorted(IGNORABLE)) + ''.join(
    chr(c) for c in range(0x300, 0x10000) if unicodedata.combining(chr(c))) + ']')


def tokens(text, language):
    """Token strings only (no offsets). Same rules as lexical(), faster for ru/en."""
    if language == 'uz':
        return TOKEN.findall(fold(text))
    text = _DROP.sub('', unicodedata.normalize('NFC', text).lower())
    if language == 'ru':
        text = text.replace('ё', 'е')
    return TOKEN.findall(text)


def connect(path=None):
    db = sqlite3.connect(path or os.environ.get('CORPUS_DB', 'corpus.sqlite3'))
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''CREATE TABLE IF NOT EXISTS sources (
      id TEXT PRIMARY KEY, metadata TEXT NOT NULL, text TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS windows (
      fingerprint TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      source_start INTEGER NOT NULL, source_end INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS window_lookup ON windows(fingerprint);''')
    if db.execute('PRAGMA user_version').fetchone()[0] < NORMALIZATION_VERSION:
        reindex(db)
    return db


def _store_windows(db, identifier, text, language):
    db.executemany('INSERT INTO windows VALUES (?,?,?,?)',
                   ((f, identifier, span[0], span[1]) for f, span in
                    {f: (start, end) for f, start, end in windows(text, language)}.items()))


def reindex(db):
    """Rebuild fingerprints from stored authorized texts (no downloads)."""
    with db:
        db.execute('DELETE FROM windows')
        for identifier, raw, text in db.execute('SELECT id, metadata, text FROM sources').fetchall():
            _store_windows(db, identifier, text, json.loads(raw)['language'])
        db.execute(f'PRAGMA user_version={NORMALIZATION_VERSION}')


def fingerprint(words):
    return hashlib.sha256(' '.join(words).encode()).hexdigest()


def windows(text, language):
    words = lexical(text, language)
    return [(fingerprint([w[0] for w in words[i:i+7]]), words[i][1], words[i+6][2])
            for i in range(max(0, len(words)-6))]


def ingest(db, metadata, text):
    required = ('title', 'url', 'language', 'authors', 'year', 'rights_basis', 'permission_reference')
    if any(not metadata.get(key) for key in required):
        raise ValueError('Missing citation or rights metadata')
    if metadata['language'] not in ('uz', 'ru', 'en'):
        raise ValueError('Unsupported language')
    if metadata['rights_basis'] not in ('public_domain', 'open_license', 'written_permission'):
        raise ValueError('Explicit full-text indexing rights required')
    if urlsplit(metadata['url']).scheme != 'https' or not urlsplit(metadata['url']).hostname:
        raise ValueError('HTTPS citation URL required')
    if not isinstance(text, str) or not text.strip() or len(text) > 300000:
        raise ValueError('Source must contain 1–300000 characters')
    identifier = hashlib.sha256(text.encode()).hexdigest()
    metadata = {**metadata, 'sha256': identifier}
    if (not db.execute('SELECT 1 FROM sources WHERE id=?', (identifier,)).fetchone()
            and db.execute('SELECT count(*) FROM sources').fetchone()[0] >= MAX_SOURCES):
        raise ValueError('Pilot index is limited to 100 sources; review resource scaling before expansion')
    with db:
        db.execute('DELETE FROM sources WHERE id=?', (identifier,))
        db.execute('INSERT INTO sources VALUES (?,?,?)', (identifier, json.dumps(metadata), text))
        _store_windows(db, identifier, text, metadata['language'])
    return identifier


def merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def _passages(window_hits):
    """Join consecutive window hits that continue in both texts into one passage."""
    passages = []
    for qs, qe, ss, se in window_hits:
        last = passages[-1] if passages else None
        if last and qs <= last['query_end'] and last['source_start'] <= ss <= last['source_end']:
            last['query_end'] = max(last['query_end'], qe)
            last['source_end'] = max(last['source_end'], se)
        else:
            passages.append({'query_start': qs, 'query_end': qe, 'source_start': ss, 'source_end': se})
    return passages


def search(db, text, language, max_passages=5):
    """Read-only lookup. The manuscript is fingerprinted in memory and never written."""
    hits, candidates = {}, {}
    for f, start, end in windows(text, language):
        if f not in candidates:
            candidates[f] = db.execute('SELECT source_id, source_start, source_end FROM windows WHERE fingerprint=?', (f,)).fetchall()
        for identifier, source_start, source_end in candidates[f]:
            hits.setdefault(identifier, []).append((start, end, source_start, source_end))
    sources, union = [], []
    for identifier, window_hits in hits.items():
        raw, source_text = db.execute('SELECT metadata, text FROM sources WHERE id=?', (identifier,)).fetchone()
        metadata = json.loads(raw)
        if metadata['language'] != language:
            continue
        spans = merge([[qs, qe] for qs, qe, _, _ in window_hits])
        union.extend(spans)
        passages = _passages(window_hits)
        shown = sorted(sorted(passages, key=lambda p: p['query_start'] - p['query_end'])[:max_passages],
                       key=lambda p: p['query_start'])
        for passage in shown:
            excerpt = source_text[passage['source_start']:passage['source_end']]
            passage['source_excerpt'] = excerpt[:EXCERPT_LIMIT] + ('…' if len(excerpt) > EXCERPT_LIMIT else '')
        public = {k: metadata.get(k) for k in ('title', 'authors', 'year', 'url', 'language', 'rights_basis', 'sha256')}
        sources.append({**public, 'source_filename': metadata['title'], 'source_url': metadata['url'],
                        'matched_chars': sum(e-s for s, e in spans), 'passage_count': len(passages),
                        'passages': shown})
    counts = {lang: 0 for lang in ('uz', 'ru', 'en')}
    for (raw,) in db.execute('SELECT metadata FROM sources'):
        counts[json.loads(raw)['language']] += 1
    union = merge(union)
    return {'query_text_length': len(text), 'unique_passages': union,
            'sources': sorted(sources, key=lambda s: -s['matched_chars']),
            'corpus_sources': sum(counts.values()), 'language_counts': counts,
            'coverage': f"Indexed full text only: uz={counts['uz']}, ru={counts['ru']}, en={counts['en']}. Exact seven-token matches; no translation or paraphrase detection."}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Owner-only local import; verify rights before running.')
    parser.add_argument('manifest', type=Path, help='JSON array: citation/rights metadata plus local UTF-8 text_file')
    parser.add_argument('--db', default=os.environ.get('CORPUS_DB', 'corpus.sqlite3'))
    args = parser.parse_args()
    records = json.loads(args.manifest.read_text('utf-8'))
    with closing(connect(args.db)) as db:
        for record in records:
            record = dict(record)
            path = args.manifest.parent / record.pop('text_file')
            ingest(db, record, path.read_text('utf-8'))
    print(f'Imported {len(records)} authorized sources. No remote content downloaded.')
