"""Exact seven-word matcher and the local, rights-gated full-text index.

One matching algorithm is used for every PlagIQ mode (own reference, OpenAlex
abstracts, private corpus). Never downloads source URLs or indexes queries.
"""
import argparse
import difflib
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
RIGHTS = ('public_domain', 'open_license', 'written_permission', 'author_deposit', 'institution_deposit')
# Deposited (unpublished) works: never shown to other users, so no URL and no source excerpt in reports.
DEPOSITS = ('author_deposit', 'institution_deposit')


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

SEEDS_SQL = ('CREATE TABLE IF NOT EXISTS seeds (gram INTEGER NOT NULL, source INTEGER NOT NULL, '
             'PRIMARY KEY (gram, source)) WITHOUT ROWID')


def connect(path=None):
    db = sqlite3.connect(path or os.environ.get('CORPUS_DB', 'corpus.sqlite3'), timeout=60)
    # WAL lets searches read while a background import writes.
    db.execute('PRAGMA journal_mode=WAL')
    db.execute(SOURCES_SQL)
    db.execute(SEEDS_SQL)
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
        db.execute('DELETE FROM seeds')
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
    words = lexical(text, language)
    db.executemany('INSERT OR IGNORE INTO windows VALUES (?,?,?)', ((f, num, i) for f, i in windows(words)))
    _store_seeds(db, num, words)


def _store_seeds(db, num, words):
    """1 in SEED_SAMPLE three-word phrases (chosen by hash, so the manuscript samples the same ones): used only to
    find library texts that may contain an edited copy; every candidate is then verified against its full text."""
    db.executemany('INSERT OR IGNORE INTO seeds VALUES (?,?)',
                   ((f, num) for f in {f for f, _ in grams(words) if f % SEED_SAMPLE == 0}))


def seeds_upgrade(db, batch=50):
    """Add edited-copy seeds for texts indexed before seeds existed, `batch` texts per call, in source order.
    Returns how many texts still need them. Texts imported meanwhile get seeds at import."""
    db.execute('CREATE TABLE IF NOT EXISTS corpus_meta (key TEXT PRIMARY KEY, value TEXT)')
    if db.execute("SELECT 1 FROM corpus_meta WHERE key='seeds_indexed'").fetchone():
        return 0
    row = db.execute("SELECT value FROM corpus_meta WHERE key='seeds_upto'").fetchone()
    upto = int(row[0]) if row else 0
    chosen = db.execute('SELECT num, metadata, text FROM sources WHERE num > ? ORDER BY num LIMIT ?', (upto, batch)).fetchall()
    with db:
        for num, raw, text in chosen:
            _store_seeds(db, num, lexical(text, json.loads(raw)['language']))
        if chosen:
            db.execute("INSERT OR REPLACE INTO corpus_meta VALUES ('seeds_upto', ?)", (str(chosen[-1][0]),))
    remaining = db.execute('SELECT COUNT(*) FROM sources WHERE num > ?', (chosen[-1][0] if chosen else upto,)).fetchone()[0]
    if not remaining:
        with db:
            db.execute("DELETE FROM corpus_meta WHERE key='seeds_upto'")
            db.execute("INSERT OR REPLACE INTO corpus_meta VALUES ('seeds_indexed', ?)",
                       (datetime.now(timezone.utc).isoformat(timespec='seconds'),))
    return remaining


LIGATURE_CHARS = "\ufb00\ufb01\ufb02\ufb03\ufb04\ufb05\ufb06"


def ligature_upgrade(db, batch=50):
    """Re-fingerprint up to `batch` stored texts that contain typographic ligatures (indexed before
    ligatures were expanded). Returns how many still need it afterwards. Only those texts are touched,
    so the engine never has to rebuild the whole index at start-up."""
    db.execute('CREATE TABLE IF NOT EXISTS corpus_meta (key TEXT PRIMARY KEY, value TEXT)')
    if db.execute("SELECT 1 FROM corpus_meta WHERE key='ligatures_expanded'").fetchone():
        return 0
    condition = ' OR '.join(f"instr(text, '{c}') > 0" for c in LIGATURE_CHARS)
    done = {int(v) for (v,) in db.execute("SELECT value FROM corpus_meta WHERE key LIKE 'ligature_done:%'")}
    pending = [(num, raw, text) for num, raw, text in db.execute(f'SELECT num, metadata, text FROM sources WHERE {condition}')
               if num not in done]
    chosen = pending[:batch]
    if chosen:
        nums = [num for num, _, _ in chosen]
        with db:
            db.execute(f"DELETE FROM windows WHERE source IN ({','.join('?' * len(nums))})", nums)
            for num, raw, text in chosen:
                _store_windows(db, num, text, json.loads(raw)['language'])
                db.execute("INSERT OR REPLACE INTO corpus_meta VALUES (?, ?)", (f'ligature_done:{num}', str(num)))
        try:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.Error:
            pass
    remaining = len(pending) - len(chosen)
    if not remaining:
        with db:
            db.execute("DELETE FROM corpus_meta WHERE key LIKE 'ligature_done:%'")
            db.execute("INSERT OR REPLACE INTO corpus_meta VALUES ('ligatures_expanded', ?)",
                       (datetime.now(timezone.utc).isoformat(timespec='seconds'),))
    return remaining


