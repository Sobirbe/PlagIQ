"""Browser check: short check without an account, then email + password registration and sign-in.

Usage: CHROMIUM_PATH=... python tests/browser_sign_in.py [screenshot_dir]
(The phone-number route through the Telegram bot is covered by test_sign_in_options.py.)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser_smoke_lib import start  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
TOKEN = "123456789:AAtest-token-for-plagiq-tests_0123456789"
EN = ("The rapid growth of remittance inflows has reshaped rural household budgets across the Fergana valley, "
      "and this study examines how families allocate these funds between consumption and investment. ") * 6
PORT = 8153
tmp = tempfile.mkdtemp()
env = {k: v for k, v in os.environ.items() if k not in ('ENGINE_URL', 'PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD', 'PUBLIC_ORIGIN')}
env.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_BOT_USERNAME="plagiq_bot", USERS_DB=tmp + "/users.sqlite3")
web = start('app', PORT, env)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
        for width in (390, 1440):
            page = browser.new_context(viewport={'width': width, 'height': 900}, locale='en-US',
                                       device_scale_factor=2 if SHOTS else 1).new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{PORT}')
            page.locator('[data-lang=en]').click()
            page.wait_for_selector('#account summary.signin-open')
            # 1. A short text is checked without an account.
            page.locator('input[name=mode][value=reference]').check()
            page.locator('#submission').set_input_files({'name': 'essay.txt', 'mimeType': 'text/plain', 'buffer': EN.encode()})
            page.locator('#reference_text').fill(EN)
            page.locator('#submit').click()
            page.wait_for_selector('#result', state='visible')
            assert page.locator('#score').inner_text().endswith('%')
            # 2. Register with email and password.
            page.locator('#account summary.signin-open').click()
            menu = page.locator('#account .menu.signin')
            assert 'Sign in with Telegram' in menu.inner_text() and 'No email is sent' in menu.inner_text()
            if SHOTS:
                page.evaluate('scrollTo(0,0)')
                page.screenshot(path=str(SHOTS / f'sign-in-menu-{width}.png'))
            email = f'student{width}@example.uz'
            menu.locator('input[name=email]').fill(email)
            menu.locator('input[name=password]').fill('short')
            menu.locator('button', has_text='Create account').click()
            page.wait_for_function("() => document.querySelector('#account .err').textContent.includes('8 characters')")
            menu.locator('input[name=password]').fill('a good password')
            menu.locator('input[name=uname]').fill('Malika')
            menu.locator('button', has_text='Create account').click()
            page.wait_for_function("() => document.querySelector('#account summary').textContent.includes('Malika')")
            assert page.locator('#login-note').is_hidden()
            # 3. Sign out, then sign back in with Enter.
            page.locator('#account summary').click()
            page.locator('#account .menu button', has_text='Sign out').click()
            page.wait_for_selector('#account summary.signin-open')
            page.locator('#account summary.signin-open').click()
            menu = page.locator('#account .menu.signin')
            menu.locator('input[name=email]').fill(email)
            menu.locator('input[name=password]').fill('wrong password')
            menu.locator('input[name=password]').press('Enter')
            page.wait_for_function("() => document.querySelector('#account .err').textContent.includes('Wrong email or password')")
            menu.locator('input[name=password]').fill('a good password')
            menu.locator('input[name=password]').press('Enter')
            page.wait_for_function("() => document.querySelector('#account summary').textContent.includes('Malika')")
            page.locator('[data-lang=uz]').click()
            page.locator('#account summary').click()
            page.locator('#account .menu button', has_text='Chiqish').click()
            page.wait_for_selector('#account summary.signin-open')
            assert page.locator('#account summary').inner_text() == 'Kirish'
            assert "ro‘yxatdan o‘tmasdan" in page.locator('#login-note').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            assert not errors, errors
            page.context.close()
        browser.close()
finally:
    web.terminate()
print('browser sign-in: ok')
