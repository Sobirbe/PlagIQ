"""Browser check: matches coloured by source, "show only this source", side-by-side passages.

Usage: CHROMIUM_PATH=... python tests/browser_report.py [screenshot_dir]
The check result is a fixed synthetic response (no network).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
A = "Remittance inflows reshape rural household budgets across the Fergana valley through seasonal consumption smoothing"
B = "Cotton cooperatives in Khorezm reorganised their irrigation schedules after the water reform"
HL = (f'My introduction. <mark class="m" data-s="0">{A}</mark>. My own middle part follows here with more words. '
      f'<mark class="m" data-s="1">{B}</mark>. My conclusion.')
RESULT = {"report_id": "b" * 32, "mode": "academic", "language": "en", "percent": 58.3, "total_tokens": 48, "matched_tokens": 28,
          "matched_chars": 200, "source_count": 2, "matched_source_count": 2, "evaluated_sources": 2,
          "database_status": "searched", "database_message": "", "queries": 1, "reference": "None", "filename": "paper.txt",
          "fulltext": {"attempted": 0, "compared": 0}, "highlight_html": HL, "scope": "test", "ai_detection": "Not performed",
          "sources": [
              {"title": "Household economics in the Fergana valley", "url": "https://doi.org/10.1/a", "year": 2021,
               "coverage": "fulltext", "matched_tokens": 15,
               "passages": [{"query_start": 17, "query_end": 133, "source_start": 900, "source_end": 1016,
                             "query_excerpt": A, "source_excerpt": A}]},
              {"title": "Irrigation reform in Khorezm", "url": "https://doi.org/10.1/b", "year": 2019, "coverage": "abstract",
               "matched_tokens": 13,
               "passages": [{"query_start": 190, "query_end": 282, "source_start": 0, "source_end": 92,
                             "query_excerpt": B, "source_excerpt": B}]}]}

env = {k: v for k, v in os.environ.items() if k not in ('ENGINE_URL', 'TELEGRAM_BOT_TOKEN', 'PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD')}
web = start('app', 8150, env)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
        for width in (390, 1440):
            page = browser.new_context(viewport={'width': width, 'height': 900}, locale='en-US',
                                       device_scale_factor=2 if SHOTS else 1).new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('**/api/check', lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps(RESULT)))
            page.goto('http://127.0.0.1:8150')
            page.locator('[data-lang=en]').click()
            page.locator('#submission').set_input_files({'name': 'paper.txt', 'mimeType': 'text/plain', 'buffer': b'word ' * 40})
            page.locator('#consent').check()
            page.locator('button[type=submit]').click()
            page.wait_for_selector('#source-list .source-item')
            first = page.locator('#source-list .source-item').first
            assert first.locator('.swatch.s0').count() == 1 and page.locator('#source-list .swatch.s1').count() == 1
            pair = first.locator('.pair').inner_text()
            assert 'Your text' in pair and 'Source' in pair and pair.count('Remittance inflows') == 2
            colour = lambda sel: page.eval_on_selector(sel, 'e => getComputedStyle(e).backgroundColor')
            page.locator('#highlight-box summary').click()
            m0, m1 = colour('#highlight-box mark[data-s="0"]'), colour('#highlight-box mark[data-s="1"]')
            assert m0 != m1 and colour('#source-list .swatch.s0') == m0                 # swatch matches the text colour
            page.locator('#source-list .only').nth(1).click()                            # show only source 2
            assert 'dim' in page.locator('#highlight-box mark[data-s="0"]').get_attribute('class')
            assert 'dim' not in (page.locator('#highlight-box mark[data-s="1"]').get_attribute('class') or '')
            page.locator('#hl-all').click()
            assert 'dim' not in page.locator('#highlight-box mark[data-s="0"]').get_attribute('class')
            page.locator('.hl-tools button', has_text='Colour by type').click()
            assert colour('#highlight-box mark[data-s="0"]') == colour('#highlight-box mark[data-s="1"]')   # back to one colour
            page.locator('.hl-tools button', has_text='Colour by source').click()
            page.locator('[data-lang=uz]').click()
            assert 'Sizning matningiz' in page.locator('#source-list').inner_text()
            page.locator('[data-lang=ru]').click()
            assert 'Ваш текст' in page.locator('#source-list').inner_text()
            page.locator('[data-lang=en]').click()
            if page.locator('#highlight-box details').get_attribute('open') is None:
                page.locator('#highlight-box summary').click()
            if SHOTS:
                page.locator('#result').screenshot(path=str(SHOTS / f'report-view-{width}.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            assert not errors, errors
            page.context.close()
            print(f'PASS {width}px: colours by source, matching swatches, show-only filter, type toggle, side-by-side, UZ/RU/EN')
        browser.close()
finally:
    web.terminate(); web.wait(timeout=10)