def remove(db, identifier):
    """Delete one source and its fingerprints. Returns True if it existed."""
    row = db.execute('SELECT num FROM sources WHERE id=?', (identifier,)).fetchone()
    if not row:
        return False
    db.execute('DELETE FROM windows WHERE source=?', (row[0],))
    db.execute('DELETE FROM seeds WHERE source=?', (row[0],))
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
        if not _has_words(qw[i0:i0 + n]):
            continue                                    # chart ticks, year rows, table digits
        if not any(k[0] <= i0 and i0 + n <= k[0] + k[2] for k in kept):
            kept.append((i0, j0, n))
    return sorted(kept)


MIN_LETTER_WORDS = 4


def _has_words(tokens):
    """A match must be text: at least 4 tokens with a letter, and letters in at least
    half of its tokens. Runs of numbers (axis labels "8 -6 -4 -2 0 2 4 6 8", year rows
    "2003 2004 ... 2010", table cells) coincide across unrelated papers."""
    lettered = sum(1 for t in tokens if any(ch.isalpha() for ch in t))
    return lettered >= MIN_LETTER_WORDS and 2 * lettered >= len(tokens)


def _excerpt(text, start, end):
    excerpt = text[start:end]
    return excerpt[:EXCERPT_LIMIT] + ('…' if len(excerpt) > EXCERPT_LIMIT else '')


def match_source(q, s, source_text, hits, max_passages=5, blocked=frozenset(), edited=True):
    """Evidence for one source: verified passages and covered manuscript words.
    Exact passages (seven or more identical words) and, unless edited=False, edited passages (copied text with
    words replaced, inserted or deleted, verified by alignment; see edited_runs). Each passage has a 'type'."""
    runs = _chains(q, s, hits) if hits else []
    passages = [{'type': 'exact', 'query_start': q[i0][1], 'query_end': q[i0+n-1][2], 'source_start': s[j0][1],
                 'source_end': s[j0+n-1][2], 'words': n, '_q': (i0, i0 + n)} for i0, j0, n in runs]
    exact_covered = {k for p in passages for k in range(*p['_q'])}
    if edited:
        for a, b, j0, j1, similarity in edited_runs(q, s, blocked):
            if sum(1 for k in range(a, b) if k in exact_covered) >= 0.9 * (b - a):
                continue                                # already an exact passage
            passages.append({'type': 'edited', 'query_start': q[a][1], 'query_end': q[b-1][2], 'source_start': s[j0][1],
                             'source_end': s[j1-1][2], 'words': b - a, 'similarity': similarity, '_q': (a, b)})
    if not passages:
        return None
    covered = {k for p in passages for k in range(*p.pop('_q'))}
    edited_words = len(covered - exact_covered)
    intervals = merge([[p['query_start'], p['query_end']] for p in passages])
    exact_intervals = merge([[p['query_start'], p['query_end']] for p in passages if p['type'] == 'exact'])
    shown = sorted(sorted(passages, key=lambda p: -p['words'])[:max_passages], key=lambda p: p['query_start'])
    for passage in shown:
        passage['source_excerpt'] = _excerpt(source_text, passage['source_start'], passage['source_end'])
    return {'covered': covered, 'intervals': intervals, 'exact_intervals': exact_intervals,
            'matched_words': len(covered), 'edited_words': edited_words,
            'matched_chars': sum(e - s for s, e in intervals), 'passage_count': len(passages), 'passages': shown}


