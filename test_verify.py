"""QR verification: signed links, the verify page and the QR code in the PDF."""
import re

import pymupdf as fitz
from fastapi.testclient import TestClient

import app as web
import verify

SECRET = 'v' * 40
TEXT = 'one two three four five six seven eight nine'


def make_report(client, monkeypatch, secret=SECRET):
    if secret:
        monkeypatch.setenv('VERIFY_SECRET', secret)
    else:
        monkeypatch.delenv('VERIFY_SECRET', raising=False)
    web.RATE.clear(); web.results.clear()
    data = client.post('/api/check', data={'mode': 'reference', 'language': 'en', 'reference_text': TEXT},
                       files={'submission': ('m.txt', TEXT.encode())}).json()
    return data


def test_signed_link_round_trip_and_tampering(monkeypatch):
    monkeypatch.setenv('VERIFY_SECRET', SECRET)
    result = {'mode': 'academic', 'percent': 12.5, 'date': '2026-11-11T05:29:22+00:00', 'sha256': 'ab' * 32,
              'total_tokens': 3740, 'language': 'uz', 'fulltext': {'compared': 3},
              'sources': [{'coverage': 'abstract'}] * 5 + [{'coverage': 'metadata_only'}]}
    url = verify.link(result, 'f' * 32, 'https://plagiq.example')
    params = dict(re.findall(r'[?&](\w)=([^&]+)', url))
    assert verify.check(params) == (True, {'r': 'f' * 12, 'd': '20261111T0529', 'h': 'ab' * 8, 'm': 'a',
                                           'p': '12.5', 'w': '3740', 'n': '8', 'l': 'uz'})
    for key, value in (('p', '0'), ('n', '9'), ('h', 'cd' * 8)):
        assert verify.check({**params, key: value})[0] is False         # any change breaks the signature
    monkeypatch.setenv('VERIFY_SECRET', 'other-secret-' * 4)
    assert verify.check(params)[0] is False                              # another server's secret


def test_no_link_without_a_long_secret(monkeypatch):
    monkeypatch.setenv('VERIFY_SECRET', 'short')
    assert verify.link({'mode': 'reference'}, 'f' * 32, 'https://x') is None


def test_pdf_has_qr_and_link_and_verify_page_confirms(monkeypatch):
    client = TestClient(web.app)
    data = make_report(client, monkeypatch)
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=uz").content
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        page = doc[0]
        text = ' '.join(''.join(p.get_text() for p in doc).split())
        assert len(page.get_drawings()) > 100                             # QR modules drawn as vector squares
    assert 'Onlayn tekshirish' in text and 'Sertifikatni tekshirish' in text and 'TEKSHIRUV SERTIFIKATI' in text
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        url = next(l['uri'] for l in doc[0].get_links() if '/verify?' in l.get('uri', ''))
    assert data['report_id'][:12] in url and 'm.txt' not in url and TEXT.split()[0] not in url.split('?')[1]
    page_html = client.get(url.replace('http://testserver', ''), headers={'accept-language': 'ru'}).text
    assert 'Действителен' in page_html and '100%' in page_html and data['sha256'][:16] in page_html
    bad = client.get(url.replace('http://testserver', '').replace('p=100', 'p=10')).text
    assert 'Недействителен' in bad or 'Not valid' in bad or 'Haqiqiy emas' in bad


def test_pdf_without_secret_says_not_set_up(monkeypatch):
    client = TestClient(web.app)
    data = make_report(client, monkeypatch, secret=None)
    pdf = client.get(f"/api/report/{data['report_id']}.pdf?full=1&lang=en").content
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        text = ' '.join(''.join(p.get_text() for p in doc).split())
    assert 'Not set up on this server' in text and '/verify?' not in text


def test_verify_page_open_during_private_pilot(monkeypatch):
    monkeypatch.setenv('PRIVATE_PILOT_USER', 'reviewer')
    monkeypatch.setenv('PRIVATE_PILOT_PASSWORD', 'safe-test-password-123')
    monkeypatch.setenv('VERIFY_SECRET', SECRET)
    client = TestClient(web.app)
    assert client.get('/').status_code == 401
    assert client.get('/verify?r=x&ui=en').status_code == 200            # supervisors can scan without the login
    assert 'Not valid' in client.get('/verify?r=x&ui=en').text
