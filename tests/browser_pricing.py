"""Browser check: instant word-count quote, plan cards, calculator, FAQ, translations.

Usage: CHROMIUM_PATH=... python tests/browser_pricing.py [screenshot_dir]
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
env = {k: v for k, v in os.environ.items() if k not in ('ENGINE_URL', 'PRICING_TIERS', 'PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD')}
web = start('app', 8143, env)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
        for width in (320, 390, 768, 1440):
            page = browser.new_context(viewport={'width': width, 'height': 900}, locale='en-US',
                                       device_scale_factor=2 if SHOTS else 1).new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('http://127.0.0.1:8143')
            page.wait_for_function("() => document.querySelectorAll('#price-cards .price').length === 3")
            cards = page.locator('#price-cards').inner_text()
            assert 'Free' in cards and '15,000' in cards and '45,000' in cards and 'Up to 1,000 words' in cards and 'Up to 3,500 words' in cards, cards
            assert 'not charged' in page.locator('#pilot-bar').inner_text()
            page.locator('#calc-words').fill('1001')
            assert 'Researcher' in page.locator('#calc-out').inner_text() and 'free during the pilot' in page.locator('#calc-out').inner_text()
            page.locator('#calc-words').fill('15001')
            assert 'contact us' in page.locator('#calc-out').inner_text()
            page.locator('#submission').set_input_files({'name': 'paper.txt', 'mimeType': 'text/plain', 'buffer': ('word ' * 2500).encode()})
            page.wait_for_function("() => document.querySelector('#quote').textContent.includes('2,500')")
            assert 'Researcher' in page.locator('#quote').inner_text() and 'not stored' in page.locator('#quote').inner_text()
            page.locator('#faq summary').first.click()
            assert 'never added' in page.locator('#faq details').first.inner_text()
            page.locator('[data-lang=uz]').click()
            assert 'Tadqiqotchi' in page.locator('#quote').inner_text() and 'so‘m' in page.locator('#price-cards').inner_text()
            assert 'Qanday ishlaydi' in page.locator('#how-title').inner_text() and page.locator('#faq details').first.get_attribute('open') is not None
            page.locator('[data-lang=ru]').click()
            assert 'Частые вопросы' in page.locator('#faq-title').inner_text() and 'сум' in page.locator('#price-cards').inner_text()
            page.locator('#submission').set_input_files({'name': 'x.exe', 'mimeType': 'application/octet-stream', 'buffer': b'x'})
            assert 'DOC, DOCX' in page.locator('#quote').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            if SHOTS and width in (390, 1440):
                page.locator('[data-lang=uz]').click()
                page.locator('#submission').set_input_files({'name': 'maqola.txt', 'mimeType': 'text/plain', 'buffer': ('so‘z ' * 6911).encode()})
                page.wait_for_function("() => document.querySelector('#quote').textContent.includes('6')")
                page.screenshot(path=str(SHOTS / f'site-uz-{width}.png'), full_page=True)
            assert not errors, errors
            page.context.close()
            print(f'PASS {width}px: quote, plan cards, calculator, FAQ, UZ/RU/EN, no overflow/JS errors')
        browser.close()
finally:
    web.terminate(); web.wait(timeout=10)