# ------------------------------------------------- edited copying (near-duplicate passages)
# Copied text with words replaced, inserted or deleted keeps short runs of identical words. Three-word phrases
# shared by the manuscript and one source are anchors; anchors that follow one another along roughly the same
# alignment are joined, and the region is verified by aligning the two passages word by word. A region counts
# only when it is long, mostly identical and not journal template text. Measured on real texts in
# evaluation/EDITED.md (choice of these values: evaluation/EDITED.md, "settings compared").
GRAM = 3
EDITED_MIN_ANCHORS = 4        # anchors in one region
EDITED_GAP = 8                # most words between consecutive anchors
EDITED_SHIFT = 3              # most inserted/deleted words between consecutive anchors
EDITED_MIN_WORDS = 20         # region length in manuscript words
EDITED_MIN_SAME = 14          # identical words in the aligned region ...
EDITED_MIN_SHARE = 0.6        # ... and at least this share of the region
EDITED_MAX_BLOCKED = 0.2      # most of a region may not be boilerplate (journal headers, licence lines)
SEED_SAMPLE = 4               # library index keeps 1 in 4 three-word phrases (by hash), for finding candidates
SEED_MAX_SOURCES = 20         # a sampled phrase in more library texts than this is too common to find candidates


def grams(words):
    """(fingerprint, first-token index) of every three-word phrase."""
    strings = [w[0] for w in words]
    return [(fingerprint(strings[i:i+GRAM]), i) for i in range(max(0, len(words)-GRAM+1))]


def edited_runs(q, s, blocked=frozenset()):
    """Verified edited-copy regions of manuscript q in source s: (q_start, q_end, s_start, s_end, similarity),
    token indices end-exclusive. `blocked`: manuscript tokens inside boilerplate (never counted)."""
    if len(q) < EDITED_MIN_WORDS or len(s) < GRAM:
        return []
    qw, sw = [w[0] for w in q], [w[0] for w in s]
    positions = {}
    for j in range(len(sw) - GRAM + 1):
        positions.setdefault(tuple(sw[j:j+GRAM]), []).append(j)
    hits = sorted((i, j) for i in range(len(qw) - GRAM + 1) for j in positions.get(tuple(qw[i:i+GRAM]), ())[:20])
    if len(hits) < EDITED_MIN_ANCHORS:
        return []
    chains = []
    for i, j in hits:
        for chain in reversed(chains[-12:]):
            li, lj = chain[-1]
            if 0 < i - li <= EDITED_GAP and abs((j - i) - (lj - li)) <= EDITED_SHIFT:
                chain.append((i, j))
                break
        else:
            chains.append([(i, j)])
    found = []
    for chain in chains:
        if len(chain) < EDITED_MIN_ANCHORS:
            continue
        i0, i1, j0, j1 = chain[0][0], chain[-1][0] + GRAM, chain[0][1], chain[-1][1] + GRAM
        if i1 - i0 < EDITED_MIN_WORDS:
            continue
        blocks = [b for b in difflib.SequenceMatcher(None, qw[i0:i1], sw[j0:j1], autojunk=False).get_matching_blocks() if b.size]
        same = sum(b.size for b in blocks)
        if not blocks or same < EDITED_MIN_SAME or same < EDITED_MIN_SHARE * (i1 - i0):
            continue
        a, b = i0 + blocks[0].a, i0 + blocks[-1].a + blocks[-1].size
        sa, sb = j0 + blocks[0].b, j0 + blocks[-1].b + blocks[-1].size
        if b - a < EDITED_MIN_WORDS or sum(1 for k in range(a, b) if k in blocked) > EDITED_MAX_BLOCKED * (b - a):
            continue
        if not _has_words(qw[a:b]):
            continue
        found.append((a, b, sa, sb, round(same / (b - a), 2)))
    return found


# Copies of one work (journal version, preprint, repository copy, web mirror; an abstract and its full text)
# must count once when deciding what is boilerplate: otherwise a passage copied from a widely mirrored paper
# is in "3 different texts" and is dropped, and verbatim copying scores 0%.
SAME_WORK_MIN = 20          # shared seven-word windows (about 26 words) ...
SAME_WORK_SHARE = 0.5       # ... that are at least half of the shorter text
SAME_WORK_CHECK_MAX = 12    # a sequence in more texts than this is boilerplate without further checks


def same_work(a, b):
    """True when two fingerprint sets are copies of one work (one largely contains the other)."""
    shared = len(a & b) if len(a) <= len(b) else len(b & a)
    return shared >= SAME_WORK_MIN and shared >= SAME_WORK_SHARE * min(len(a), len(b))


