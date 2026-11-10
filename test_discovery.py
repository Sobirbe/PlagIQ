"""Multi-excerpt OpenAlex discovery: what is sent, merging and failure states."""
import io
from urllib.error import URLError

import pytest

import discovery

BODY = ('Remittances and household welfare in Uzbekistan\n'
        + 'Introduction sentence about migration patterns. ' * 3
        + 'Households receiving remittances substantially increased educational expenditure across rural provinces. '
        + 'Middle filler text about data. ' * 20
        + 'Instrumental variable estimates demonstrate heterogeneous consumption smoothing effects among recipients. '
        + 'More filler about results here. ' * 20
        + 'Policymakers should strengthen financial inclusion mechanisms supporting productive investment decisions. '
        + '\nReferences\n1. Karimov A. Uniquebibliographyword remittances policy analysis framework. 2020.')


def test_queries_are_bounded_distinct_and_skip_bibliography():
    queries = discovery.suggest_queries(BODY)
    assert 2 <= len(queries) <= 3 and len(set(queries)) == len(queries)
    assert all(len(q.split()) <= 14 for q in queries)
    assert queries[0].startswith('Remittances and household welfare')
    assert not any('Uniquebibliographyword' in q for q in queries)
    assert len(discovery.suggest_queries('x' * 300000)[0]) <= 220
    body = 'Title of the paper about digital universities\n' + ' '.join(
        f'Sentence number {i} discusses institutional transformation regarding universities.' for i in range(60))
    for q in discovery.suggest_queries(body):                      # never starts mid-word
        assert q.split()[0] in ('Title', 'Sentence'), q


def work(i, abstract=True):
    return {'id': f'https://openalex.org/W{i}', 'display_name': f'Work {i}', 'doi': f'https://doi.org/10.1/{i}',
            'publication_year': 2024, 'abstract_inverted_index': {'word': [0]} if abstract else None}


def test_results_are_merged_without_duplicates(monkeypatch):
    calls = []
    def fake(query, language, max_results):
        calls.append((query, language))
        return [work(1), work(len(calls) + 1)]
    monkeypatch.setattr(discovery, '_search', fake)
    result = discovery.discover(BODY, language='uz')
    assert result['status'] == 'searched' and result['queries'] == len(calls)
    assert [s['title'] for s in result['sources']].count('Work 1') == 1
    assert {lang for _, lang in calls} == {'uz'}


def test_partial_and_total_failure(monkeypatch):
    state = {'n': 0}
    def flaky(query, language, max_results):
        state['n'] += 1
        if state['n'] == 1:
            raise URLError('down')
        return [work(state['n'])]
    monkeypatch.setattr(discovery, '_search', flaky)
    partial = discovery.discover(BODY)
    assert partial['status'] == 'partial' and partial['failed_queries'] == 1 and partial['sources']
    monkeypatch.setattr(discovery, '_search', lambda *a: (_ for _ in ()).throw(TimeoutError()))
    assert discovery.discover(BODY)['status'] == 'unavailable'


def test_language_filter_and_key_header(monkeypatch):
    seen = {}
    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    def fake_urlopen(request, timeout):
        seen['url'], seen['auth'] = request.full_url, request.get_header('Authorization')
        return Response(b'{"results": []}')
    monkeypatch.setattr(discovery, 'urlopen', fake_urlopen)
    monkeypatch.setenv('OPENALEX_API_KEY', 'k')
    discovery._search('query words', 'uz', 8)
    assert 'filter=language%3Auz%7Cru' in seen['url'] and 'api_key' not in seen['url'] and seen['auth'] == 'Bearer k'
