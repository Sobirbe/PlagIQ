"""Regression tests for the PR #1 follow-up audit (synthetic data only)."""
import io
import re
import sqlite3
import unicodedata
import zipfile

import pymupdf as fitz
import pytest
from docx import Document
from fastapi.testclient import TestClient

import app as web
import engine_client
import report_pdf
from corpus import NORMALIZATION_VERSION, connect, ingest, lexical, search, tokens

EN = 'one two three four five six seven eight nine'


@pytest.fixture(autouse=True)
def clear_state(monkeypatch):
    web.results.clear()
    web.RATE.clear()
    monkeypatch.delenv('TRUSTED_PROXY_HOPS', raising=False)


def post(client=None, name='a.txt', content=EN.encode(), **data):
    data = {'mode': 'reference', 'language': 'en', 'reference_text': EN, **data}
    return (client or TestClient(web.app)).post('/api/check', data=data, files={'submission': (name, content)})


def meta(language='en', **extra):
    return dict(title='Synthetic fixture', url='https://example.org/x', language=language, authors='Test author',
                year=2026, rights_basis='written_permission', permission_reference='Owner-created test', **extra)


# --- Unicode normalization -------------------------------------------------

@pytest.mark.parametrize('language,text', [
    ('ru', 'Мой новый объём работы включает чтение научных статей'),
    ('uz', 'Ўзбекистон йўли ва ғоялари илмий ишда муҳим ўрин тутади'),
    ('en', 'Café naïve résumé texts are compared word by word here'),
])
def test_decomposed_text_matches_composed(language, text):
    assert tokens(unicodedata.normalize('NFD', text), language) == tokens(text, language)


def test_russian_yo_and_invisible_characters():
    assert tokens('ещё информация', 'ru') == tokens('еще инфор\u00adма\u200bция', 'ru')


def test_fast_tokens_equal_offset_tokens():
    sample = unicodedata.normalize('NFD', "Ёлка O‘zbek G'ulom oʻqish ŏ don't Ўзбек инфор\u00adмация")
    for language in ('uz', 'ru', 'en'):
        assert tokens(sample, language) == [w for w, _, _ in lexical(sample, language)]


def test_offsets_point_to_original_decomposed_text():
    text = unicodedata.normalize('NFD', 'йод')
    word, start, end = lexical(text, 'ru')[0]
    assert word == 'йод' and text[start:end] == text


def test_index_migrates_v04_database(tmp_path):
    """A database written by v0.4 (hex fingerprints, char columns) upgrades in place."""
    import json
    path = tmp_path / 'old.db'
    text = 'Научный текст ещё содержит семь слов для проверки'
    old = sqlite3.connect(path)
    old.executescript('''CREATE TABLE sources (id TEXT PRIMARY KEY, metadata TEXT NOT NULL, text TEXT NOT NULL);
      CREATE TABLE windows (fingerprint TEXT NOT NULL, source_id TEXT NOT NULL, source_start INTEGER NOT NULL,
      source_end INTEGER NOT NULL); CREATE INDEX window_lookup ON windows(fingerprint);''')
    old.execute('INSERT INTO sources VALUES (?,?,?)', ('abc', json.dumps(meta('ru')), text))
    old.execute("INSERT INTO windows VALUES ('deadbeef','abc',0,5)")
    old.execute('PRAGMA user_version=2')
    old.commit(); old.close()
    db = connect(path)
    assert db.execute('PRAGMA user_version').fetchone()[0] == NORMALIZATION_VERSION
    assert search(db, text.replace('ё', 'е'), 'ru')['unique_passages'] == [[0, len(text)]]


# --- Upload validation -----------------------------------------------------

@pytest.mark.parametrize('name,content,code', [
    ('a.exe', b'x', 'unsupported_type'),
    ('a.txt', b'', 'empty_file'),
    ('a.txt', 'Привет мир как дела у тебя сегодня'.encode('cp1251'), 'txt_encoding'),
    ('a.txt', b'too short', 'too_short'),
    ('a.txt', b'x' * (20 * 1024 * 1024 + 1), 'file_too_large'),
    ('a.pdf', b'%PDF-not really', 'extract_failed'),
    ('a.docx', b'not a zip', 'extract_failed'),
])
def test_invalid_uploads_have_codes(name, content, code):
    response = post(name=name, content=content)
    assert response.status_code in (400, 413)
    assert response.json()['code'] == code


def test_short_reference_is_not_scored_zero():
    response = post(reference_text='hello world')
    assert response.status_code == 400 and response.json()['code'] == 'reference_too_short'


def test_utf16_txt_docx_and_pdf_extract():
    text = 'Научный текст содержит семь слов для проверки совпадений'
    assert post(content=text.encode('utf-16'), language='ru', reference_text=text).json()['percent'] == 100.0
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    assert post(name='a.docx', content=buf.getvalue(), language='ru', reference_text=text).json()['percent'] == 100.0
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), EN)
    assert post(name='a.pdf', content=pdf.tobytes()).json()['percent'] == 100.0


def test_image_only_pdf_reports_no_text():
    pdf = fitz.open()
    pdf.new_page()
    response = post(name='scan.pdf', content=pdf.tobytes())
    assert response.json()['code'] == 'no_text'


