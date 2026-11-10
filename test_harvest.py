"""Harvester against a local simulated OJS journal (no external network)."""
import csv
import http.server
import json
import threading

import pymupdf as fitz
import pytest

import harvest
from corpus import connect, import_manifest, load_manifest, search

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
TEXT = {'uz': 'Олий таълим муассасаларида сифат муҳим ва долзарб масала ҳисобланади',
        'en': 'Open licence research text about assessment quality in regional universities today',
        'ru': 'Научный текст о качестве образования в региональных университетах сегодня'}


def pdf(text):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text, fontfile=FONT, fontname='dv', fontsize=10)
    return doc.tobytes()


def record(n, rights, lang='uz', deleted=False):
    header = f'<header{" status=\"deleted\"" if deleted else ""}><identifier>oai:j:{n}</identifier><datestamp>2025-03-0{n % 9}</datestamp></header>'
    if deleted:
        return f'<record>{header}</record>'
    r = ''.join(f'<dc:rights>{x}</dc:rights>' for x in rights)
    return (f'<record>{header}<metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Article {n}</dc:title><dc:creator>Author {n}</dc:creator>'
            f'<dc:date>2025-01-15</dc:date><dc:language>{lang}</dc:language>'
            f'<dc:identifier>{{base}}/index.php/j/article/view/{n}</dc:identifier>{r}</oai_dc:dc></metadata></record>')


def page(body, token=None):
    t = f'<resumptionToken>{token}</resumptionToken>' if token else '<resumptionToken/>'
    return (f'<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>{body}{t}'
            f'</ListRecords></OAI-PMH>')


CC_BY = 'https://creativecommons.org/licenses/by/4.0/'


@pytest.fixture()
def journal():
    routes = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            key = self.path.split('&from=')[0]
            status, ctype, body = routes.get(key, (404, 'text/plain', b'missing'))
            self.send_response(status)
            if status in (301, 302):
                self.send_header('Location', body.decode())
                body = b''
            self.send_header('Content-Type', ctype)
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    base = f'http://127.0.0.1:{server.server_port}'
    other = f'http://localhost:{server.server_port}'
    article = lambda n, licence=CC_BY, pdf_url=None: (200, 'text/html', (
        f'<html><head><meta name="citation_pdf_url" content="{pdf_url or f"/index.php/j/article/download/{n}/1"}">'
        f'<meta name="DC.Rights" content="{licence}"></head><body><a rel="license" href="{licence}">licence</a>'
        '</body></html>').encode())
    page1 = ''.join([record(1, [CC_BY], 'uz'), record(2, ['CC BY-NC 4.0']), record(3, ['Copyright the authors']),
                     record(4, [CC_BY], 'ru'), record(5, [CC_BY], 'en'), record(6, [], deleted=True),
                     record(7, [CC_BY], 'en')]).replace('{base}', base)
    page2 = record(8, ['CC0'], 'en').replace('{base}', base)
    routes.update({
        '/robots.txt': (200, 'text/plain', b'User-agent: *\nDisallow: /index.php/j/article/view/7\n'),
        '/index.php/j/oai?verb=ListRecords&metadataPrefix=oai_dc': (200, 'text/xml', page(page1, 'tok1').encode()),
        '/index.php/j/oai?verb=ListRecords&resumptionToken=tok1': (200, 'text/xml', page(page2).encode()),
        '/index.php/j/article/view/1': article(1),
        '/index.php/j/article/download/1/1': (302, '', b'/files/1.pdf'),
        '/files/1.pdf': (200, 'application/pdf', pdf(TEXT['uz'])),
        '/index.php/j/article/view/4': article(4, 'https://creativecommons.org/licenses/by-nc/4.0/'),
        '/index.php/j/article/view/5': article(5, pdf_url=other + '/x.pdf'),
        '/index.php/j/article/view/8': article(8, 'https://creativecommons.org/publicdomain/zero/1.0/'),
        '/index.php/j/article/download/8/1': (200, 'application/pdf', pdf(TEXT['en'])),
        '/redirect': (302, '', (other + '/files/1.pdf').encode()),
    })
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield base, routes
    server.shutdown()


