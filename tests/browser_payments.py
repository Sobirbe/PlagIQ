"""Browser check: balance, top-up through the Payme sandbox (simulated), charged check, receipt.

Usage: CHROMIUM_PATH=... python tests/browser_payments.py [screenshot_dir]
No real Payme, Click or Telegram call is made: the sandbox checkout page is replaced by a stub and
Payme's server calls are sent by this script with the test key.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

TOKEN = "123456789:AAtest-token-for-plagiq-tests_0123456789"
EN = ("The rapid growth of remittance inflows has reshaped rural household budgets across the Fergana valley, "
      "and this study examines how families allocate these funds between consumption and investment. ") * 6


def tg_result(user_id):
    data = {"id": user_id, "first_name": "Dilnoza", "last_name": "Karimova", "username": "dilnoza", "auth_date": int(time.time())}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
KEY = "sandbox-test-key-not-real"
PORT = 8151
BASE = f"http://127.0.0.1:{PORT}"
LONG = EN * 7                                       # about 1,180 words: Researcher plan, 15,000 so'm


def payme(method, params):
    request = urllib.request.Request(BASE + "/payments/payme", json.dumps({"id": 1, "method": method, "params": params}).encode(),
                                     {"Content-Type": "application/json",
                                      "Authorization": "Basic " + base64.b64encode(f"Paycom:{KEY}".encode()).decode()})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


tmp = tempfile.mkdtemp()
env = {k: v for k, v in os.environ.items() if k not in ('ENGINE_URL', 'PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD')}
env.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_BOT_USERNAME="plagiq_bot", USERS_DB=tmp + "/users.sqlite3",
           PAYMENTS_ENABLED="1", PAYMENTS_MODE="test", PAYME_MERCHANT_ID="5e730e8e0b852a417aa49ceb", PAYME_TEST_KEY=KEY,
           PUBLIC_ORIGIN=BASE)
web = start('app', PORT, env)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
        for width in (390, 1440):
            context = browser.new_context(viewport={'width': width, 'height': 900}, locale='en-US', device_scale_factor=2 if SHOTS else 1)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(BASE + '/login#tgAuthResult=' + tg_result(42 + width))
            page.locator('[data-lang=en]').click()
            page.wait_for_selector('#account summary:not(.signin-open)')
            page.wait_for_function('() => document.querySelector("#account summary").textContent.includes("UZS")')
            assert '0 UZS' in page.locator('#account summary').inner_text()
            assert 'TEST MODE' in page.locator('#pilot-bar').inner_text()
            page.locator('#calc-words').fill('3000')
            assert '15,000' in page.locator('#calc-out').inner_text() and 'taken from your balance' in page.locator('#calc-out').inner_text()
            # A paid check with an empty balance is refused.
            page.locator('input[name=mode][value=reference]').check()
            page.locator('#submission').set_input_files({'name': 'essay.txt', 'mimeType': 'text/plain', 'buffer': LONG.encode()})
            page.locator('#reference_text').fill(EN)
            page.locator('button[type=submit]').click()
            page.wait_for_function('() => document.querySelector("#status").textContent.includes("balance is too low")')
            # Top up 20,000 so'm with Payme: the browser goes to the sandbox checkout.
            page.locator('#account summary').click()
            menu = page.locator('#account .menu')
            assert 'sandbox payments only' in menu.inner_text() and 'Balance: 0' in menu.inner_text()
            if SHOTS:
                page.evaluate('scrollTo(0,0)')
                page.screenshot(path=str(SHOTS / f'balance-menu-{width}.png'))
            menu.locator('input[type=number]').fill('20000')
            context.route('https://test.paycom.uz/**', lambda route: route.fulfill(status=200, content_type='text/html',
                                                                                  body='<h1>Payme sandbox stub</h1>'))
            with page.expect_navigation(url='https://test.paycom.uz/**'):
                menu.locator('button', has_text='Pay with Payme').click()
            decoded = base64.b64decode(page.url[len('https://test.paycom.uz/'):]).decode()
            order_id = decoded.split('ac.order_id=')[1].split(';')[0]
            assert 'a=2000000' in decoded and f'c={BASE}/?paid={order_id}' in decoded
            # Payme's server confirms the payment.
            tx = order_id.rjust(24, '0')
            params = {"amount": 2000000, "account": {"order_id": order_id}}
            assert payme('CheckPerformTransaction', params)['result']['allow'] is True
            assert payme('CreateTransaction', {**params, "id": tx, "time": int(time.time() * 1000)})['result']['state'] == 1
            assert payme('PerformTransaction', {"id": tx})['result']['state'] == 2
            # The user comes back.
            page.goto(f'{BASE}/?paid={order_id}')
            page.wait_for_function('() => document.querySelector("#login-note").textContent.includes("Payment received")')
            assert '20,000' in page.locator('#login-note').inner_text()
            assert '?paid=' not in page.url
            page.wait_for_function('() => document.querySelector("#account summary").textContent.includes("20,000")')
            page.locator('input[name=mode][value=reference]').check()
            page.locator('#submission').set_input_files({'name': 'essay.txt', 'mimeType': 'text/plain', 'buffer': LONG.encode()})
            page.locator('#reference_text').fill(EN)
            page.locator('button[type=submit]').click()
            page.wait_for_selector('#result', state='visible')
            page.wait_for_function('() => document.querySelector("#plan-note").textContent.includes("Charged")')
            assert 'Charged 15,000' in page.locator('#plan-note').inner_text()
            page.wait_for_function('() => document.querySelector("#account summary").textContent.includes("5,000")')
            page.locator('#account summary').click()
            with page.expect_popup() as popup:
                page.locator('#account .menu a', has_text='receipt').click()
            receipt = popup.value
            receipt.wait_for_load_state()
            text = receipt.locator('main').inner_text()
            assert 'TEST PAYMENT' in text and order_id in text and '20 000' in text, text
            if SHOTS and width == 1440:
                receipt.screenshot(path=str(SHOTS / 'receipt.png'))
            page.locator('[data-lang=uz]').click()
            assert 'SINOV REJIMI' in page.locator('#pilot-bar').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            assert not errors, errors
            context.close()
        browser.close()
finally:
    web.terminate()
print('browser payments: ok')