def distinct_works(members, sets):
    """Number of different works among `members` (keys of `sets`), grouping copies of the same work."""
    groups = []
    for m in members:
        for group in groups:
            if any(same_work(sets[m], sets[g]) for g in group):
                group.append(m)
                break
        else:
            groups.append([m])
    return len(groups)


@lru_cache(maxsize=256)
def _source_fingerprints(identifier, language, text):
    return frozenset(f for f, _ in windows(_source_lexical(identifier, language, text)))


def compare_texts(text, sources, language):
    """In-memory comparison against user-supplied texts (reference, abstracts).

    `sources` is a list of source texts. Returns the manuscript tokens, one
    evidence entry (or None) per source, and the union of covered tokens.
    """
    q = lexical(text, language)
    by_fp = {}
    for f, i in windows(q):
        by_fp.setdefault(f, []).append(i)
    lexed = [lexical(source_text, language) if source_text else [] for source_text in sources]
    source_windows = [windows(s) for s in lexed]
    limit = boilerplate_sources()
    common = set()
    if limit and len(sources) >= limit:
        holders = {}
        for k, ws in enumerate(source_windows):
            for f in {f for f, _ in ws if f in by_fp}:
                holders.setdefault(f, []).append(k)
        sets = {}
        for f, members in holders.items():
            if len(members) < limit:
                continue
            if len(members) > SAME_WORK_CHECK_MAX:
                common.add(f)
                continue
            for k in members:
                if k not in sets:
                    sets[k] = frozenset(g for g, _ in source_windows[k])
            if distinct_works(members, sets) >= limit:
                common.add(f)
    blocked = {k for f in common for i in by_fp[f] for k in range(i, i + WINDOW)}
    blocked |= _shared_edits(q, [(k, s) for k, s in enumerate(lexed) if s], blocked, limit,
                             lambda k: frozenset(g for g, _ in source_windows[k]))
    results, covered, union = [], set(), []
    for source_text, s, ws in zip(sources, lexed, source_windows):
        hits = [(i, j) for f, j in ws if f not in common for i in by_fp.get(f, ())]
        evidence = match_source(q, s, source_text, hits, blocked=blocked) if s else None
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


def search(db, text, language, max_passages=5, exclude=(), ignore=()):
    """Read-only lookup. The manuscript is fingerprinted in memory and never written.
    `exclude`: SHA-256 ids of deposits to skip (the same document deposited earlier). Published
    sources are never skipped: an exact copy of a published work must still be found.
    `ignore`: source numbers hidden completely (used only by evaluation/real_eval.py)."""
    q = lexical(text, language)
    hits, seen, common = {}, {}, set()
    limit = boilerplate_sources()
    sets = {}

    def work_sets(nums):
        for num in nums:
            if num not in sets:
                identifier, raw, source_text = db.execute('SELECT id, metadata, text FROM sources WHERE num=?', (num,)).fetchone()
                sets[num] = _source_fingerprints(identifier, json.loads(raw)['language'], source_text)
        return sets

    for f, i in windows(q):
        if f not in seen:
            seen[f] = db.execute('SELECT source, token_index FROM windows WHERE fingerprint=?', (f,)).fetchall()
            holders = sorted({num for num, _ in seen[f]})
            if limit and len(holders) >= limit and (len(holders) > SAME_WORK_CHECK_MAX or
                                                    distinct_works(holders, work_sets(holders)) >= limit):
                common.add(f)                           # in `limit` different works, not copies of one
        if f in common:
            continue                                    # boilerplate shared by many texts
        for num, j in seen[f]:
            if ignore and num in ignore:
                continue
            hits.setdefault(num, []).append((i, j))
    blocked = {k for f, i in windows(q) if f in common for k in range(i, i + WINDOW)}
    candidates = dict.fromkeys(hits)                    # texts with exact matches, then edited-copy candidates
    for num in _seed_candidates(db, q, ignore):
        candidates.setdefault(num, None)
    texts = {}
    for num in candidates:
        identifier, raw, source_text = db.execute('SELECT id, metadata, text FROM sources WHERE num=?', (num,)).fetchone()
        if json.loads(raw)['language'] == language:
            texts[num] = _source_lexical(identifier, language, source_text)
    blocked |= _shared_edits(q, list(texts.items()), blocked, limit, lambda num: work_sets([num])[num])
    sources, union, exact_union = [], [], []
    for num in candidates:
        pairs = hits.get(num, [])
        identifier, raw, source_text = db.execute('SELECT id, metadata, text FROM sources WHERE num=?', (num,)).fetchone()
        metadata = json.loads(raw)
        if metadata['language'] != language:
            continue
        if identifier in exclude and metadata.get('rights_basis') in DEPOSITS:
            continue                                    # the same deposited document, checked again
        evidence = match_source(q, _source_lexical(identifier, language, source_text), source_text, pairs, max_passages,
                                blocked=blocked)
        if not evidence:
            continue
        union.extend(evidence['intervals'])
        exact_union.extend(evidence['exact_intervals'])
        public = {k: metadata.get(k) for k in ('title', 'authors', 'year', 'url', 'language', 'rights_basis', 'sha256')}
        if metadata.get('rights_basis') in DEPOSITS:
            # Unpublished deposit: other users see title, author and year, never its text.
            for passage in evidence['passages']:
                passage['source_excerpt'] = ''
            public.update(url='', owner=metadata.get('owner', ''), collection=metadata.get('collection', ''))
        sources.append({**public, 'source_filename': metadata['title'], 'source_url': metadata['url'],
                        'matched_chars': evidence['matched_chars'], 'matched_words': evidence['matched_words'],
                        'edited_words': evidence['edited_words'], 'intervals': evidence['intervals'][:2000],
                        'passage_count': evidence['passage_count'], 'passages': evidence['passages']})
    counts = {lang: 0 for lang in LANGUAGES}
    for (raw,) in db.execute('SELECT metadata FROM sources'):
        counts[json.loads(raw)['language']] += 1
    return {'query_text_length': len(text), 'unique_passages': merge(union), 'exact_passages': merge(exact_union),
            'boilerplate_windows': len(common),
            'sources': sorted(sources, key=lambda s: (-s['matched_words'], s['title'])),
            'corpus_sources': sum(counts.values()), 'language_counts': counts,
            'corpus_version': corpus_version(db),
            'coverage': f"Indexed full text only: uz={counts['uz']}, ru={counts['ru']}, en={counts['en']}. Exact seven-word matches and edited copies (words replaced, inserted or deleted); no translation or paraphrase detection."}


