"""Transient full-text comparison during academic checks (local simulated publisher)."""
import http.server
import threading
import time

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

import app as web
import discovery
import fulltext
from harvest import Fetcher

BODY = ("Remittance inflows reshape rural household budgets across the Fergana valley through seasonal consumption "
        "smoothing, investment in housing, and payment of school fees for younger children. ")
ARTICLE = ("Abstract. A short summary of the study. " + "Introduction text about migration research in Central Asia. " * 20
           + BODY + "Further discussion of results and policy implications for regional development. " * 20)


def pdf(text):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=7)
    return doc.tobytes()


@pytest.fixture()
def publisher():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            routes = {'/article.pdf': (200, 'application/pdf', pdf(ARTICLE)),
                      '/content/works/W7.pdf': (200, 'application/pdf', pdf(ARTICLE)),
                      '/landing': (200, 'text/html', b'<html><head><meta name="citation_pdf_url" content="/article.pdf"></head></html>'),
                      '/broken.pdf': (200, 'application/pdf', b'not a pdf'),
                      '/robots.txt': (404, 'text/plain', b'')}
            if self.path == '/slow.pdf':
                time.sleep(3)
                routes[self.path] = (200, 'application/pdf', pdf(ARTICLE))
            status, ctype, body = routes.get(self.path, (404, 'text/plain', b'missing'))
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()


def local_fetcher():
    return Fetcher(delay=0, allow_http_hosts={'127.0.0.1'})


def source(i, base, pdf_urls=(), pages=()):
    return {'title': f'Work {i}', 'url': f'https://doi.org/10.1/{i}', 'year': 2024, 'coverage': 'abstract',
            'abstract': 'A short summary of the study.', 'access': {'openalex_id': f'W{i}', 'pdf_urls': [base + p for p in pdf_urls],
                                                                    'pages': [base + p for p in pages], 'openalex_pdf': False}}


def test_fetch_counts_successes_failures_and_timeouts(publisher):
    sources = [source(1, publisher, ['/article.pdf']), source(2, publisher, pages=['/landing']),
               source(3, publisher, ['/broken.pdf']), source(4, publisher, ['/slow.pdf']), source(5, publisher)]
    stats = fulltext.fetch_fulltexts(sources, max_works=8, seconds=1.5, fetcher_factory=local_fetcher)
    assert stats == {'attempted': 4, 'compared': 2, 'failed': 1, 'timed_out': 1,
                     'reasons': {'not a PDF': 1, 'timed out': 1}}
    assert sources[0]['fulltext_via'] == 'pdf' and sources[1]['fulltext_via'] == 'article page'
    assert BODY.split()[3] in sources[0]['fulltext'] and 'fulltext' not in sources[4]


def test_copying_from_article_body_is_detected_and_not_stored(publisher, monkeypatch):
    sources = [source(1, publisher, ['/article.pdf'])]
    monkeypatch.setattr(web, 'discover', lambda text, *a: dict(sources=[dict(s) for s in sources], status='searched',
                                                               message='', queries=1, query_texts=['q']))
    monkeypatch.setattr(fulltext, 'make_fetcher', local_fetcher)
    monkeypatch.delenv('FULLTEXT_FETCH', raising=False)
    web.RATE.clear(); web.results.clear()
    manuscript = 'My own introduction to the topic of this essay. ' + BODY + 'My own conclusion follows here.'
    data = TestClient(web.app).post('/api/check', data={'mode': 'academic', 'language': 'en', 'consent': 'true'},
                                    files={'submission': ('m.txt', manuscript.encode())}).json()
    assert data['percent'] > 50 and data['fulltext']['compared'] == 1
    assert data['sources'][0]['coverage'] == 'fulltext' and data['sources'][0]['passages']
    cached = repr(web.results[data['report_id']])
    assert 'Introduction text about migration research' not in cached        # article text not kept
    monkeypatch.setenv('FULLTEXT_FETCH', '0')                               # switch off: abstracts only
    web.RATE.clear()
    off = TestClient(web.app).post('/api/check', data={'mode': 'academic', 'language': 'en', 'consent': 'true'},
                                   files={'submission': ('m.txt', manuscript.encode())}).json()
    assert off['percent'] == 0 and off['fulltext']['attempted'] == 0


