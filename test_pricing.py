"""Word-count pricing: counting, tiers, configuration and the quote endpoint."""
import json

from fastapi.testclient import TestClient

import app as web
import pricing


def test_word_count_is_fair_across_languages():
    assert pricing.count_words("Oliy ta'lim o‘qituvchi — sifat!") == 4
    assert pricing.count_words("Научный текст, ещё 2 слова.") == 5
    assert pricing.count_words("don't stop 2026") == 3
    assert pricing.count_words("") == 0


def test_tiers_and_boundaries(monkeypatch):
    monkeypatch.delenv('PRICING_TIERS', raising=False)
    assert pricing.quote(1000)['id'] == 'free' and pricing.quote(1000)['price_uzs'] == 0
    assert pricing.quote(1001)['id'] == 'researcher' and pricing.quote(1001)['price_uzs'] == 15000
    assert pricing.quote(3500)['id'] == 'researcher'
    assert pricing.quote(3501)['id'] == 'extended' and pricing.quote(3501)['price_uzs'] == 25000
    assert pricing.quote(15000)['id'] == 'extended'
    assert pricing.quote(15001)['id'] == 'thesis' and pricing.quote(40001)['id'] == 'phd' and pricing.quote(150001) == {'id': 'contact', 'max_words': None, 'price_uzs': None, 'words': 150001, 'fits': False}


def test_owner_configuration_and_invalid_fallback(monkeypatch):
    monkeypatch.setenv('PRICING_TIERS', json.dumps([{'id': 'big', 'max_words': 9000, 'price_uzs': 9}, {'id': 'small', 'max_words': 100, 'price_uzs': 0}]))
    assert [t['id'] for t in pricing.tiers()] == ['small', 'big']
    for bad in ('not json', '[]', '[{"id":"x","max_words":-1,"price_uzs":0}]', '[{"id":"<b>","max_words":5,"price_uzs":0}]'):
        monkeypatch.setenv('PRICING_TIERS', bad)
        assert pricing.tiers() == pricing.DEFAULT_TIERS


def test_quote_endpoint_counts_words_and_stores_nothing(monkeypatch):
    monkeypatch.delenv('PRICING_TIERS', raising=False)
    web.QUOTES.clear(); web.RATE.clear(); web.results.clear()
    client = TestClient(web.app)
    text = ('word ' * 2500).encode()
    data = client.post('/api/quote', files={'submission': ('a.txt', text)}).json()
    assert data['words'] == 2500 and data['id'] == 'researcher' and data['price_uzs'] == 15000 and data['payments_enabled'] is False
    assert not web.results and not web.RATE                       # no report, no scan allowance used
    assert client.post('/api/quote', files={'submission': ('a.exe', b'x')}).json()['code'] == 'unsupported_type'
    config = client.get('/api/pricing').json()
    assert config['payments_enabled'] is False and config['status'] == 'planned' and len(config['tiers']) == 5


def test_check_result_includes_plan():
    web.RATE.clear()
    text = 'one two three four five six seven eight nine'
    data = TestClient(web.app).post('/api/check', data={'mode': 'reference', 'language': 'en', 'reference_text': text},
                                    files={'submission': ('m.txt', text.encode())}).json()
    assert data['plan']['id'] == 'free' and data['plan']['words'] == 9


def test_other_services_prices(monkeypatch):
    for name in ('AI_PRICE_PER_WORD', 'AI_MIN_PRICE', 'PRICING_PACKAGES'):
        monkeypatch.delenv(name, raising=False)
    assert pricing.ai_price(1000) == 6000 and pricing.ai_price(5000) == 15000          # 3 so'm/word, minimum 6,000
    config = pricing.public_config()
    assert config['ai'] == {'per_word': 3, 'minimum': 6000} and config['certificate'] == 0
    assert config['packages'] == [{'checks': 10, 'bonus': 2}, {'checks': 20, 'bonus': 5}]
    monkeypatch.setenv('AI_PRICE_PER_WORD', '2')
    monkeypatch.setenv('PRICING_PACKAGES', '[{"checks": 5, "bonus": 9}]')                  # invalid: bonus > checks
    assert pricing.ai_price(5000) == 10000 and pricing.packages()[0] == {'checks': 10, 'bonus': 2}
