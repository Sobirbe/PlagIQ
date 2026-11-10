"""Synthetic browser checks for translated errors, accessibility and evidence display.

Usage: CHROMIUM_PATH=... python tests/browser_followup.py [screenshot_dir]
"""
import io
import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from corpus import connect, ingest  # noqa: E402
from docx import Document  # noqa: E402
import pymupdf as fitz  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
RU = 'Научный текст содержит семь слов для проверки совпадений в корпусе'
UZ = "Oliy ta'lim muassasalarida sifat muhim va dolzarb masala hisoblanadi"
CYR = 'Олий таълим муассасаларида сифат муҳим ва долзарб масала ҳисобланади'
LONG_NAME = 'Магистрлик_диссертацияси_' + 'juda_uzun_fayl_nomi_' * 8 + '.docx'


def docx_bytes(text):
    doc = Document()
    doc.add_paragraph('Kirish. ' + text + ' Xulosa qismi alohida yozilgan.')
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


with tempfile.TemporaryDirectory() as tmp:
    env = {**os.environ, 'ENGINE_API_KEY': secrets.token_hex(32), 'ENGINE_URL': 'http://127.0.0.1:8134',
           'CORPUS_DB': tmp + '/corpus.sqlite3'}
    for key in ('PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD', 'PUBLIC_ORIGIN', 'TRUSTED_PROXY_HOPS'):
        env.pop(key, None)
    with connect(env['CORPUS_DB']) as db:
        ingest(db, dict(title='Oliy ta’limda sifat: sintetik test manbasi ' + 'uzun sarlavha ' * 6,
                        url='https://example.org/uz-' + 'x' * 120, language='uz', authors='Test muallif; Ikkinchi muallif',
                        year=2025, rights_basis='written_permission', permission_reference='Synthetic owner fixture'), CYR)
        ingest(db, dict(title='Синтетический русский источник', url='https://example.org/ru', language='ru',
                        authors='Тестовый автор', year=2024, rights_basis='open_license',
                        permission_reference='Synthetic owner fixture'), RU)
    engine = start('engine_service', 8134, env)
    web = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                        headless=True, args=['--no-sandbox'])
            for width in (320, 390, 768, 1440):
                if web:
                    web.terminate(); web.wait(timeout=10)
                web = start('app', 8133, env)  # fresh process: fresh in-memory rate limit
                ctx = browser.new_context(viewport={'width': width, 'height': 900}, locale='ru-RU',
                                          device_scale_factor=2 if SHOTS else 1, accept_downloads=True)
                page = ctx.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto('http://127.0.0.1:8133')
                # Browser locale picks Russian on first visit.
                assert page.locator('html').get_attribute('lang') == 'ru', 'locale default'
                assert page.locator('[data-lang=ru]').get_attribute('aria-pressed') == 'true'

                # Client-side validation is translated and needs no upload.
                page.locator('#submit').click()
                assert 'Сначала выберите документ' in page.locator('#status').inner_text()
                page.locator('[data-lang=uz]').click()
                assert 'Avval hujjatni tanlang' in page.locator('#status').inner_text(), 'status re-translated'

                # Server error codes are translated.
                page.locator('#submission').set_input_files({'name': 'short.txt', 'mimeType': 'text/plain', 'buffer': b'too short'})
                page.locator('[name=mode][value=reference]').check()
                page.locator('#reference_text').fill(UZ)
                page.locator('#submit').click()
                page.wait_for_function("() => document.querySelector('#status').classList.contains('error')")
                assert 'kamida 7' in page.locator('#status').inner_text(), page.locator('#status').inner_text()
                page.locator('#submission').set_input_files({'name': 'win.txt', 'mimeType': 'text/plain',
                                                             'buffer': RU.encode('cp1251')})
                page.locator('#submit').click()
                page.wait_for_function("() => document.querySelector('#status').textContent.includes('UTF-8')")

                # Keyboard: Tab reaches language buttons and focus is visibly outlined.
                page.locator('body').click(position={'x': 1, 'y': 1})
                page.keyboard.press('Tab')
                outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
                assert outline != 'none', 'visible focus'

                # DOCX with a long Cyrillic filename vs. Uzbek Cyrillic corpus source (cross-script).
                page.locator('#submission').set_input_files({'name': LONG_NAME, 'mimeType':
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': docx_bytes(UZ)})
                page.locator('[name=mode][value=corpus]').check()
                page.locator('#consent').check()
                page.locator('#submit').click()
                page.wait_for_function("() => document.querySelector('#source-list').textContent.includes('sintetik')")
                assert 'UZ 1' in page.locator('#coverage').inner_text()
                assert 'Iqtiboslar' in page.locator('#notice').inner_text()
                assert 'so‘zdan' in page.locator('#details').inner_text(), 'word measure shown'
                assert 'korpus versiyasi' in page.locator('#coverage').inner_text()
                assert page.locator('#highlight-box summary').count() == 1
                page.locator('#highlight-box summary').click()
                assert page.locator('#highlight-box mark').count() >= 1
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
                if SHOTS:
                    page.locator('#result').screenshot(path=str(SHOTS / f'result-uz-{width}.png'))
                # Switch language after analysis: results re-render, PDF link follows.
                page.locator('[data-lang=en]').click()
                assert 'words matched' in page.locator('#details').inner_text()
                assert 'lang=en' in page.locator('#report').get_attribute('href')
                page.locator('[data-lang=ru]').click()
                with page.expect_download() as info:
                    page.locator('#report').click()
                info.value.save_as(tmp + '/r.pdf')
                with fitz.open(tmp + '/r.pdf') as pdf:
                    text = ''.join(pg.get_text() for pg in pdf)
                assert 'Отчёт о совпадениях' in text and 'Письменное разрешение' in text and 'None' not in text
                assert 'Версия корпуса' in text

                # Russian-language corpus check with no Uzbek fallback; English has no sources -> Not evaluated.
                page.locator('#language').select_option('en')
                page.locator('#submission').set_input_files({'name': 'en.txt', 'mimeType': 'text/plain',
                                                             'buffer': b'one two three four five six seven eight'})
                page.locator('#consent').check()
                page.locator('#submit').click()
                page.wait_for_function("() => document.querySelector('#score').textContent==='Не оценено'")
                assert 'нет источников на этом языке' in page.locator('#basis').inner_text()
                if SHOTS and width in (390, 1440):
                    page.locator('#result').screenshot(path=str(SHOTS / f'not-evaluated-ru-{width}.png'))
                page.locator('#delete-report').click()
                page.wait_for_function("() => document.querySelector('#status').textContent.includes('удалён')")
                # Own-reference mode now shows the same passage evidence and highlighting.
                page.locator('#language').select_option('ru')
                page.locator('#submission').set_input_files({'name': 'ru.txt', 'mimeType': 'text/plain',
                                                             'buffer': ('Введение. ' + RU + '. Заключение.').encode()})
                page.locator('[name=mode][value=reference]').check()
                page.locator('#reference_text').fill('Источник: ' + RU)
                page.locator('#submit').click()
                page.wait_for_function("() => document.querySelectorAll('#source-list blockquote').length === 1")
                assert page.locator('#highlight-box summary').count() == 1
                assert page.locator('#source-list').inner_text().count('Ваш источник') == 1
                # Citation options: a quoted, referenced passage excluded on request, disclosed before/after.
                assert 'ПАРАМЕТРЫ РАСЧЁТА' in page.locator('#options-label').inner_text()
                page.locator('#submission').set_input_files({'name': 'q.txt', 'mimeType': 'text/plain',
                                                             'buffer': ('Введение. «' + RU + '» [1]. Заключение.').encode()})
                page.locator('#exclude_quotes').check()
                page.locator('#submit').click()
                page.wait_for_function("() => document.querySelector('#notice').textContent.includes('До исключений')")
                assert page.locator('#score').inner_text() == '0%'
                assert 'В цитате со ссылкой рядом' in page.locator('#breakdown').inner_text()
                page.locator('#highlight-box summary').click()
                assert page.locator('#highlight-box mark.qc.x').count() == 1
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'options overflow')
                if SHOTS and width in (390, 1440):
                    page.locator('#result').screenshot(path=str(SHOTS / f'citation-ru-{width}.png'))
                page.locator('#exclude_quotes').uncheck()
                if SHOTS:
                    page.goto('http://127.0.0.1:8133')
                    page.screenshot(path=str(SHOTS / f'home-ru-{width}.png'), full_page=width >= 768)
                assert not errors, errors
                ctx.close()
                print(f'PASS {width}px: translated errors, focus, DOCX cross-script corpus, coverage, highlight, localized PDF, not-evaluated, delete')
            browser.close()
    finally:
        if web:
            web.terminate(); web.wait(timeout=10)
        engine.terminate(); engine.wait(timeout=10)
