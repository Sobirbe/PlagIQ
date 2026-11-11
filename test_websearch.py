"""Internet search module (Yandex Search API v2) against a local simulated API and web site."""
import base64
import http.server
import json
import threading

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
    calls = []

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
    monkeypatch.delenv('WEB_SEARCH', raising=False)
    yield base, calls
    server.shutdown()


def local_fetcher():
    return Fetcher(delay=0, allow_http_hosts={'127.0.0.1'})


def test_settings_require_key_and_folder(monkeypatch):
    for name in ('YANDEX_API_KEY', 'YANDEX_FOLDER_ID', 'WEB_SEARCH'):
        monkeypatch.delenv(name, raising=False)
    assert websearch.settings() == (False, 6, 20)
    monkeypatch.setenv('YANDEX_API_KEY', 'k')
    assert websearch.settings()[0] is False                       # folder ID missing
    monkeypatch.setenv('YANDEX_FOLDER_ID', 'f')
    monkeypatch.setenv('WEB_MAX_PAGES', '99'); monkeypatch.setenv('WEB_SECONDS', '1')
    assert websearch.settings() == (True, 15, 5)
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
    found, searches, errors = websearch.search(['household budgets Fergana valley'], 'ru')
    assert searches == 2 and not errors
    assert [u for u, _ in found] == [base + '/en', base + '/ru', base + '/short']    # '#top' duplicate removed
    auth, body = calls[0]
    assert auth == 'Api-Key test-key' and body['folderId'] == 'b1gtestfolder'
    assert body['query']['queryText'] == '"household budgets Fergana valley"'
    assert body['query']['searchType'] == 'SEARCH_TYPE_RU' and calls[1][1]['query']['queryText'] == 'household budgets Fergana valley'
    _, _, errors = websearch.search(['broken query'], 'en')
    assert errors == ['search refused (403)']


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
    top = data['sources'][0]
    assert top['coverage'] == 'web' and top['url'] == base + '/en' and top['passages']
    assert 'Yandex' in data['scope']
    assert 'Background on household economics' not in repr(web.results[data['report_id']])
    assert TestClient(web.app).get('/health').json()['web_search'] is True
