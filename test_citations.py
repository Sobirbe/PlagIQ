"""Citation-aware classification: rules, exclusions and the web workflow."""
import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

import app as web
from citations import assess, bibliography_span, is_cited, quotation_spans
from corpus import compare_texts

COPY = 'alpha beta gamma delta epsilon zeta eta theta iota kappa'


@pytest.fixture(autouse=True)
def clear():
    web.results.clear()
    web.RATE.clear()


def test_uzbek_apostrophes_are_not_quotation_marks():
    text = "O‘zbek g‘oya ta’lim maʼno o'qish va oʻquvchi"
    assert quotation_spans(text) == []


@pytest.mark.parametrize('text', ['«a „b“ c»', '“a ‘b’ c”', '„a“', '"a" and "b"'])
def test_quotation_pairs(text):
    spans = quotation_spans(text)
    assert spans and spans[0][0] == 0


def test_unclosed_and_cross_paragraph_quotes_are_ignored():
    assert quotation_spans('He said "start\n\nnew paragraph" end') == []
    assert quotation_spans('«never closed ' + 'x ' * 50) == []


@pytest.mark.parametrize('ref', ['[12]', '[3, p. 45]', '[3, b. 45]', '[2, с. 15]', '(Karimov, 2019)',
                                 '(Иванов, 2018, с. 5)', '(Smith et al., 2020a)', '¹'])
def test_reference_markers(ref):
    text = '«quoted words here»' + (' ' if ref != '¹' else '') + ref
    assert is_cited(text, quotation_spans(text)[0][1])


def test_reference_must_be_near_and_before_next_quote():
    text = '«quoted»' + ' filler' * 15 + ' [3]'
    assert not is_cited(text, 8)
    assert not is_cited('«one» «two» [3]', 5)


def test_bibliography_heading_rules():
    body = 'word ' * 100
    assert bibliography_span('Contents\nReferences\n' + body) is None          # table of contents only
    text = 'Contents\nReferences\n' + body + '\nReferences\n1. Item\nAppendix A\nTable'
    start, end, heading = bibliography_span(text)
    assert text[start:end].strip().startswith('References') and 'Appendix' not in text[start:end]
    for heading in ('Список использованной литературы', 'Foydalanilgan adabiyotlar ro‘yxati', 'Адабиётлар рўйхати',
                    '5. REFERENCES', 'Литература:'):
        assert bibliography_span(body + '\n' + heading + '\n1. x') is not None, heading
    assert bibliography_span(body + '\nThe references show that\n') is None     # a sentence is not a heading


def manuscript():
    return ('Intro text here. ' + f'Author wrote «{COPY}» [4]. ' + 'middle part of the essay text. '
            + f'Plain {COPY} again. ' + 'word ' * 30 + f'\nReferences\n1. {COPY}. 2020.')


def test_exclusions_change_only_what_was_chosen():
    text = manuscript()
    q, _, _, intervals = compare_texts(text, [COPY], 'en')
    base = assess(text, 'en', intervals, q=q)
    assert base['locations'] == {'quotation_cited': 10, 'quotation_uncited': 0, 'bibliography': 10, 'other': 10}
    assert base['percent_all'] == base['percent_counted'] and sum(base['excluded'].values()) == 0
    quotes = assess(text, 'en', intervals, q=q, exclude_quotes=True)
    both = assess(text, 'en', intervals, q=q, exclude_quotes=True, exclude_bibliography=True)
    short = assess(text, 'en', intervals, q=q, min_words=15)
    assert quotes['matched_words_counted'] == 20 and both['matched_words_counted'] == 10
    assert short['matched_words_counted'] == 0 and short['excluded']['short'] == 30
    assert quotes['total_words'] == base['total_words']                          # denominator never changes


def test_web_options_and_report():
    client = TestClient(web.app)
    def run(**options):
        return client.post('/api/check', data={'mode': 'reference', 'language': 'en', 'reference_text': COPY, **options},
                           files={'submission': ('m.txt', manuscript().encode())}).json()
    plain, excluded = run(), run(exclude_quotes='true', exclude_bibliography='true')
    assert plain['percent'] == plain['percent_all'] and excluded['percent_all'] == plain['percent']
    assert excluded['percent'] < plain['percent'] and excluded['citation']['excluded'] == {'bibliography': 10, 'quotations': 10, 'short': 0}
    assert 'class="qc x"' in excluded['highlight_html'] and 'class="bib x"' in excluded['highlight_html']
    assert 'segments' not in excluded['citation']
    pdf = client.get(f"/api/report/{excluded['report_id']}.pdf?full=1&lang=ru").content
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        text = ' '.join(''.join(page.get_text() for page in doc).split())
    assert 'Исключения, выбранные пользователем' in text and 'цитаты (10 слов)' in text and 'References' in text
    assert client.post('/api/check', data={'mode': 'reference', 'language': 'en', 'reference_text': COPY, 'min_words': '8'},
                       files={'submission': ('m.txt', manuscript().encode())}).json()['code'] == 'invalid_option'