EDITED_CANDIDATES = 30


def _shared_edits(q, sources, blocked, limit, fingerprints):
    """Manuscript tokens inside edited-copy regions found in `limit` or more different works: edited boilerplate
    (a reworded licence or template line printed in many papers) is not counted, as for exact matches.
    `sources`: (key, tokens); `fingerprints(key)`: the work's fingerprint set, to group copies of one work."""
    if not limit or len(sources) < limit:
        return set()
    holders = {}
    for key, s in sources:
        for a, b, *_ in edited_runs(q, s, blocked):
            for k in range(a, b):
                holders.setdefault(k, set()).add(key)
    shared, decided = set(), {}
    for k, keys in holders.items():
        if len(keys) < limit:
            continue
        group = tuple(sorted(keys))
        if group not in decided:
            decided[group] = len(group) > SAME_WORK_CHECK_MAX or distinct_works(
                list(group), {key: fingerprints(key) for key in group}) >= limit
        if decided[group]:
            shared.add(k)
    return shared


def _seed_candidates(db, q, ignore=()):
    """Library texts sharing at least 2 sampled three-word phrases with the manuscript (most shared first)."""
    counts = {}
    for f in {f for f, _ in grams(q) if f % SEED_SAMPLE == 0}:
        holders = db.execute('SELECT source FROM seeds WHERE gram=? LIMIT ?', (f, SEED_MAX_SOURCES + 1)).fetchall()
        if len(holders) > SEED_MAX_SOURCES:
            continue                                    # a common phrase: says nothing about copying
        for (num,) in holders:
            if num not in ignore:
                counts[num] = counts.get(num, 0) + 1
    ranked = sorted((n for n, c in counts.items() if c >= 2), key=lambda n: -counts[n])
    return ranked[:EDITED_CANDIDATES]


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
    deposit = metadata.get('rights_basis') in DEPOSITS
    missing = [key for key in required if not metadata.get(key) and not (deposit and key == 'url')]
    if missing:
        raise ValueError('Missing citation or rights metadata: ' + ', '.join(missing))
    if metadata['language'] not in LANGUAGES:
        raise ValueError('Unsupported language')
    if metadata['rights_basis'] not in RIGHTS:
        raise ValueError('Explicit full-text indexing rights required')
    if deposit and not metadata.get('owner'):
        raise ValueError('Deposits need an owner (depositing account or institution)')
    if deposit:
        metadata = {**metadata, 'url': ''}
    elif urlsplit(metadata['url']).scheme != 'https' or not urlsplit(metadata['url']).hostname:
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


