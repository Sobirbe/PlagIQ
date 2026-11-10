"""OpenAlex/CORE staging against local simulated services (no external network)."""
import argparse
import http.server
import json
import threading
from urllib.parse import parse_qs, urlsplit

import pymupdf as fitz
import pytest

import harvest
import oa_import
from corpus import connect, import_manifest, search

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
UZ = 'Олий таълим муассасаларида сифат муҳим ва долзарб масала ҳисобланади'
RU = 'Научный текст о качестве образования в региональных университетах сегодня'


def pdf(text):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text, fontfile=FONT, fontname='dv', fontsize=10)
    return doc.tobytes()


def work(n, licence, language='uz', pdf_url=None, doi=True):
    return {'id': f'https://openalex.org/W{n}', 'doi': f'https://doi.org/10.1/{n}' if doi else None,
            'display_name': f'Work {n}', 'publication_year': 2024, 'language': language,
            'authorships': [{'author': {'display_name': f'Author {n}'}}],
            'best_oa_location': {'license': licence, 'pdf_url': pdf_url, 'landing_page_url': f'https://example.org/{n}'}}


@pytest.fixture()
def services():
    seen = {'headers': [], 'paths': []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            seen['paths'].append(self.path)
            seen['headers'].append(dict(self.headers))
            status, ctype, body = 404, 'text/plain', b'missing'
            if url.path == '/oa/works' and q.get('cursor') == '*':
                body = json.dumps({'meta': {'next_cursor': 'c2'}, 'results': [
                    work(1, 'cc-by', 'uz', base + '/pub/1.pdf'), work(2, 'cc-by-nc', 'ru'), work(3, None, 'en'),
                    work(4, 'cc-by', 'kk', base + '/pub/1.pdf'), work(5, 'cc-by', 'en', base + '/pub/page.html'),
                    work(8, 'cc-by', 'uz', base + '/pub/1.pdf')]})
            elif url.path == '/oa/works' and q.get('cursor') == 'c2':
                body = json.dumps({'meta': {'next_cursor': None}, 'results': [work(7, 'cc-by-sa', 'ru', base + '/pub/redir')]})
            elif url.path.startswith('/oa/works/doi:'):
                doi = url.path.split('doi:')[1]
                tail = doi.rsplit('/', 1)[-1]
                licence = 'cc-by-nd' if 'nd' in tail else 'cc-by-nc' if 'nc' in tail else 'cc-by'
                body = json.dumps(work(int(''.join(c for c in tail if c.isdigit())) + (19 if 'nd' in tail else 0), licence, 'ru'))
            elif url.path == '/core/search/works':                         # live API redirects without slash
                status, ctype, body = 301, 'text/plain', b''
            elif url.path == '/core/search/works/':
                long_ru = (RU + ' ') * 12
                prov = lambda i, n: [{'id': i, 'name': n}]
                core = lambda i, doi, text=long_ru, provider=prov(1, 'Other'): {
                    'id': i, 'title': f'Core {i}', 'doi': doi, 'fullText': text, 'yearPublished': 2023,
                    'language': {'code': 'ru', 'name': 'Russian'}, 'authors': [{'name': 'Автор'}],
                    'downloadUrl': f'https://core.ac.uk/download/{i}.pdf', 'dataProviders': provider}
                body = json.dumps({'results': [] if q.get('offset') != '0' else [
                    core(11, '10.1/11'), core(12, None), core(13, '10.1/nd13'),
                    core(14, None, provider=prov(14935, 'Uzbekistan Research Online')),
                    core(15, None, text='junk', provider=prov(14935, 'Uzbekistan Research Online')),
                    core(16, '10.1/nc16', provider=prov(14935, 'Uzbekistan Research Online'))]})
            elif url.path.startswith('/upw/'):
                body = json.dumps({'best_oa_location': {'license': {'8': 'cc-by-nc', '7': 'cc-by-sa'}.get(url.path.rsplit('/', 1)[-1], 'cc-by')}})
            elif url.path == '/pub/1.pdf':
                status, ctype, body = 200, 'application/pdf', pdf(UZ)
            elif url.path == '/pub/2.pdf':
                status, ctype, body = 200, 'application/pdf', pdf(RU)
            elif url.path == '/pub/redir':
                self.send_response(302)
                self.send_header('Location', other + '/pub/2.pdf')
                self.end_headers()
                return
            elif url.path == '/pub/page.html':
                status, ctype, body = 200, 'text/html', b'<html>landing page</html>'
            elif url.path == '/content/works/W6.pdf':
                status, ctype, body = 200, 'application/pdf', pdf(UZ)
            if isinstance(body, str):
                status, ctype, body = 200, 'application/json', body.encode()
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    base = f'http://127.0.0.1:{server.server_port}'
    other = f'http://localhost:{server.server_port}'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield base, seen
    server.shutdown()


def args(**kw):
    defaults = dict(language=None, country=None, since=None, source_id=None, query=None, download=True,
                    pdf_from='publisher', unpaywall_email=None, max_records=50, trusted_provider=None,
                    trusted_note=None, trusted_basis='open_license')
    return argparse.Namespace(**{**defaults, **kw})


def fetcher():
    return harvest.Fetcher(delay=0, allow_http_hosts={'127.0.0.1', 'localhost'})


def test_openalex_staging_end_to_end(services, tmp_path, monkeypatch):
    base, seen = services
    monkeypatch.setenv('OPENALEX_API_KEY', 'test-key')
    monkeypatch.setattr(oa_import, 'UNPAYWALL', base + '/upw')
    stage, budget = oa_import.Stage(tmp_path / 's'), oa_import.Budget(0.5)
    oa_import.run_openalex(args(unpaywall_email='owner@example.org'), fetcher(), budget, stage, api=base + '/oa')
    stage.save(budget)
    status = {r['id']: (r['status'], r['reason']) for r in stage.rows}
    assert status['W1'][0] == 'downloaded' and status['W7'][0] == 'downloaded'   # page 2, cross-host redirect
    assert status['W2'][0] == 'review' and 'non-commercial' in status['W2'][1]
    assert status['W3'][0] == 'rejected'
    assert status['W4'][0] == 'review' and 'uz/ru/en' in status['W4'][1]
    assert status['W5'][0] == 'review' and 'not a PDF' in status['W5'][1]
    assert status['W8'][0] == 'review' and 'Unpaywall states cc-by-nc' in status['W8'][1]
    assert 'non-commercial' not in status['W8'][1]                               # reason names the real cause
    first = [p for p in seen['paths'] if p.startswith('/oa/works?')][0]
    assert 'best_oa_location.license%3Acc-by%7Ccc-by-sa' in first and 'api_key' not in first
    assert any(h.get('Authorization') == 'Bearer test-key' for h in seen['headers'])
    assert json.loads((tmp_path / 's' / 'cost.json').read_text())['calls'] == {'list': 2}
    with connect(tmp_path / 'c.db') as db:
        import_manifest(db, tmp_path / 's' / 'manifest.json')
        assert search(db, "Oliy ta'lim muassasalarida sifat muhim va dolzarb masala hisoblanadi", 'uz')['sources'][0]['title'] == 'Work 1'
        assert search(db, RU, 'ru')['sources'][0]['title'] == 'Work 7'
    # Incremental run skips staged works.
    stage2 = oa_import.Stage(tmp_path / 's')
    oa_import.run_openalex(args(), fetcher(), oa_import.Budget(0.5), stage2, api=base + '/oa')
    assert {r['id']: r['status'] for r in stage2.rows}['W1'] == 'skipped'


def test_cost_limit_stops_and_keeps_progress(services, tmp_path):
    base, _ = services
    stage, budget = oa_import.Stage(tmp_path / 's'), oa_import.Budget(0.00015)
    with pytest.raises(harvest.HarvestError, match='cost limit'):
        oa_import.run_openalex(args(), fetcher(), budget, stage, api=base + '/oa')
    stage.save(budget)
    assert len(json.loads((tmp_path / 's' / 'manifest.json').read_text())) >= 1   # page 1 was staged


def test_openalex_cached_pdf_is_billed(services, tmp_path):
    base, seen = services
    stage, budget = oa_import.Stage(tmp_path / 's'), oa_import.Budget(0.5)
    oa_import.stage_work(work(6, 'cc0', 'uz'), args(pdf_from='openalex'), fetcher(), budget, stage, '', base + '/content')
    assert stage.rows[0]['status'] == 'downloaded' and budget.calls == {'content': 1}
    assert stage.manifest[0]['rights_basis'] == 'public_domain'


def test_core_texts_need_verified_licence(services, tmp_path, monkeypatch):
    base, seen = services
    monkeypatch.setenv('CORE_API_KEY', 'core-key')
    stage, budget = oa_import.Stage(tmp_path / 's'), oa_import.Budget(0.5)
    oa_import.run_core(args(query='language.code:ru'), fetcher(), budget, stage, api=base + '/core', openalex=base + '/oa')
    status = {r['title']: r['status'] for r in stage.rows}
    assert status == {'Work 11': 'staged', 'Core 12': 'rejected', 'Work 32': 'review', 'Core 14': 'rejected',
                      'Core 15': 'rejected', 'Work 16': 'review'}
    assert any(h.get('Authorization') == 'Bearer core-key' for h in seen['headers'])
    assert stage.manifest[0]['text_file'].endswith('.txt') and stage.manifest[0]['rights_basis'] == 'open_license'


def test_core_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv('CORE_API_KEY', raising=False)
    with pytest.raises(harvest.HarvestError, match='CORE_API_KEY'):
        oa_import.run_core(args(), fetcher(), oa_import.Budget(1), oa_import.Stage(tmp_path))


def test_publisher_links_to_internal_hosts_are_refused():
    with pytest.raises(harvest.HarvestError, match='non-public'):
        harvest.Fetcher(delay=0).get('https://127.0.0.1/x.pdf', 1000, cross_host=True)
    with pytest.raises(harvest.HarvestError, match='non-public'):
        harvest.Fetcher(delay=0).get('https://localhost/x.pdf', 1000, cross_host=True)


def test_mislabelled_uzbek_cyrillic_is_corrected(tmp_path):
    stage = oa_import.Stage(tmp_path / 's')
    w = work(40, 'cc-by', 'ru')
    w['display_name'] = 'Aйблaнувчининг қaеpдa экaнлиги нoмaълум бўлгaн ҳoллapдa'
    oa_import.stage_work(w, args(download=False), fetcher(), oa_import.Budget(1), stage, '', '')
    assert stage.rows[0]['language'] == 'uz' and 'corrected to uz' in stage.rows[0]['reason']
    assert oa_import.correct_language('ru', 'Научный текст') is None
    assert oa_import.correct_language('uz', 'Высшее образование') == 'ru'


def test_core_trusted_repository_policy(services, tmp_path, monkeypatch):
    base, _ = services
    monkeypatch.setenv('CORE_API_KEY', 'core-key')
    with pytest.raises(harvest.HarvestError, match='trusted-note'):
        oa_import.run_core(args(trusted_provider=[14935]), fetcher(), oa_import.Budget(1), oa_import.Stage(tmp_path / 'x'),
                           api=base + '/core', openalex=base + '/oa')
    stage = oa_import.Stage(tmp_path / 's')
    oa_import.run_core(args(trusted_provider=[14935], trusted_note='CC BY policy page checked 2026-11-10'), fetcher(),
                       oa_import.Budget(1), stage, api=base + '/core', openalex=base + '/oa')
    status = {r['title']: (r['status'], r['licence']) for r in stage.rows}
    assert status['Core 14'] == ('staged', 'repository policy')              # no DOI, trusted repository
    assert status['Core 15'][0] == 'rejected'                                # junk full text
    assert status['Work 16'][0] == 'review'                                  # explicit NC licence beats trust
    entry = next(m for m in stage.manifest if m['title'] == 'Core 14')
    assert 'Uzbekistan Research Online' in entry['permission_reference'] and 'checked 2026-11-10' in entry['permission_reference']
    assert entry['language'] == 'ru' and entry['url'].startswith('https://')
