"""Browser check: a check needs an account; signing in with e-mail and a one-time code; sign out; sign in again.

Usage: CHROMIUM_PATH=... python tests/browser_sign_in.py [screenshot_dir]
The site runs with MAIL_BACKEND=dev, which writes the sign-in e-mail to the server log instead of sending it; the
test reads the 6-digit code from there, as a user would read it from the e-mail. (Telegram sign-in is covered by
browser_accounts.py, the phone-number route by test_sign_in_options.py.)
"""
import os
import re
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
PORT = 8153
tmp = tempfile.mkdtemp()
env = {k: v for k, v in os.environ.items() if k not in ('ENGINE_URL', 'PRIVATE_PILOT_USER', 'PRIVATE_PILOT_PASSWORD',
                                                        'PUBLIC_ORIGIN', 'RESEND_API_KEY', 'BREVO_API_KEY', 'SMTP_HOST')}
env.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_BOT_USERNAME="plagiq_bot", USERS_DB=tmp + "/users.sqlite3",
           EMAIL_SIGNIN="1", MAIL_BACKEND="dev")
web = start('app', PORT, env)
log = []
threading.Thread(target=lambda: [log.append(line.decode("utf-8", "replace")) for line in iter(web.stderr.readline, b"")],
                 daemon=True).start()


def code_for(address, after):
    """The newest 6-digit code in the dev e-mail written to the log for `address` (after line `after`)."""
    for _ in range(100):
        text = "".join(log[after:])
        mails = text.split("not sent: to=" + address)
        if len(mails) > 1:
            found = re.findall(r"\b(\d{6})\b", mails[-1])
            if found:
                return found[0]
        time.sleep(0.1)
    raise AssertionError("no sign-in e-mail in the log for " + address)


def sign_in_by_email(page, address):
    if page.locator('#account > details').get_attribute('open') is None:
        page.locator('#account > details > summary.signin-open').click()
    menu = page.locator('#account .menu.signin')
    if menu.locator('input[name=email]').first.is_hidden():
        menu.locator('button.email-open').click()
    before = len(log)
    box = menu.locator('.emailflow')
    box.locator('input[name=email]').fill(address)
    box.locator('button', has_text='Send code').click()
    page.wait_for_function("() => !document.querySelector('#account .emailflow input[name=code]').closest('[hidden]')")
    assert address in box.locator('.sent').inner_text()
    box.locator('input[name=code]').fill(code_for(address, before))
    box.locator('input[name=code]').press('Enter')


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
            page.wait_for_selector('#account > details > summary.signin-open')
            # 1. A check needs an account: the file stays selected and the sign-in menu opens.
            page.locator('input[name=mode][value=reference]').check()
            page.locator('#submission').set_input_files({'name': 'essay.txt', 'mimeType': 'text/plain', 'buffer': EN.encode()})
            page.locator('#reference_text').fill(EN)
            page.locator('#submit').click()
            page.wait_for_function("() => document.querySelector('#status').textContent.includes('Sign in to run a check')")
            assert page.locator('#result').is_hidden()
            # 2. Sign in with e-mail and the one-time code (a new address creates the account).
            address = f'student{width}@example.uz'
            wrong_before = len(log)
            if page.locator('#account > details').get_attribute('open') is None:
                page.locator('#account > details > summary.signin-open').click()
            menu = page.locator('#account .menu.signin')
            if menu.locator('.emailflow input[name=email]').is_hidden():
                menu.locator('button.email-open').click()
            box = menu.locator('.emailflow')
            box.locator('input[name=email]').fill(address)
            box.locator('button', has_text='Send code').click()
            page.wait_for_function("() => !document.querySelector('#account .emailflow input[name=code]').closest('[hidden]')")
            code = code_for(address, wrong_before)
            box.locator('input[name=code]').fill('000000' if code != '000000' else '111111')     # a wrong code first
            box.locator('input[name=code]').press('Enter')
            page.wait_for_function("() => document.querySelector('#account .emailflow .err').textContent.length > 0")
            box.locator('input[name=code]').fill(code)
            box.locator('input[name=code]').press('Enter')
            page.wait_for_function(f"() => ((document.querySelector('#account > details > summary')||{{}}).textContent||'').includes('student{width}')")
            assert page.locator('#login-note').is_hidden()
            # 3. The check now runs (the account's free check).
            page.locator('#submit').click()
            page.wait_for_selector('#result', state='visible')
            assert page.locator('#score').inner_text().endswith('%')
            # 4. Sign out, then sign in again with a new code.
            page.locator('#account > details > summary').click()
            page.locator('#account .menu button', has_text='Sign out').click()
            page.wait_for_selector('#account > details > summary.signin-open')
            sign_in_by_email(page, address)
            page.wait_for_function(f"() => ((document.querySelector('#account > details > summary')||{{}}).textContent||'').includes('student{width}')")
            # 5. The "check is ready" message can be switched off in the account menu, and stays off after a reload.
            page.locator('#account > details > summary').click()
            row = page.locator('#account .menu .notify-row')
            assert 'when a check ends after I have closed the page' in row.inner_text()
            assert row.locator('input').is_checked()
            assert 'null' not in page.locator('#account .menu').inner_text()          # append(null) printed "null"
            row.locator('input').uncheck()
            page.wait_for_function("() => window.meNotify === false")
            page.reload()
            page.wait_for_function(f"() => ((document.querySelector('#account > details > summary')||{{}}).textContent||'').includes('student{width}')")
            page.locator('#account > details > summary').click()
            assert not page.locator('#account .menu .notify-row input').is_checked()
            if SHOTS:
                page.screenshot(path=str(SHOTS / f'account-menu-{width}.png'))
                page.evaluate('scrollTo(0,0)')
                page.screenshot(path=str(SHOTS / f'signed-in-{width}.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'overflow')
            assert not errors, errors
            page.context.close()
            print(f'PASS {width}px: check needs an account, e-mail code sign-in (wrong code refused), check runs, sign out and in')
        browser.close()
finally:
    web.terminate()