def boilerplate_sources():
    """A seven-word sequence found in this many different works is treated as boilerplate (journal
    headers, licence lines, website banners, Wikipedia navigation boxes) and not counted as a match.
    Measured on real texts in evaluation/REAL_RESULTS.md. 0 turns the filter off."""
    try:
        return max(0, int(os.environ.get('BOILERPLATE_SOURCES', '3')))
    except ValueError:
        return 3


def max_deposits():
    """Separate room for archive deposits, so bulk imports can never block a user's or a
    university's deposit (and deposits never block imports)."""
    try:
        return max(1, int(os.environ.get('ARCHIVE_MAX_SOURCES', '5000')))
    except ValueError:
        return 5000


def _check_capacity(db, new_ids, deposit=False):
    marks = ','.join('?' * len(DEPOSITS))
    if deposit:
        existing = {row[0] for row in db.execute(
            f"SELECT id FROM sources WHERE json_extract(metadata, '$.rights_basis') IN ({marks})", DEPOSITS)}
        limit, name = max_deposits(), 'ARCHIVE_MAX_SOURCES'
    else:
        existing = {row[0] for row in db.execute(
            f"SELECT id FROM sources WHERE json_extract(metadata, '$.rights_basis') NOT IN ({marks})", DEPOSITS)}
        limit, name = max_sources(), 'CORPUS_MAX_SOURCES'
    if len(existing | set(new_ids)) > limit:
        raise ValueError(f'Index limit is {limit} sources ({name}); '
                         'run evaluation/benchmark_corpus.py on the engine before raising it')


def category(metadata):
    """Which collection a source belongs to (for the library summary)."""
    host = urlsplit(metadata.get('url') or '').hostname or ''
    if metadata.get('rights_basis') == 'author_deposit':
        return 'archive'
    if metadata.get('rights_basis') == 'institution_deposit':
        return 'university'
    if metadata.get('source_type') == 'wikipedia' or host.endswith('wikipedia.org'):
        return 'wikipedia'
    if host == 'cyberleninka.ru':
        return 'cyberleninka'
    if 'zenodo' in host or '10.5281/zenodo' in (metadata.get('url') or '').lower():
        return 'zenodo'
    if host.endswith('.uz') or 'oai' in (metadata.get('permission_reference') or '').lower():
        return 'journals'
    return 'other'


def count_category(db, name, language=None):
    return sum(1 for (raw,) in db.execute('SELECT metadata FROM sources')
               if category(json.loads(raw)) == name and (language is None or json.loads(raw).get('language') == language))


def ingest(db, metadata, text):
    metadata = validate(metadata, text)
    _check_capacity(db, [hashlib.sha256(text.encode()).hexdigest()], metadata['rights_basis'] in DEPOSITS)
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
    deposits_ = [r[2] for r in records if r[0]['rights_basis'] in DEPOSITS]
    _check_capacity(db, deposits_, True) if deposits_ else None
    _check_capacity(db, [r[2] for r in records if r[0]['rights_basis'] not in DEPOSITS])
    with db:  # all records or none
        for metadata, text, _, name in records:
            _insert(db, {**metadata, 'source_file': name}, text)
    return records


def deposits(db, owner):
    """(id, metadata) of the works one owner deposited."""
    rows = []
    for identifier, raw in db.execute('SELECT id, metadata FROM sources'):
        metadata = json.loads(raw)
        if metadata.get('rights_basis') in DEPOSITS and metadata.get('owner') == owner:
            rows.append((identifier, metadata))
    return sorted(rows, key=lambda r: r[1].get('imported_at', ''), reverse=True)


def remove_deposit(db, identifier, owner):
    """Delete a deposit only for its owner. True if something was removed."""
    row = db.execute('SELECT metadata FROM sources WHERE id=?', (identifier,)).fetchone()
    if not row:
        return False
    metadata = json.loads(row[0])
    if metadata.get('rights_basis') not in DEPOSITS or metadata.get('owner') != owner:
        return False
    with db:
        return remove(db, identifier)


