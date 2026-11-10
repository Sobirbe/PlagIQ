"""Local, rights-gated academic index. Never downloads source URLs or indexes queries."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
from textfold import fold_with_map

TOKEN = re.compile(r"(?:[^\W_]|[\ue000\ue001])+", re.UNICODE)


def lexical(text, language):
    if language == 'uz':
        normalized, starts, ends = fold_with_map(text)
    else:
        chars = [(c, i, i+1) for i, ch in enumerate(text) for c in ch.lower()]
        normalized = ''.join(c for c, _, _ in chars)
        starts = [s for _, s, _ in chars]
        ends = [e for _, _, e in chars]
    return [(m.group(), starts[m.start()], ends[m.end()-1]) for m in TOKEN.finditer(normalized)]


def connect(path=None):
    db = sqlite3.connect(path or os.environ.get('CORPUS_DB', 'corpus.sqlite3'))
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''CREATE TABLE IF NOT EXISTS sources (
      id TEXT PRIMARY KEY, metadata TEXT NOT NULL, text TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS windows (
      fingerprint TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      source_start INTEGER NOT NULL, source_end INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS window_lookup ON windows(fingerprint);''')
    return db


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
            and db.execute('SELECT count(*) FROM sources').fetchone()[0] >= 100):
        raise ValueError('Pilot index is limited to 100 sources; review resource scaling before expansion')
    with db:
        db.execute('DELETE FROM sources WHERE id=?', (identifier,))
        db.execute('INSERT INTO sources VALUES (?,?,?)', (identifier, json.dumps(metadata), text))
        db.executemany('INSERT INTO windows VALUES (?,?,?,?)',
                       ((f, identifier, span[0], span[1]) for f, span in
                        {f:(start,end) for f,start,end in windows(text, metadata['language'])}.items()))
    return identifier


def merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def search(db, text, language):
    # Query only bounded seven-token fingerprints; manuscript never enters SQLite.
    hits, evidence, candidates = {}, {}, {}
    for f, start, end in windows(text, language):
        if f not in candidates:
            candidates[f] = db.execute('SELECT source_id, source_start, source_end FROM windows WHERE fingerprint=?', (f,)).fetchall()
        for identifier, source_start, source_end in candidates[f]:
            spans = hits.setdefault(identifier, [])
            if spans and start <= spans[-1][1]:
                spans[-1][1] = max(end, spans[-1][1])
            else:
                spans.append([start, end])
            if len(evidence.setdefault(identifier, [])) < 5:
                evidence[identifier].append({'query_start':start, 'query_end':end,
                                             'source_start':source_start, 'source_end':source_end})
    sources, union = [], []
    for identifier, spans in hits.items():
        raw, source_text = db.execute('SELECT metadata, text FROM sources WHERE id=?', (identifier,)).fetchone()
        metadata = json.loads(raw)
        for passage in evidence[identifier]:
            passage['source_excerpt'] = source_text[passage['source_start']:passage['source_end']][:500]
        if metadata['language'] != language:
            continue
        spans = merge(spans)
        union.extend(spans)
        sources.append({**metadata, 'source_filename': metadata['title'], 'source_url': metadata['url'],
                        'matched_chars': sum(e-s for s, e in spans), 'passages': evidence[identifier]})
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
    with connect(args.db) as db:
        for record in records:
            record = dict(record)
            path = args.manifest.parent / record.pop('text_file')
            ingest(db, record, path.read_text('utf-8'))
    print(f'Imported {len(records)} authorized sources. No remote content downloaded.')
