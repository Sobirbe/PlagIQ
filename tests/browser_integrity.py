"""Browser check: the AI-writing and manipulation indicators panel (reference mode, no network).

Usage: CHROMIUM_PATH=... python tests/browser_integrity.py [screenshot_dir]
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
BODY = ("This study examines how remittance inflows affect rural household budgets in the Fergana valley. "
        "We combine survey data from 2019 with interviews and compare seasonal consumption patterns. ") * 3
TEXT = ("Certainly! Here is a revised version of your introduction. " + BODY.replace("a", "а", 8)
        + " As shown by [Insert citation], prices rose." + " ​" * 12)
env = {k: v for k, v in os.environ.items() if k not in ('ENGINE_URL', 'PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD')}
web = start('app', 8145, env)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
        for width in (390, 1440):
            page = browser.new_context(viewport={'width': width, 'height': 900}, locale='en-US',
                                       device_scale_factor=2 if SHOTS else 1).new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('http://127.0.0.1:8145')
            page.locator('input[name=mode][value=reference]').check()
            page.locator('#submission').set_input_files({'name': 'essay.txt', 'mimeType': 'text/plain', 'buffer': TEXT.encode()})
            page.locator('#reference_text').fill(BODY)
            page.locator('button[type=submit]').click()
            page.wait_for_selector('#integrity h3')
            panel = page.locator('#integrity').inner_text()
            for expected in ('AI-writing and manipulation indicators', 'Review suggested', 'Chatbot phrases left in the text: 1',
                             'Unfilled placeholders: 1', 'Hidden characters', 'Words mixing Latin and Cyrillic',
                             '»Certainly! Here is a revised version«', '⟦U+200B⟧', '⟨а⟩', 'separate from the optional AI detector',
                             '“Find academic publications” mode'):
                assert expected in panel, (expected, panel)
            assert 'AI percentage: not calculated' in page.locator('#scope').inner_text()
            page.locator('[data-lang=uz]').click()
            assert 'Tekshirish tavsiya etiladi' in page.locator('#integrity').inner_text()
            page.locator('[data-lang=ru]').click()
            assert 'Рекомендуется проверка' in page.locator('#integrity').inner_text()
            page.locator('[data-lang=en]').click()
            if SHOTS:
                page.locator('#integrity').scroll_into_view_if_needed()
                page.locator('#integrity').screenshot(path=str(SHOTS / f'integrity-{width}.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            assert not errors, errors
            page.context.close()
            print(f'PASS {width}px: indicators panel with examples, UZ/RU/EN, no overflow/JS errors')
        browser.close()
finally:
    web.terminate(); web.wait(timeout=10)