def test_open_access_links_only_from_open_locations():
    work = {'id': 'https://openalex.org/W9', 'best_oa_location': {'is_oa': True, 'pdf_url': 'https://j.uz/a.pdf',
                                                                  'landing_page_url': 'https://j.uz/a'},
            'locations': [{'is_oa': False, 'pdf_url': 'https://paywall.example/x.pdf'},
                          {'is_oa': True, 'pdf_url': 'https://repo.uz/a.pdf'}, {'is_oa': True, 'pdf_url': 'http://insecure/x.pdf'}],
            'open_access': {'oa_url': 'https://j.uz/a'}, 'has_content': {'pdf': True}}
    links = discovery.open_access_links(work)
    assert links == {'openalex_id': 'W9', 'pdf_urls': ['https://j.uz/a.pdf', 'https://repo.uz/a.pdf'],
                     'pages': ['https://j.uz/a'], 'openalex_pdf': True}


def test_settings_bounded(monkeypatch):
    monkeypatch.setenv('FULLTEXT_MAX', '500')
    monkeypatch.setenv('FULLTEXT_SECONDS', '1')
    monkeypatch.setenv('FULLTEXT_OPENALEX_PDF', '1')
    assert fulltext.settings() == (True, 20, 5, True)


def test_doi_style_pdf_link_leading_to_html_page(publisher):
    """OpenAlex often lists a doi.org link as 'pdf_url'; it leads to the article page, which names the PDF."""
    sources = [source(1, publisher, ['/landing'])]
    stats = fulltext.fetch_fulltexts(sources, seconds=5, fetcher_factory=local_fetcher)
    assert stats['compared'] == 1 and sources[0]['fulltext_via'] == 'pdf'


def test_openalex_copy_first_when_allowed(publisher, monkeypatch):
    s = source(7, publisher, ['/broken.pdf'])
    s['access']['openalex_pdf'] = True
    stats = fulltext.fetch_fulltexts([s], seconds=5, allow_paid=True, content_base=publisher + '/content',
                                     fetcher_factory=local_fetcher)
    assert stats['compared'] == 1 and s['fulltext_via'] == 'OpenAlex copy'


def test_reason_labels():
    from harvest import HarvestError
    assert fulltext.reason(HarvestError('robots.txt disallows x')) == 'blocked by robots.txt'
    assert fulltext.reason(OSError('The read operation timed out')) == 'timed out'
    assert fulltext.reason(Exception('HTTP Error 403: Forbidden')) == 'refused by the website (403)'


def test_language_detected_from_text_and_reported(monkeypatch):
    import pymupdf
    monkeypatch.setattr(web, 'discover', lambda text, *a: dict(sources=[], status='searched', message='', queries=1,
                                                               query_texts=['q'], language_used=a[1] if len(a) > 1 else None))
    calls = []
    real = web.discover
    monkeypatch.setattr(web, 'discover', lambda text, n, lang: calls.append(lang) or real(text, n, lang))
    web.RATE.clear(); web.results.clear()
    english = ('The quality of higher education is assessed with the help of new indicators for the region and the country. ' * 8)
    client = TestClient(web.app)
    data = client.post('/api/check', data={'mode': 'academic', 'language': 'uz', 'consent': 'true'},
                       files={'submission': ('m.txt', english.encode())}).json()
    assert calls == ['en'] and data['language'] == 'en' and data['language_note'] == {'selected': 'uz', 'detected': 'en'}
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=uz").content
    with pymupdf.open(stream=pdf, filetype='pdf') as doc:
        text = ' '.join(''.join(p.get_text() for p in doc).split())
    assert 'EN (matndan aniqlandi; tanlangan: UZ)' in text
