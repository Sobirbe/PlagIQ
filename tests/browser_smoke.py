"""Synthetic-only browser regression; starts isolated frontend and private engine."""
import os
from pathlib import Path
import secrets
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start
from corpus import connect, ingest
from playwright.sync_api import sync_playwright

SAMPLES = {
 'en':'one two three four five six seven eight nine',
 'uz':"Oliy ta'lim muassasalarida sifat muhim va dolzarb masala",
 'ru':'Научный текст содержит семь слов для проверки совпадений',
}


with tempfile.TemporaryDirectory() as tmp:
    env = {**os.environ, 'ENGINE_API_KEY':secrets.token_hex(32), 'ENGINE_URL':'http://127.0.0.1:8124',
           'CORPUS_DB':tmp+'/corpus.sqlite3'}
    for key in ('PRIVATE_PILOT_USER','PRIVATE_PILOT_PASSWORD','PUBLIC_ORIGIN'):
        env.pop(key,None)
    with connect(env['CORPUS_DB']) as db:
        for lang,text in SAMPLES.items():
            ingest(db,dict(title='Synthetic '+lang, url='https://example.org/'+lang, language=lang,
                           authors='Test author', year=2026, rights_basis='written_permission',
                           permission_reference='Synthetic fixture owned by test author'),text)
    engine=start('engine_service',8124,env)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),
                                      headless=True, args=['--no-sandbox'])
            for width in (320,390,768,1440):
                web=start('app',8123,env)
                try:
                    for lang,text in SAMPLES.items():
                        context=browser.new_context(viewport={'width':width,'height':900})
                        page=context.new_page()
                        errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto('http://127.0.0.1:8123')
                        page.locator(f'[data-lang={lang}]').click()
                        assert page.locator('html').get_attribute('lang') == lang
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width,lang,'overflow')
                        page.locator('#language').select_option(lang)
                        page.locator('#submission').set_input_files({'name':'fixture.txt','mimeType':'text/plain','buffer':text.encode()})
                        page.locator('[name=mode][value=reference]').check()
                        page.locator('#reference_text').fill(text)
                        page.locator('#submit').click()
                        page.wait_for_function("() => document.querySelector('#score').textContent==='100%'")
                        assert page.locator('#consent').is_visible() is False
                        # Browser-owned PDF is downloadable, including Cyrillic text.
                        with page.expect_download() as download_info:
                            page.locator('#report').click()
                        download=download_info.value
                        download.save_as(tmp+'/report.pdf')
                        import pymupdf as fitz
                        with fitz.open(tmp+'/report.pdf') as pdf:
                            assert 'PlagIQ' in ''.join(pg.get_text() for pg in pdf)
                        page.locator('[name=mode][value=corpus]').check()
                        assert not page.locator('#consent').is_checked()
                        assert 'OpenAlex' not in page.locator('#consent-label').inner_text()
                        page.locator('#consent').check()
                        page.locator('#submit').click()
                        page.wait_for_function("() => document.querySelector('#source-list').textContent.includes('Synthetic')")
                        assert page.locator('#score').inner_text() == '100%'
                        assert page.locator('blockquote').count() > 0
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width,lang,'result overflow')
                        page.locator('[data-lang=en]').click()
                        assert 'Only authorized' in page.locator('#scope').inner_text()
                        page.locator('#delete-report').click()
                        page.wait_for_function("() => getComputedStyle(document.querySelector('#result')).display==='none'")
                        page.locator('[name=mode][value=academic]').check()
                        assert not page.locator('#consent').is_checked()
                        assert 'OpenAlex' in page.locator('#consent-label').inner_text()
                        page.route('**/api/check', lambda route: route.fulfill(json={
                            'report_id':'synthetic', 'percent':None, 'matched_tokens':0, 'sources':[],
                            'mode':'academic', 'database_status':'unavailable', 'self_comparison':False}))
                        page.locator('#consent').check()
                        page.locator('#submit').click()
                        page.wait_for_function("() => document.querySelector('#score').textContent==='Not evaluated'")
                        assert 'Unavailable' in page.locator('#scope').inner_text()
                        assert not errors, errors
                        context.close()
                    print(f'PASS {width}px: UZ/RU/EN reference, private corpus, consent, PDF, delete, unavailable discovery, no overflow/JS errors')
                finally:
                    web.terminate();web.wait(timeout=10)
            browser.close()
    finally:
        engine.terminate();engine.wait(timeout=10)
