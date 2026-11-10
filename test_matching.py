"""Properties the exact matcher and the rights-gated importer must keep."""
import asyncio
import json

import pymupdf as fitz
import httpx
import pytest
from fastapi.testclient import TestClient

import app as web
import corpus
import engine_client
import engine_service
from corpus import compare_texts, connect, import_manifest, ingest, load_manifest, search
from extract import dehyphenate, extract_text

SRC = 'alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu'
FILL = 'one two three four five six seven eight nine ten'


def meta(language='en', **extra):
    return dict(title='Synthetic fixture', url='https://example.org/x', language=language, authors='Test author',
                year=2026, rights_basis='written_permission', permission_reference='Owner-created test', **extra)


@pytest.fixture(autouse=True)
def clear():
    web.results.clear()
    web.RATE.clear()
    engine_service.reports.clear()


def test_hash_collisions_never_become_matches(monkeypatch):
    monkeypatch.setattr(corpus, 'fingerprint', lambda words: 1)   # every window "collides"
    _, (evidence,), covered, _ = compare_texts(FILL, [SRC], 'en')
    assert evidence is None and not covered


def test_passage_boundaries_and_source_alignment():
    manuscript = FILL + ' ' + SRC + ' ' + FILL
    q, (evidence,), covered, _ = compare_texts(manuscript, ['Intro words. ' + SRC + '. End.'], 'en')
    (passage,) = evidence['passages']
    assert manuscript[passage['query_start']:passage['query_end']] == SRC
    assert passage['source_excerpt'] == SRC and passage['words'] == 12 and len(covered) == 12


def test_repeated_phrase_in_source_is_reported_once():
    q, (evidence,), covered, _ = compare_texts(SRC, [SRC + ' filler words here ' + SRC], 'en')
    assert evidence['passage_count'] == 1 and len(covered) == 12


def test_two_separate_passages_and_six_word_gap_not_matched():
    a, b = SRC, 'red orange yellow green blue indigo violet white black'
    manuscript = a + ' my own sentence sits here now ' + b + ' ' + 'pi rho sigma tau upsilon phi'
    _, (evidence,), covered, _ = compare_texts(manuscript, [a + ' x y z ' + b + ' gap ' + 'pi rho sigma tau upsilon phi'], 'en')
    assert evidence['passage_count'] == 2
    assert len(covered) == 12 + 9                     # the 6-word tail is below the threshold


def test_punctuation_and_case_do_not_change_word_percent():
    plain = web.compare(SRC, SRC, 'en')['percent']
    noisy = web.compare(SRC.upper().replace(' ', ', ') + '.', SRC, 'en')['percent']
    assert plain == noisy == 100.0


def test_overlapping_sources_counted_once():
    manuscript = FILL + ' ' + SRC
    result = web.compare_sources(manuscript, [{'abstract': SRC}, {'abstract': SRC + ' extra'},
                                              {'abstract': ''}], 'en')
    assert result['matched_tokens'] == 12 and result['percent'] == round(100 * 12 / 22, 2)
    assert [s['matched_tokens'] for s in result['sources']] == [12, 12, 0]


def test_every_mode_reports_the_same_number(monkeypatch, tmp_path):
    manuscript, source = FILL + ' ' + SRC + ' closing remark', 'Header. ' + SRC + ' footer'
    reference = web.compare(manuscript, source, 'en')
    academic = web.compare_sources(manuscript, [{'abstract': source}], 'en')
    monkeypatch.setenv('ENGINE_API_KEY', 'x' * 32)
    monkeypatch.setenv('ENGINE_URL', 'http://engine')
    monkeypatch.setenv('CORPUS_DB', str(tmp_path / 'i.db'))
    with connect() as db:
        ingest(db, meta(), source)
    real = httpx.AsyncClient
    monkeypatch.setattr(engine_client.httpx, 'AsyncClient',
                        lambda **kw: real(**kw, transport=httpx.ASGITransport(app=engine_service.app)))
    data = TestClient(web.app).post('/api/check', data={'mode': 'corpus', 'language': 'en', 'consent': 'true'},
                                    files={'submission': ('m.txt', manuscript.encode())}).json()
    assert reference['percent'] == academic['percent'] == data['percent'] == round(100 * 12 / 24, 2)
    assert reference['matched_tokens'] == academic['matched_tokens'] == data['matched_tokens'] == 12
    assert len(data['corpus_version']) == 12 and '<mark' in data['highlight_html']


def test_reference_mode_returns_evidence_and_highlight():
    response = TestClient(web.app).post('/api/check', data={'mode': 'reference', 'language': 'en',
                                        'reference_text': 'Header. ' + SRC}, files={'submission': ('m.txt', (FILL + ' ' + SRC).encode())})
    data = response.json()
    assert data['sources'][0]['passages'][0]['source_excerpt'] == SRC
    assert data['highlight_html'].count('<mark') == 1 and data['unit'] == 'words'


def test_pdf_line_break_hyphenation_is_repaired():
    assert dehyphenate('приме-\nнение и Latin-\nCyrillic') == 'применение и Latin-\nCyrillic'
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), 'The compari-\nson of documents', fontsize=11)
    assert 'comparison' in extract_text('a.pdf', pdf.tobytes())
    assert dehyphenate("xo'-\njalik va xo\u02bb-\njalik") == "xo'jalik va xo\u02bbjalik"


# ------------------------------------------------------------- importer