def fetcher():
    return harvest.Fetcher(delay=0, allow_http_hosts={'127.0.0.1', 'localhost'})


def test_end_to_end_harvest_review_import(journal, tmp_path):
    base, _ = journal
    rows = harvest.harvest(base + '/index.php/j/oai', tmp_path / 'stage', download=True, fetcher=fetcher())
    status = {r['oai_id']: (r['status'], r['reason']) for r in rows}
    assert status['oai:j:1'][0] == 'downloaded'
    assert status['oai:j:8'][0] == 'downloaded'                                  # second page via resumptionToken
    assert status['oai:j:2'][0] == 'review' and 'non-commercial' in status['oai:j:2'][1]
    assert status['oai:j:3'][0] == 'rejected'
    assert status['oai:j:4'][0] == 'review' and 'different licence' in status['oai:j:4'][1]
    assert status['oai:j:5'][0] == 'review' and 'hosted elsewhere' in status['oai:j:5'][1]
    assert status['oai:j:7'][0] == 'review' and 'robots.txt' in status['oai:j:7'][1]
    assert 'oai:j:6' not in status                                              # deleted record skipped
    manifest = json.loads((tmp_path / 'stage' / 'manifest.json').read_text('utf-8'))
    assert [m['rights_basis'] for m in manifest] == ['open_license', 'public_domain']
    assert all(m['url'].startswith('https://') and 'OAI-PMH record' in m['permission_reference'] for m in manifest)
    with open(tmp_path / 'stage' / 'review.csv', encoding='utf-8-sig') as handle:
        assert len(list(csv.DictReader(handle))) == 7
    # The staged manifest passes the importer's checks and becomes searchable full text.
    assert len(load_manifest(tmp_path / 'stage' / 'manifest.json')) == 2
    with connect(tmp_path / 'corpus.db') as db:
        import_manifest(db, tmp_path / 'stage' / 'manifest.json')
        found = search(db, "Oliy ta'lim muassasalarida sifat muhim va dolzarb masala hisoblanadi", 'uz')
        assert found['sources'][0]['title'] == 'Article 1'
    # Incremental re-run skips what was already harvested.
    again = harvest.harvest(base + '/index.php/j/oai', tmp_path / 'stage', download=True, fetcher=fetcher())
    assert {r['oai_id']: r['status'] for r in again}['oai:j:1'] == 'skipped'
    assert len(json.loads((tmp_path / 'stage' / 'manifest.json').read_text('utf-8'))) == 2


def test_metadata_only_run_downloads_nothing(journal, tmp_path):
    base, _ = journal
    rows = harvest.harvest(base + '/index.php/j/oai', tmp_path / 's', download=False, fetcher=fetcher())
    assert {r['status'] for r in rows} >= {'accepted'} and not list((tmp_path / 's' / 'files').iterdir())
    assert json.loads((tmp_path / 's' / 'manifest.json').read_text()) == []


def test_fetcher_refuses_unsafe_requests(journal):
    base, routes = journal
    f = fetcher()
    with pytest.raises(harvest.HarvestError, match='another host'):
        f.get(base + '/redirect', 1000)
    with pytest.raises(harvest.HarvestError, match='HTTPS required'):
        harvest.Fetcher(delay=0).get('http://example.org/x', 1000)
    with pytest.raises(harvest.HarvestError, match='robots.txt'):
        f.get(base + '/index.php/j/article/view/7', 1000)
    routes['/big'] = (200, 'text/plain', b'x' * 2000)
    with pytest.raises(harvest.HarvestError, match='MB'):
        f.get(base + '/big', 1000)


def test_xml_with_doctype_is_refused():
    with pytest.raises(harvest.HarvestError, match='DOCTYPE'):
        harvest.parse_records(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><OAI-PMH/>')


def test_oai_error_and_no_records():
    ns = 'xmlns="http://www.openarchives.org/OAI/2.0/"'
    assert harvest.parse_records(f'<OAI-PMH {ns}><error code="noRecordsMatch"/></OAI-PMH>'.encode()) == ([], None)
    with pytest.raises(harvest.HarvestError, match='badArgument'):
        harvest.parse_records(f'<OAI-PMH {ns}><error code="badArgument">x</error></OAI-PMH>'.encode())
