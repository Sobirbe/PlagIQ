"""Internet search module (Yandex Search API v2) against a local simulated API and web site."""
import base64
import http.server
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import app as web
import websearch
from harvest import Fetcher

COPIED = ("Remittance inflows reshape rural household budgets across the Fergana valley through seasonal consumption "
          "smoothing, investment in housing, and payment of school fees for younger children. ")
PAGE_RU = ("<html><head><meta charset='windows-1251'><script>var hidden = 'не текст';</script></head><body>"
           "<nav>Меню сайта</nav><p>" + "Обзор литературы о трудовой миграции в Центральной Азии. " * 30
           + "</p><p>Денежные переводы мигрантов меняют бюджеты сельских домохозяйств Ферганской долины.</p></body></html>")
PAGE_EN = "<html><body><article><p>" + "Background on household economics. " * 40 + COPIED + "</p></article></body></html>"

BRAVE = []                                                   # Brave requests seen by the simulated API


def xml(urls, error=None):
    if error:
        body = f'<response><error code="{error}">Sorry, there are no results</error></response>'
    else:
        docs = "".join(f"<group><doc><url>{u}</url><title>Page <hlword>{i}</hlword></title></doc></group>"
                       for i, u in enumerate(urls))
        body = f"<response><results><grouping>{docs}</grouping></results></response>"
    return f'<?xml version="1.0" encoding="utf-8"?><yandexsearch version="1.0">{body}</yandexsearch>'.encode()


@pytest.fixture()
def site(monkeypatch):
    calls, brave = [], BRAVE
    brave.clear()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.headers['Authorization'], request))
            text = request['query']['queryText']
            if text.startswith('"'):                                      # exact phrase: nothing found
                raw = xml([], error="15")
            elif 'broken' in text:
                self.send_response(403); self.end_headers(); return
            else:
                raw = xml([base + '/en', base + '/ru', base + '/en#top', base + '/short'])
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'rawData': base64.b64encode(raw).decode()}).encode())

        def do_GET(self):
            if self.path.startswith('/brave'):
                query = parse_qs(urlsplit(self.path).query)
                brave.append((self.headers['X-Subscription-Token'], query, time.monotonic()))
                q = query['q'][0]
                if 'limited' in q:
                    self.send_response(429); self.end_headers(); return
                results = [] if q.startswith('"') else [{'url': base + '/en', 'title': 'Household &amp; economics'},
                                                        {'url': 'javascript:alert(1)', 'title': 'x'}, {'title': 'no url'}]
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'type': 'search', 'web': {'results': results}}).encode())
                return
            routes = {'/en': ('text/html; charset=utf-8', PAGE_EN.encode()),
                      '/ru': ('text/html', PAGE_RU.encode('windows-1251')),
                      '/short': ('text/html', b'<p>Too short.</p>'), '/robots.txt': ('text/plain', b'')}
            ctype, body = routes.get(self.path, ('text/plain', b'missing'))
            self.send_response(200 if self.path in routes else 404)
            self.send_header('Content-Type', ctype)
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('YANDEX_API_KEY', 'test-key')
    monkeypatch.setenv('YANDEX_FOLDER_ID', 'b1gtestfolder')
    monkeypatch.setenv('YANDEX_SEARCH_URL', base + '/v2/web/search')
    monkeypatch.setenv('BRAVE_SEARCH_URL', base + '/brave')
    monkeypatch.setenv('BRAVE_INTERVAL', '0')
    monkeypatch.delenv('BRAVE_API_KEY', raising=False)
    monkeypatch.delenv('WEB_SEARCH', raising=False)
    yield base, calls
    server.shutdown()


def local_fetcher():
    return Fetcher(delay=0, allow_http_hosts={'127.0.0.1'})