def test_validation_error_does_not_echo_input():
    response = TestClient(web.app).post('/api/check', data={'reference_text': 'SECRET-MANUSCRIPT'})
    assert response.status_code == 422
    assert response.json()['code'] == 'invalid_request'
    assert 'SECRET-MANUSCRIPT' not in response.text


def test_middleware_errors_have_codes():
    client = TestClient(web.app)
    assert client.post('/api/check', headers={'Origin': 'https://evil.example'}).json()['code'] == 'origin_rejected'
    assert client.post('/api/check', content=b'x' * (45 * 1024 * 1024 + 1)).json()['code'] == 'request_too_large'


def test_uploads_are_kept_in_memory():
    from starlette.formparsers import MultiPartParser
    assert MultiPartParser.spool_max_size > web.MAX_REQUEST


def test_filename_control_characters_and_paths_removed():
    assert web.clean_filename('C:\\secret\\dir\\paper\x07.txt', 'Document') == 'paper.txt'
    assert web.clean_filename('', 'Document') == 'Document'
    assert post(name='../../etc/x.txt').json()['document'] == 'x.txt'


# --- Rate limiting and concurrency ----------------------------------------

def test_rate_limit_uses_trusted_proxy_hop(monkeypatch):
    monkeypatch.setenv('TRUSTED_PROXY_HOPS', '1')
    client = TestClient(web.app)
    codes = [client.post('/api/check', data={'mode': 'reference', 'language': 'en', 'reference_text': EN},
                         files={'submission': ('a.txt', EN.encode())},
                         headers={'X-Forwarded-For': f'spoofed, 203.0.113.{i}'}).status_code for i in range(14)]
    assert set(codes) == {200}                     # different real clients behind one proxy
    same = [client.post('/api/check', data={'mode': 'reference', 'language': 'en', 'reference_text': EN},
                        files={'submission': ('a.txt', EN.encode())},
                        headers={'X-Forwarded-For': f'{i}.1.1.1, 198.51.100.7'}).status_code for i in range(13)]
    assert same[-1] == 429                         # spoofed left entries cannot evade the limit


def test_proxy_headers_ignored_by_default():
    client = TestClient(web.app)
    codes = [client.post('/api/check', data={'mode': 'reference', 'language': 'en', 'reference_text': EN},
                         files={'submission': ('a.txt', EN.encode())},
                         headers={'X-Forwarded-For': f'203.0.113.{i}'}).status_code for i in range(13)]
    assert codes[-1] == 429


def test_rejects_when_scan_slots_full(monkeypatch):
    monkeypatch.setattr(web, 'active_scans', 99)
    response = post()
    assert response.status_code == 503 and response.json()['code'] == 'server_busy'


# --- Engine evidence and summary validation --------------------------------

def test_passages_are_merged_not_sliding_windows(tmp_path):
    source = ' '.join(f'w{i}' for i in range(60))
    with connect(tmp_path / 'i.db') as db:
        ingest(db, meta(), source)
        result = search(db, 'intro words ' + source, 'en')
    passages = result['sources'][0]['passages']
    assert len(passages) == 1 and passages[0]['source_start'] == 0 and passages[0]['source_end'] == len(source)
    assert 'permission_reference' not in result['sources'][0]


def test_search_never_writes_manuscript(tmp_path):
    path = tmp_path / 'i.db'
    with connect(path) as db:
        ingest(db, meta(), EN)
    secret = 'UNIQUEMANUSCRIPTMARKER one two three four five six seven eight nine'
    db = connect(path)
    search(db, secret, 'en')
    db.close()
    assert b'UNIQUEMANUSCRIPTMARKER' not in path.read_bytes()


def valid_report(**source):
    return {'query_text_length': 20, 'unique_passages': [[0, 10]], 'corpus_sources': 1,
            'language_counts': {'uz': 0, 'ru': 0, 'en': 1},
            'sources': [{'source_filename': 'T', 'source_url': 'https://e.org', 'matched_chars': 10,
                         'passages': [], **source}]}


def test_summary_sanitizes_untrusted_fields():
    report = valid_report(year={'x': 1}, authors='A' * 5000, rights_basis='stolen', extra='<script>',
                          passages=[{'query_start': 0, 'query_end': 10, 'source_start': 0, 'source_end': 10,
                                     'source_excerpt': 'x', 'evil': 1}])
    report['language_counts']['zz'] = 5
    src = engine_client.summarize(report)['sources'][0]
    assert src['year'] is None and len(src['authors']) == 300 and src['rights_basis'] == 'unknown'
    assert 'extra' not in src and set(src['passages'][0]) == {'query_start', 'query_end', 'source_start', 'source_end', 'source_excerpt', 'words'}
    assert engine_client.summarize(report)['language_counts'] == {'uz': 0, 'ru': 0, 'en': 1}