def restore_deposits(db, path):
    """Load a deposit backup (plagiq-archive.jsonl.gz from /admin) back into the index.
    Existing identical deposits are replaced; returns how many were restored."""
    import gzip
    restored = 0
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            metadata = {k: v for k, v in item['metadata'].items() if k not in ('sha256', 'imported_at', 'words')}
            if metadata.get('rights_basis') not in DEPOSITS:
                raise ValueError('Backup contains a non-deposit record; refusing to restore it')
            ingest(db, metadata, item['text'])
            restored += 1
    return restored


def provider(metadata):
    """Which importer or route a source came through (for /admin): wikipedia, cyberleninka, openalex,
    core, oai_journal, author_deposit, institution_deposit or manual."""
    if metadata.get('rights_basis') in DEPOSITS:
        return metadata['rights_basis']
    if category(metadata) == 'wikipedia':
        return 'wikipedia'
    if category(metadata) == 'cyberleninka':
        return 'cyberleninka'
    reference = (metadata.get('permission_reference') or '').lower()
    if 'openalex' in reference:
        return 'openalex'
    if 'core' in reference.split():
        return 'core'
    if 'oai' in reference or category(metadata) == 'journals':
        return 'oai_journal'
    return 'manual'


SHORT_TEXT = 300          # sources with fewer words are searchable, but are not counted as full texts


def stats(db, now=None):
    """Owner statistics: counts by language, collection, provider and rights basis, indexing freshness
    and searchable full-text coverage. Metadata only; no texts are read."""
    today = (now or datetime.now(timezone.utc)).date()
    out = {'total': 0, 'by_language': {}, 'by_category': {}, 'by_provider': {}, 'by_rights': {},
           'category_language': {}, 'freshness': {}, 'coverage': {}}
    for (raw,) in db.execute('SELECT metadata FROM sources'):
        m = json.loads(raw)
        lang, cat, prov = m.get('language', '?'), category(m), provider(m)
        words = int(m.get('words') or 0)
        out['total'] += 1
        for key, value in (('by_language', lang), ('by_category', cat), ('by_provider', prov),
                           ('by_rights', m.get('rights_basis', '?'))):
            out[key][value] = out[key].get(value, 0) + 1
        out['category_language'].setdefault(cat, {}).setdefault(lang, 0)
        out['category_language'][cat][lang] += 1
        fresh = out['freshness'].setdefault(cat, {'newest': None, 'oldest': None, 'last_7_days': 0, 'last_30_days': 0})
        day = str(m.get('imported_at') or '')[:10]
        if day:
            fresh['newest'] = max(fresh['newest'] or day, day)
            fresh['oldest'] = min(fresh['oldest'] or day, day)
            try:
                age = (today - datetime.fromisoformat(day).date()).days
            except ValueError:
                age = None
            if age is not None and age <= 7:
                fresh['last_7_days'] += 1
            if age is not None and age <= 30:
                fresh['last_30_days'] += 1
        cov = out['coverage'].setdefault(lang, {'sources': 0, 'full_texts': 0, 'short_texts': 0, 'words': 0})
        cov['sources'] += 1
        cov['words'] += words
        cov['full_texts' if words >= SHORT_TEXT else 'short_texts'] += 1
    out['windows'] = db.execute('SELECT COUNT(*) FROM windows').fetchone()[0]
    return out


def find_source(db, doi='', title=''):
    """Indexed sources matching a DOI (in the source URL) or a title (case-insensitive, whole or part).
    Returns [(num, metadata)], at most 5. Never returns text."""
    doi = re.sub(r'^(https?://(dx\.)?doi\.org/|doi:)', '', (doi or '').strip(), flags=re.I).lower()
    title = ' '.join((title or '').lower().split())
    found = []
    for num, raw in db.execute('SELECT num, metadata FROM sources'):
        m = json.loads(raw)
        url = (m.get('url') or '').lower()
        name = ' '.join((m.get('title') or '').lower().split())
        if (doi and doi in url) or (title and len(title) >= 8 and (title == name or title in name)):
            found.append((num, m))
            if len(found) >= 5:
                break
    return found


