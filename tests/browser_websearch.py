"""Browser check: the Yandex web-search consent box and how web results are shown.

Usage: CHROMIUM_PATH=... python tests/browser_websearch.py [screenshot_dir]
The check result is a fixed synthetic response (no real search is made).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
EXCERPT = 'Remittance inflows reshape rural household budgets across the Fergana valley'
RESULT = {"report_id": "a" * 32, "mode": "academic", "language": "en", "percent": 41.2, "total_tokens": 34, "matched_tokens": 14,
          "source_count": 1, "matched_source_count": 1, "evaluated_sources": 1, "database_status": "searched",
          "database_message": "", "queries": 1, "search_phrases": [EXCERPT], "reference": "None", "filename": "paper.txt",
          "fulltext": {"attempted": 0, "compared": 0, "failed": 0, "timed_out": 0},
          "web": {"enabled": True, "searches": 2, "results": 3, "attempted": 3, "compared": 2, "failed": 1, "timed_out": 0,
                  "reasons": {"too little text on page": 1}, "errors": []},
          "sources": [{"title": "Household economics notes", "url": "https://example.org/notes", "year": None,
                       "coverage": "web", "matched_tokens": 14, "percent": 41.2,
                       "passages": [{"text": EXCERPT, "source_excerpt": EXCERPT, "words": 14}]}],
          "highlight_html": "<mark>" + EXCERPT + "</mark>", "scope": "test", "self_comparison": False,
          "ai_detection": "Not performed"}

env = {k: v for k, v in os.environ.items() if k not in ('ENGINE_URL', 'PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD')}
env.update(YANDEX_API_KEY='dummy-not-used', YANDEX_FOLDER_ID='b1gdummy', YANDEX_SEARCH_URL='http://127.0.0.1:9/none')
web = start('app', 8144, env)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
        for width in (390, 1440):
            page = browser.new_context(viewport={'width': width, 'height': 900}, locale='en-US',
                                       device_scale_factor=2 if SHOTS else 1).new_page()
            errors, posted = [], []
            page.on('pageerror', lambda e: errors.append(str(e)))

            def fake(route):
                posted.append(route.request.post_data or '')
                route.fulfill(status=200, content_type='application/json', body=json.dumps(RESULT))
            page.route('**/api/check', fake)
            page.goto('http://127.0.0.1:8144')
            page.wait_for_function("() => !document.getElementById('web-opt').hidden")
            assert 'Yandex' in page.locator('#web-label').inner_text()
            assert not page.locator('#web_search').is_checked()                  # off by default
            page.locator('#web_search').check()
            page.locator('input[name=mode][value=reference]').check()
            assert page.locator('#web-opt').is_hidden()
            page.locator('input[name=mode][value=academic]').check()
            assert not page.locator('#web_search').is_checked()                  # consent reset on mode change
            page.locator('[data-lang=uz]').click()
            assert 'Yandex' in page.locator('#web-label').inner_text() and 'Internetda' in page.locator('#web-label').inner_text()
            page.locator('[data-lang=ru]').click()
            assert 'Яндекс' in page.locator('#web-label').inner_text()
            page.locator('[data-lang=en]').click()
            page.locator('#submission').set_input_files({'name': 'paper.txt', 'mimeType': 'text/plain', 'buffer': ('word ' * 40).encode()})
            page.locator('#consent').check()
            page.locator('#web_search').check()
            page.locator('button[type=submit]').click()
            page.wait_for_function("() => document.body.innerText.includes('Web (Yandex)')")
            body = page.locator('body').inner_text()
            assert 'Web (Yandex): 2 searches, 3 pages tried, 2 compared' in body, body
            assert 'Web page (compared, not stored)' in body
            assert 'name="web_search"' in posted[0] and 'true' in posted[0].split('name="web_search"')[1][:20]
            if SHOTS:
                page.locator('#result').scroll_into_view_if_needed()
                page.screenshot(path=str(SHOTS / f'web-search-{width}.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            assert not errors, errors
            page.context.close()
            print(f'PASS {width}px: Yandex consent off by default, academic only, resets, UZ/RU/EN, web results shown')
        browser.close()
finally:
    web.terminate(); web.wait(timeout=10)
