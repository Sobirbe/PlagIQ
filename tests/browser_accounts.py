"""Browser check: Telegram sign-in (signed test data), login gate, AI detector mode and panel.

Usage: CHROMIUM_PATH=... python tests/browser_accounts.py [screenshot_dir]
Pangram is simulated by a local server; no real Telegram or Pangram call is made.
"""
import base64
import hashlib
import hmac
import http.server
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
TOKEN = "123456789:AAtest-token-for-plagiq-tests_0123456789"
EN = ("The rapid growth of remittance inflows has reshaped rural household budgets across the Fergana valley, "
      "and this study examines how families allocate these funds between consumption and investment. ") * 6
RESULT = {"stage": "STAGE_SUCCESS", "text": EN, "version": "4.1", "prediction_short": "Mixed", "fraction_ai": 0.42,
          "fraction_ai_assisted": 0.13, "fraction_human": 0.45,
          "windows": [{"text": "The rapid growth of remittance inflows has reshaped rural household budgets",
                       "label": "AI-Generated", "ai_assistance_score": 0.97, "confidence": "High", "word_count": 12,
                       "is_humanized": True}]}


class Pangram(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.reply({"task_id": "123e4567-e89b-12d3-a456-426614174000"})

    def do_GET(self):
        self.reply(RESULT)


def tg_data(user_id=42):
    data = {"id": user_id, "first_name": "Dilnoza", "last_name": "Karimova", "username": "dilnoza", "auth_date": int(time.time())}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return data


def tg_result(user_id=42):
    return base64.urlsafe_b64encode(json.dumps(tg_data(user_id), ensure_ascii=False).encode()).decode().rstrip("=")


def telegram_popup(route):
    """Stands in for oauth.telegram.org: like Telegram's page, it posts the signed result to the opener and closes."""
    message = json.dumps({"event": "auth_result", "result": tg_data()})
    route.fulfill(status=200, content_type="text/html",
                  body=f"<script>window.opener.postMessage({json.dumps(message)}, '*'); window.close();</script>")


fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pangram)
threading.Thread(target=fake.serve_forever, daemon=True).start()
tmp = tempfile.mkdtemp()
env = {k: v for k, v in os.environ.items() if k not in ('ENGINE_URL', 'PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD')}
env.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_BOT_USERNAME="plagiq_bot", USERS_DB=tmp + "/users.sqlite3",
           PANGRAM_API_KEY="pg-test", PANGRAM_URL=f"http://127.0.0.1:{fake.server_port}")
web = start('app', 8146, env)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
        for width in (390, 1440):
            page = browser.new_context(viewport={'width': width, 'height': 900}, locale='en-US',
                                       device_scale_factor=2 if SHOTS else 1).new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('http://127.0.0.1:8146')
            page.locator('[data-lang=en]').click()
            page.wait_for_selector('#account button.tg')
            assert page.locator('#account').inner_text() == 'Sign in with Telegram'
            assert 'Sign in with Telegram to run a check' in page.locator('#login-note').inner_text()
            page.locator('#submission').set_input_files({'name': 'essay.txt', 'mimeType': 'text/plain', 'buffer': EN.encode()})
            page.locator('#consent').check()
            page.locator('button[type=submit]').click()
            assert 'Please sign in with Telegram' in page.locator('#status').inner_text()
            page.context.route('https://oauth.telegram.org/**', telegram_popup)
            with page.expect_popup() as popup:
                page.locator('#account button.tg').click()
            assert popup.value.url.startswith('https://oauth.telegram.org/auth?bot_id=123456789&origin=http%3A%2F%2F127.0.0.1%3A8146')
            page.wait_for_selector('#account summary')                              # PlagIQ tab stays open
            assert 'Dilnoza Karimova' in page.locator('#account summary').inner_text()
            assert page.locator('#status').inner_text() == ''                        # "sign in first" message cleared
            page.locator('#account summary').click()
            page.locator('#account .menu button', has_text='Sign out').click()
            page.wait_for_selector('#account button.tg')
            page.goto('http://127.0.0.1:8146/login#tgAuthResult=' + tg_result())    # redirect fallback still works
            page.wait_for_selector('#account summary')
            assert 'Dilnoza Karimova' in page.locator('#account summary').inner_text() and page.url.endswith('/')
            assert page.locator('#login-note').is_hidden()
            # AI detector mode
            page.locator('input[name=mode][value=ai]').check()
            assert page.locator('#academic-note').is_hidden() and page.locator('#options-set').is_hidden()
            assert 'Pangram (USA)' in page.locator('#ai-explain').inner_text()
            page.locator('#submission').set_input_files({'name': 'essay.txt', 'mimeType': 'text/plain', 'buffer': EN.encode()})
            page.locator('button[type=submit]').click()
            assert 'agree to send the text to the AI detector' in page.locator('#status').inner_text()
            page.locator('#ai_consent').check()
            page.locator('button[type=submit]').click()
            page.wait_for_selector('#ai-box h3')
            box = page.locator('#ai-box').inner_text()
            for expected in ('AI detector (Pangram estimate)', 'Mixed · AI-written or AI-assisted: 55%', 'AI-written 42%',
                             'AI-assisted 13%', 'Human 45%', '1 of 1 passages flagged · 1 look “humanized”', 'not proof'):
                assert expected in box, (expected, box)
            assert page.locator('#score').inner_text() == '55%'
            page.locator('#ai-box details summary').click()
            assert 'The rapid growth of remittance inflows' in page.locator('#ai-box details').inner_text()
            page.locator('[data-lang=uz]').click()
            assert 'AI detektor (Pangram bahosi)' in page.locator('#ai-box').inner_text()
            assert 'Hujjatni AI detektoriga tekshirish' in page.locator('#mode-ai').inner_text()
            page.locator('[data-lang=en]').click()
            if SHOTS:
                page.locator('#ai-box').scroll_into_view_if_needed()
                page.locator('#result').screenshot(path=str(SHOTS / f'ai-result-{width}.png'))
            page.locator('#account summary').click()
            assert 'My recent checks' in page.locator('#account .menu').inner_text() and 'AI 42' in page.locator('#account .menu').inner_text()
            if SHOTS and width == 1440:
                page.evaluate('scrollTo(0,0)')
                page.screenshot(path=str(SHOTS / 'account-menu.png'), clip={'x': 700, 'y': 0, 'width': 740, 'height': 330})
            page.locator('#account .menu button', has_text='Sign out').click()
            page.wait_for_selector('#account button.tg')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            assert not errors, errors
            page.context.close()
            print(f'PASS {width}px: Telegram sign-in, login gate, AI detector mode, panel, history, sign-out')
        browser.close()
finally:
    web.terminate(); web.wait(timeout=10); fake.shutdown()