def write_manifest(tmp_path, records, files):
    for name, content in files.items():
        (tmp_path / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(records, ensure_ascii=False))
    return path


def test_import_is_all_or_nothing(tmp_path):
    good = {**meta(), 'text_file': 'a.txt'}
    bad = {**meta(), 'text_file': 'b.txt', 'rights_basis': 'found_online'}
    path = write_manifest(tmp_path, [good, bad], {'a.txt': SRC, 'b.txt': FILL})
    with connect(tmp_path / 'i.db') as db:
        with pytest.raises(ValueError, match='record 2'):
            import_manifest(db, path)
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0


def test_import_pdf_docx_and_lists_problems(tmp_path):
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), 'Научный текст содержит семь слов для проверки совпадений',
                               fontfile='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', fontname='dejavu')
    records = [{**meta('ru'), 'text_file': 'ru.pdf'},
               {**meta('ru'), 'text_file': 'latin.txt'},
               {**meta(), 'text_file': '../outside.txt'},
               {**meta(), 'text_file': 'scan.pdf'},
               {**meta(), 'year': '2026', 'text_file': 'a.txt'}]
    blank = fitz.open(); blank.new_page()
    path = write_manifest(tmp_path, records, {'ru.pdf': pdf.tobytes(), 'latin.txt': SRC,
                                              'scan.pdf': blank.tobytes(), 'a.txt': SRC})
    with pytest.raises(ValueError) as info:
        load_manifest(path)
    message = str(info.value)
    assert 'record 1' not in message
    for expected in ('record 2: Declared Russian', 'record 3: text_file must be', 'record 4: Source has no readable text',
                     'record 5: Year'):
        assert expected in message


def test_source_limit_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv('CORPUS_MAX_SOURCES', '1')
    with connect(tmp_path / 'i.db') as db:
        ingest(db, meta(), SRC)
        ingest(db, meta(), SRC)                        # re-import of same text is allowed
        with pytest.raises(ValueError, match='Index limit is 1'):
            ingest(db, meta(), FILL)


def test_corpus_version_changes_with_content(tmp_path):
    with connect(tmp_path / 'i.db') as db:
        ingest(db, meta(), SRC)
        first = search(db, SRC, 'en')['corpus_version']
        ingest(db, meta(), FILL)
        assert search(db, SRC, 'en')['corpus_version'] != first


def test_remove_deletes_source_and_fingerprints(tmp_path):
    with connect(tmp_path / 'i.db') as db:
        keep = ingest(db, meta(), SRC)
        gone = ingest(db, meta(), FILL)
        with db:
            assert corpus.remove(db, gone) and not corpus.remove(db, gone)
        assert db.execute('SELECT count(DISTINCT source) FROM windows').fetchone()[0] == 1
        assert search(db, FILL, 'en')['sources'] == [] and search(db, SRC, 'en')['sources'][0]['sha256'] == keep


def test_uzbek_tutuq_belgisi_keeps_one_word():
    assert corpus.tokens("Ta'lim maʼno sanʼat таълим", 'uz') == ["ta'lim", "ma'no", "san'at", "ta'lim"]
    assert corpus.tokens("o'zbek 'quoted' word", 'uz')[0] == '\ue000zbek'


def test_mixed_alphabet_words_match_their_correct_spelling():
    for language, mixed, clean in [
            ('ru', 'Aйблaнувчининг қaеpдa Pоссия', 'Айбланувчининг қаерда Россия'),
            ('uz', 'Aйблaнувчининг қaеpдa', "Ayblanuvchining qayerda"),
            ('en', 'Thе rеsеarch оf scіence', 'The research of science')]:
        assert corpus.tokens(mixed, language) == corpus.tokens(clean, language)
        assert [w for w, _, _ in corpus.lexical(mixed, language)] == corpus.tokens(mixed, language)
    assert corpus.tokens('CO2 и NaCl', 'ru') == corpus.tokens('СО2 и NaCl', 'ru')  # normalized identically
    assert corpus.tokens("ko'p bo", 'uz') == ['k\ue000p', 'bo']                   # Uzbek Latin words never converted


def test_language_label_checked_against_letters(tmp_path):
    uz_cyr = 'Олий таълим муассасаларида сифат муҳим ва долзарб масала ҳисобланади ўқитувчи ғоя'
    ru = 'Научный текст о качестве высшего образования в регионах и новых университетах страны'
    with connect(tmp_path / 'i.db') as db:
        with pytest.raises(ValueError, match='set language to "uz"'):
            ingest(db, meta('ru'), uz_cyr)
        with pytest.raises(ValueError, match='set language to "ru"'):
            ingest(db, meta('uz'), ru)
        ingest(db, meta('uz'), uz_cyr)
        ingest(db, meta('ru'), ru)


def test_detect_language_from_text():
    from corpus import detect_language
    ru = 'Научный текст о качестве высшего образования в регионах страны и новых университетах. ' * 5
    uz_cyr = 'Олий таълим муассасаларида сифат муҳим ва долзарб масала ҳисобланади. ' * 5
    uz_lat = "Oliy ta'lim sifati bilan bog'liq masalalar va ularning yechimi uchun yangi yondashuv kerak bo'lib turibdi. " * 6
    en = 'The quality of higher education is assessed with the help of new indicators for the region. ' * 6
    assert [detect_language(t) for t in (ru, uz_cyr, uz_lat, en)] == ['ru', 'uz', 'uz', 'en']
    assert detect_language('short') is None