def test_settings_require_key_and_folder(monkeypatch):
    for name in ('YANDEX_API_KEY', 'YANDEX_FOLDER_ID', 'WEB_SEARCH', 'BRAVE_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    assert websearch.settings() == (False, 10, 20)
    monkeypatch.setenv('YANDEX_API_KEY', 'k')
    assert websearch.settings()[0] is False                       # folder ID missing
    monkeypatch.setenv('YANDEX_FOLDER_ID', 'f')
    monkeypatch.setenv('WEB_MAX_PAGES', '99'); monkeypatch.setenv('WEB_SECONDS', '1')
    assert websearch.settings() == (True, 20, 5)
    monkeypatch.setenv('WEB_SEARCH', '0')
    assert websearch.settings()[0] is False


def test_parse_results_and_errors():
    assert websearch.parse_results(xml(['https://a.uz/x', 'ftp://bad'])) == [('https://a.uz/x', 'Page 0')]
    assert websearch.parse_results(xml([], error="15")) == []
    with pytest.raises(websearch.HarvestError):
        websearch.parse_results(xml([], error="32"))
    assert websearch.parse_results(b'<!DOCTYPE x [<!ENTITY a "b">]><yandexsearch/>') == []


def test_search_exact_phrase_then_unquoted_and_dedupe(site):
    base, calls = site
    found, searches, errors, used = websearch.search(['household budgets Fergana valley'], 'ru')
    assert searches == 2 and not errors and used == ['Yandex']
    assert [u for u, _ in found] == [base + '/en', base + '/ru', base + '/short']    # '#top' duplicate removed
    auth, body = calls[0]
    assert auth == 'Api-Key test-key' and body['folderId'] == 'b1gtestfolder'
    assert body['query']['queryText'] == '"household budgets Fergana valley"'
    assert body['query']['searchType'] == 'SEARCH_TYPE_RU' and calls[1][1]['query']['queryText'] == 'household budgets Fergana valley'
    _, _, errors, _ = websearch.search(['broken query'], 'en')
    assert errors == ['Yandex: search refused (403)']


def test_page_text_decodes_windows_1251_and_skips_scripts(site):
    base, _ = site
    text = websearch.page_text(base + '/ru', local_fetcher())
    assert 'Ферганской долины' in text and 'не текст' not in text and 'Меню сайта' not in text


def test_run_compares_pages_and_reports_failures(site):
    base, _ = site
    sources, stats = websearch.run(['household budgets'], 'en', fetcher_factory=local_fetcher)
    assert stats['enabled'] and stats['searches'] == 2 and stats['results'] == 3
    assert stats['attempted'] == 3 and stats['compared'] == 2 and stats['failed'] == 1
    assert stats['reasons'] == {'too little text on page': 1}
    assert {s['coverage'] for s in sources} == {'web'}


def test_check_uses_web_only_with_consent_and_keeps_no_page_text(site, monkeypatch):
    base, calls = site
    monkeypatch.setattr(web, 'discover', lambda text, *a: dict(sources=[], status='searched', message='', queries=1,
                                                               query_texts=['household budgets']))
    monkeypatch.setattr(websearch.fulltext, 'make_fetcher', local_fetcher)
    manuscript = 'My own introduction to the topic of this essay. ' + COPIED + 'My own conclusion follows here.'

    def check(extra):
        web.RATE.clear(); web.results.clear()
        return TestClient(web.app).post('/api/check', data={'mode': 'academic', 'language': 'en', 'consent': 'true', **extra},
                                        files={'submission': ('m.txt', manuscript.encode())}).json()

    without = check({})
    assert calls == [] and without['web'] == {'enabled': False} and without['percent'] is None
    data = check({'web_search': 'true'})
    assert data['web']['compared'] == 2 and data['percent'] > 50
    top = max(data['sources'], key=lambda s: s['matched_tokens'])           # page order depends on download timing
    assert top['coverage'] == 'web' and top['url'] == base + '/en' and top['passages']
    assert 'Yandex' in data['scope']
    assert 'Background on household economics' not in repr(web.results[data['report_id']])
    assert TestClient(web.app).get('/health').json()['web_search'] is True


def test_provider_order_by_language(monkeypatch):
    monkeypatch.delenv('WEB_SEARCH', raising=False)
    monkeypatch.setenv('YANDEX_API_KEY', 'k'); monkeypatch.setenv('YANDEX_FOLDER_ID', 'f')
    monkeypatch.setenv('BRAVE_API_KEY', 'b')
    assert websearch.providers('en') == ['Brave', 'Yandex']
    assert websearch.providers('ru') == websearch.providers('uz') == ['Yandex', 'Brave']
    monkeypatch.delenv('YANDEX_API_KEY')
    assert websearch.providers('ru') == ['Brave'] and websearch.settings()[0]


def test_parse_brave_ignores_bad_items():
    data = {'web': {'results': [{'url': 'https://a.uz/x', 'title': 'A &amp; B'}, {'url': 'ftp://x'}, 'junk', {'title': 't'}]}}
    assert websearch.parse_brave(data) == [('https://a.uz/x', 'A & B')]
    assert websearch.parse_brave({'type': 'search'}) == [] and websearch.parse_brave([]) == []


def test_brave_search_english_first_and_request_format(site, monkeypatch):
    base, calls = site
    monkeypatch.setenv('BRAVE_API_KEY', 'brave-token')
    found, searches, errors, used = websearch.search(['household budgets'], 'en')
    assert used == ['Brave'] and calls == [] and searches == 2 and not errors        # Yandex not needed
    assert found == [(base + '/en', 'Household & economics')]
    token, query, _ = BRAVE[0]
    assert token == 'brave-token' and query['q'] == ['"household budgets"'] and query['search_lang'] == ['en']
    BRAVE.clear()
    websearch.search(['household budgets'], 'uz')                                    # Yandex first for Uzbek
    assert calls and not BRAVE


def test_falls_back_to_second_provider_and_spaces_brave_requests(site, monkeypatch):
    base, calls = site
    monkeypatch.setenv('BRAVE_API_KEY', 'brave-token')
    found, _, errors, used = websearch.search(['limited query'], 'en')               # Brave 429 -> Yandex
    assert errors == ['Brave: search refused (429)'] and used == ['Yandex'] and found
    monkeypatch.delenv('YANDEX_API_KEY')
    monkeypatch.setenv('BRAVE_INTERVAL', '0.3')
    BRAVE.clear()
    websearch.search(['one', 'two'], 'en')
    times = [t for _, _, t in BRAVE]
    assert len(times) == 4 and min(b - a for a, b in zip(times, times[1:])) >= 0.25


def test_check_with_brave_reports_provider(site, monkeypatch):
    base, _ = site
    monkeypatch.delenv('YANDEX_API_KEY')
    monkeypatch.setenv('BRAVE_API_KEY', 'brave-token')
    monkeypatch.setattr(web, 'discover', lambda text, *a: dict(sources=[], status='searched', message='', queries=1,
                                                               query_texts=['household budgets']))
    monkeypatch.setattr(websearch.fulltext, 'make_fetcher', local_fetcher)
    web.RATE.clear(); web.results.clear()
    manuscript = 'My own introduction to the topic of this essay. ' + COPIED + 'My own conclusion follows here.'
    data = TestClient(web.app).post('/api/check', data={'mode': 'academic', 'language': 'en', 'consent': 'true', 'web_search': 'true'},
                                    files={'submission': ('m.txt', manuscript.encode())}).json()
    assert data['web']['providers'] == ['Brave'] and data['web']['compared'] == 1 and data['percent'] > 50
    assert 'Brave' in data['scope'] and data['sources'][0]['coverage'] == 'web'
    assert TestClient(web.app).get('/health').json()['web_providers'] == ['Brave']


def test_excerpts_spread_over_the_whole_document(monkeypatch):
    import discovery
    for name in ('WEB_MAX_QUERIES', 'WEB_WORDS_PER_EXCERPT'):
        monkeypatch.delenv(name, raising=False)
    assert websearch.excerpt_count(400) == 3 and websearch.excerpt_count(5000) == 10
    assert websearch.excerpt_count(60000) == 20
    monkeypatch.setenv('WEB_MAX_QUERIES', '8')
    assert websearch.excerpt_count(60000) == 8
    sections = [f"Section {i}. The distinctive phrase number{i} describes irrigation reforms affecting cotton "
                f"cooperatives across Khorezm province during reconstruction period{i}. " * 3 for i in range(10)]
    text = "Water policy in Khorezm\n" + " ".join(sections) + "\nReferences\n1. Karimov A. Irrigation number99 policy and reconstruction."
    excerpts = discovery.spread_excerpts(text, 10)
    assert excerpts[0] == "Water policy in Khorezm" and len(excerpts) == 10
    covered = {i for i in range(10) if any(f"number{i} " in e for e in excerpts)}
    assert len(covered) == 9 and {0, 9} <= covered and not any("number99" in e for e in excerpts)  # start to end; no bibliography
    assert all(len(e.split()) <= 14 for e in excerpts)


def test_pages_found_by_several_excerpts_come_first_and_retries_are_limited(monkeypatch):
    calls = []

    def fake(query, language):
        calls.append(query)
        plain = query.strip('"')
        return {'"a"': [('https://x.uz/1', 'one')], '"b"': [('https://x.uz/2', 'two'), ('https://x.uz/3', 'three')],
                '"c"': [('https://x.uz/3', 'three')], '"d"': [('https://x.uz/3/', 'three')]}.get(query, []) if query.startswith('"') else []
    monkeypatch.setattr(websearch, '_yandex', fake)
    monkeypatch.setenv('YANDEX_API_KEY', 'k'); monkeypatch.setenv('YANDEX_FOLDER_ID', 'f')
    monkeypatch.delenv('BRAVE_API_KEY', raising=False); monkeypatch.delenv('WEB_SEARCH', raising=False)
    found, searches, errors, used = websearch.search(['a', 'b', 'c', 'd', 'e', 'f'], 'ru')
    assert [u for u, _ in found] == ['https://x.uz/3', 'https://x.uz/1', 'https://x.uz/2']   # x.uz/3 found 3 times
    assert calls == ['"a"', '"b"', '"c"', '"d"', '"e"', '"f"'] and searches == 6           # no unquoted retry needed/allowed
    calls.clear()
    websearch.search(['zz1', 'zz2', 'zz3', 'zz4'], 'ru')
    assert calls == ['"zz1"', 'zz1', '"zz2"', 'zz2', '"zz3"', 'zz3', '"zz4"']             # retry only for the first 3