@pytest.mark.parametrize('mutate', [
    lambda r: r.update(sources={'not': 'a list'}),
    lambda r: r['sources'][0].update(matched_chars=21),
    lambda r: r['sources'][0].update(passages=[{'query_start': 5, 'query_end': 2, 'source_start': 0, 'source_end': 1}]),
    lambda r: r.update(corpus_sources=7),
    lambda r: r.update(query_text_length=True),
])
def test_summary_rejects_malformed(mutate):
    report = valid_report()
    mutate(report)
    with pytest.raises(engine_client.EngineError):
        engine_client.summarize(report)


def test_corpus_result_language_without_sources_is_not_evaluated(monkeypatch):
    async def fake(text, language, exclude=()):
        return {'query_text_length': len(text), 'unique_passages': [], 'sources': [], 'corpus_sources': 1,
                'language_counts': {'uz': 0, 'ru': 1, 'en': 0}, 'coverage': 'x'}
    monkeypatch.setattr(web, 'corpus_configured', lambda: True)
    monkeypatch.setattr(web, 'run_check', fake)
    data = post(mode='corpus', consent='true').json()
    assert data['percent'] is None and data['not_evaluated_reason'] == 'no_sources_in_language'
    assert data['unit'] == 'words'


def test_engine_busy_is_reported(monkeypatch):
    async def busy(text, language, exclude=()):
        raise engine_client.EngineError('busy', 'engine_busy')
    monkeypatch.setattr(web, 'corpus_configured', lambda: True)
    monkeypatch.setattr(web, 'run_check', busy)
    response = post(mode='corpus', consent='true')
    assert response.status_code == 503 and response.json()['code'] == 'engine_busy'


def test_academic_reason_distinguishes_unavailable_and_no_abstracts(monkeypatch):
    monkeypatch.setattr(web, 'discover', lambda text, *a: dict(sources=[{'title': 'T', 'url': 'https://e.org', 'abstract': ''}],
                                                           status='searched', message='', queries=1))
    data = post(mode='academic', consent='true').json()
    assert data['percent'] is None and data['not_evaluated_reason'] == 'no_abstracts' and data['source_count'] == 1


# --- PDF report -------------------------------------------------------------

def pdf_text(client, identifier, lang):
    response = client.get(f'/api/report/{identifier}.pdf?full=1&lang={lang}')
    assert response.status_code == 200
    with fitz.open(stream=response.content, filetype='pdf') as pdf:
        return ''.join(page.get_text() for page in pdf), pdf.page_count


@pytest.mark.parametrize('lang,expected', [('en', 'Scope-limited evidence report'),
                                           ('ru', 'Отчёт о совпадениях'), ('uz', 'dalillar hisoboti')])
def test_pdf_is_localized_and_never_prints_none(lang, expected):
    client = TestClient(web.app)
    identifier = post(client).json()['report_id']
    web.results[identifier]['result'].update(mode='corpus', unit='characters', matched_tokens=None,
                                             matched_chars=10, total_chars=40, sources_truncated=True,
                                             matched_source_count=30, language_counts={'uz': 0, 'ru': 0, 'en': 1})
    text, _ = pdf_text(client, identifier, lang)
    assert expected in text and 'None' not in text
    assert re.search(r'Page (\d+) of \1|Стр\. (\d+) из \2|(\d+) sahifadan \3', text)


def test_pdf_discloses_exclusions_and_truncation():
    client = TestClient(web.app)
    identifier = post(client).json()['report_id']
    web.results[identifier]['result'].update(sources=[{'title': 'Src ' + 'L' * 400, 'url': 'https://e.org/' + 'p' * 300,
        'authors': 'Муаллиф', 'year': 2026, 'rights_basis': 'open_license', 'matched_chars': 5, 'passage_count': 9,
        'passages': [{'query_start': 0, 'query_end': 5, 'source_start': 1, 'source_end': 6, 'source_excerpt': 'Ўзбек'}]}] * 20,
        sources_truncated=True, matched_source_count=40)
    text, pages = pdf_text(client, identifier, 'en')
    assert 'No exclusions' in text and 'Showing 15 of 40' in text and 'Open licence' in text
    assert '8 further passage' in text and 'Ўзбек' in text and pages > 1 and f'Page {pages} of {pages}' in text


def test_uzbek_strings_are_latin_script():
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    assert not [s for s in strings(report_pdf.T['uz']) if re.search('[\u0400-\u04ff]', s)]


def test_pdf_bold_unicode_font_available():
    assert report_pdf.UNICODE_FONT, 'Install fonts-dejavu-core so Cyrillic renders in PDFs'


def test_academic_pdf_counts_discovered_records(monkeypatch):
    records = [{'title': f'Record {i}', 'url': f'https://doi.org/10.1/{i}', 'abstract': 'unrelated words only here',
                'coverage': 'abstract', 'year': 2024} for i in range(24)]
    monkeypatch.setattr(web, 'discover', lambda text, *a: dict(sources=records, status='searched', message='', queries=3))
    client = TestClient(web.app)
    data = post(client, mode='academic', consent='true').json()
    assert data['percent'] == 0 and data['source_count'] == 24
    text, _ = pdf_text(client, data['report_id'], 'en')
    text = ' '.join(text.split())
    assert 'Showing 15 of 24 discovered records' in text and 'of 0 sources' not in text