def diagnose(db, num, passage=''):
    """Is this indexed source really searchable? Checks its fingerprints and runs a search for a
    passage: the one given, or about 60 words sampled internally from the middle of the source.
    Returns facts only (never the source text or the sampled passage)."""
    row = db.execute('SELECT id, metadata, text FROM sources WHERE num=?', (num,)).fetchone()
    if not row:
        return None
    identifier, raw, text = row
    m = json.loads(raw)
    fingerprints = db.execute('SELECT COUNT(*) FROM windows WHERE source=?', (num,)).fetchone()[0]
    sampled = not (passage or '').strip()
    if sampled:
        words = text.split()
        middle = max(0, len(words) // 2 - 30)
        passage = ' '.join(words[middle:middle + 60])
    result = search(db, passage, m['language'], max_passages=1)
    ranks = [i for i, s in enumerate(result['sources']) if s.get('sha256') == identifier]
    hit = result['sources'][ranks[0]] if ranks else None
    return {'title': m.get('title', ''), 'year': m.get('year'), 'language': m['language'], 'url': m.get('url', ''),
            'category': category(m), 'provider': provider(m), 'rights_basis': m.get('rights_basis'),
            'deposit': m.get('rights_basis') in DEPOSITS, 'words': m.get('words'), 'imported_at': m.get('imported_at'),
            'fingerprints': fingerprints, 'searchable': fingerprints > 0,
            'passage': {'source': 'sampled from the indexed text (60 words, middle)' if sampled else 'given by the owner',
                        'words': len(passage.split()), 'found': bool(ranks), 'rank': ranks[0] + 1 if ranks else None,
                        'matched_words': hit['matched_words'] if hit else 0,
                        'other_sources_matched': len(result['sources']) - len(ranks),
                        'boilerplate_windows': result['boilerplate_windows']}}


PRUNABLE = ('wikipedia',)        # only general-reference collections; never journals, theses or deposits


def prune_collection(db, collection, keep_per_language, dry_run=True, chunk=100):
    """Remove texts of a general-reference collection (only Wikipedia), keeping the longest
    `keep_per_language` per language. Never touches journals, theses, CyberLeninka or deposits.
    Deletes in chunks with a WAL checkpoint after each, so a large removal cannot fill the disk.
    Returns {"remove": {lang: n}, "keep": {lang: n}, "removed": [{title, url, language, words}]}."""
    if collection not in PRUNABLE:
        raise ValueError(f'Only these collections can be pruned: {", ".join(PRUNABLE)}')
    keep_per_language = max(0, int(keep_per_language))
    groups = {}
    for num, identifier, raw in db.execute('SELECT num, id, metadata FROM sources'):
        m = json.loads(raw)
        if category(m) == collection and m.get('rights_basis') not in DEPOSITS:
            groups.setdefault(m.get('language', '?'), []).append((int(m.get('words') or 0), num, identifier, m))
    plan, removed = {'remove': {}, 'keep': {}}, []
    doomed = []
    for lang, items in groups.items():
        items.sort(key=lambda x: (-x[0], x[1]))          # longest first: most text per article kept
        plan['keep'][lang] = min(len(items), keep_per_language)
        plan['remove'][lang] = len(items) - plan['keep'][lang]
        for words, num, identifier, m in items[keep_per_language:]:
            doomed.append(num)
            removed.append({'title': m.get('title', ''), 'url': m.get('url', ''), 'language': lang, 'words': words})
    if not dry_run:
        for start in range(0, len(doomed), chunk):
            part = doomed[start:start + chunk]
            marks = ','.join('?' * len(part))
            with db:
                db.execute(f'DELETE FROM windows WHERE source IN ({marks})', part)
                db.execute(f'DELETE FROM seeds WHERE source IN ({marks})', part)
                db.execute(f'DELETE FROM sources WHERE num IN ({marks})', part)
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return {**plan, 'removed': removed}


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
    if getattr(args, 'restore_archive', None):
        with closing(connect(args.db)) as db:
            print(f'Restored {restore_deposits(db, args.restore_archive)} deposit(s).')
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
    if not (args.manifest or args.list or args.remove or getattr(args, 'restore_archive', None)):
        parser.print_help()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Owner-only local corpus tool; verify rights before importing.')
    parser.add_argument('manifest', type=Path, nargs='?', help='JSON array: citation/rights metadata plus local .txt/.docx/.pdf text_file')
    parser.add_argument('--db', default=os.environ.get('CORPUS_DB', 'corpus.sqlite3'))
    parser.add_argument('--check', action='store_true', help='validate the manifest only; write nothing')
    parser.add_argument('--list', action='store_true', help='list indexed sources and coverage by language')
    parser.add_argument('--remove', metavar='SHA256', help='remove one source by its SHA-256 identifier')
    parser.add_argument('--restore-archive', metavar='FILE', type=Path, help='restore deposits from plagiq-archive.jsonl.gz')
    args = parser.parse_args()
    try:
        _cli(args, parser)
    except (ValueError, OSError) as exc:
        raise SystemExit(f'ERROR: {exc}')
