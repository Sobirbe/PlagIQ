"""Browser check: protected archive with the real engine. A signed-in author deposits a thesis;
another user's copy is then found, labelled as an archived work, with the source text hidden.

Usage: CHROMIUM_PATH=... python tests/browser_archive.py [screenshot_dir]
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
TOKEN = "123456789:AAtest-token-for-plagiq-tests_0123456789"
THESIS = ("Magistrlik dissertatsiyasi Farg‘ona vodiysidagi suv resurslarini boshqarish tizimini o‘rganadi va "
          "fermer xo‘jaliklarining sug‘orish samaradorligini oshirish yo‘llarini tahlil qiladi. ") * 8
COPY = "Kirish qismi boshqacha yozilgan. " + THESIS[:400] + " Xulosa ham mustaqil yozilgan."


def popup_for(user_id, name):
    def handler(route):
        data = {"id": user_id, "first_name": name, "auth_date": int(time.time())}
        check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
        message = json.dumps({"event": "auth_result", "result": data})
        route.fulfill(status=200, content_type="text/html",
                      body=f"<script>window.opener.postMessage({json.dumps(message)}, '*'); window.close();</script>")
    return handler


tmp = tempfile.mkdtemp()
base = {k: v for k, v in os.environ.items() if k not in ('PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD')}
key = "k" * 40
engine = start('engine_service', 8148, {**base, "ENGINE_API_KEY": key, "CORPUS_DB": tmp + "/index.db"})
web = start('app', 8149, {**base, "ENGINE_API_KEY": key, "ENGINE_URL": "http://127.0.0.1:8148", "TELEGRAM_BOT_TOKEN": TOKEN,
                          "TELEGRAM_BOT_USERNAME": "plagiq_bot", "USERS_DB": tmp + "/users.db", "FULLTEXT_FETCH": "0",
                          "OPENALEX_API_URL": "http://127.0.0.1:9/none"})
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])

        def session(user_id, name, width):
            ctx = browser.new_context(viewport={'width': width, 'height': 900}, locale='en-US', device_scale_factor=2 if SHOTS else 1)
            ctx.route('https://oauth.telegram.org/**', popup_for(user_id, name))
            page = ctx.new_page()
            page.goto('http://127.0.0.1:8149')
            page.locator('[data-lang=en]').click()
            page.wait_for_selector('#account button.tg')
            with page.expect_popup():
                page.locator('#account button.tg').click()
            page.wait_for_selector('#account summary')
            return page

        def run(page, name, text, mode='reference'):
            page.locator(f'input[name=mode][value={mode}]').check()
            page.locator('#submission').set_input_files({'name': name, 'mimeType': 'text/plain', 'buffer': text.encode()})
            if mode == 'reference':
                page.locator('#reference_text').fill('Bu boshqa mavzudagi qisqa matn bo‘lib, hech narsa bilan mos kelmaydi va faqat sinov uchun.')

        for width in (390, 1440):
            author = session(1000 + width, "Dilnoza", width)
            errors = []
            author.on('pageerror', lambda e: errors.append(str(e)))
            run(author, 'Suv_resurslari.txt', THESIS)
            assert author.locator('#archive-note').is_visible()
            author.locator('#archive').check()
            assert author.locator('#archive_title').input_value() == 'Suv_resurslari'
            author.locator('#archive_title').fill('Suv resurslarini boshqarish')
            author.locator('button[type=submit]').click()
            assert 'confirm the archive consent' in author.locator('#status').inner_text()
            author.locator('#archive_consent').check()
            author.locator('button[type=submit]').click()
            author.wait_for_function("() => document.body.innerText.includes('Added to the protected archive')")
            author.locator('#account summary').click()
            menu = author.locator('#account .menu').inner_text()
            assert 'My archived works' in menu and 'Suv resurslarini boshqarish' in menu, menu
            author.locator('#account summary').click()

            student = session(2000 + width, "Aziz", width)
            student.on('pageerror', lambda e: errors.append(str(e)))
            run(student, 'kurs_ishi.txt', COPY, mode='corpus')
            student.locator('#consent').check()
            student.locator('button[type=submit]').click()
            student.wait_for_selector('#source-list .source-item')
            item = student.locator('#source-list .source-item').first.inner_text()
            for expected in ('Suv resurslarini boshqarish', 'Dilnoza', 'PlagIQ archive (unpublished; text not shown)',
                             'Text not shown: unpublished work in the PlagIQ archive', 'Author’s deposit'):
                assert expected in item, (expected, item)
            assert 'Magistrlik dissertatsiyasi' not in item                       # source text never shown
            if SHOTS:
                student.locator('#result').screenshot(path=str(SHOTS / f'archive-match-{width}.png'))
            # the author removes the work; the copy is no longer found
            author.locator('#account summary').click()
            author.once('dialog', lambda d: d.accept())
            author.locator('#account .menu button', has_text='Remove').click()
            author.wait_for_function("() => document.querySelector('#account .menu') && document.querySelector('#account .menu').textContent.includes('No archived works')")
            student.locator('button[type=submit]').click()
            student.wait_for_function("() => !document.querySelector('#result').hasAttribute('aria-busy')")
            assert 'Suv resurslarini boshqarish' not in student.locator('#source-list').inner_text()
            for page in (author, student):
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            assert not errors, errors
            author.context.close(); student.context.close()
            print(f'PASS {width}px: deposit with consent, copy found and labelled, text hidden, removal')
        browser.close()
finally:
    web.terminate(); web.wait(timeout=10); engine.terminate(); engine.wait(timeout=10)
