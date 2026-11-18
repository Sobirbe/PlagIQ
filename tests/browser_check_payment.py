"""Browser check of "one document, one payment" in TEST mode (synthetic document, simulated Payme calls, no network).

Sign in with an e-mail code → upload 1,500 words → "Pay 15,000 UZS with Payme" → the browser is sent to the Payme
SANDBOX page (intercepted here) → coming back alone runs nothing → Payme's signed calls confirm the payment →
the page shows the check running and done → the report has its PDF.

Usage: CHROMIUM_PATH=... python tests/browser_check_payment.py
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = 8152
BASE = f"http://127.0.0.1:{PORT}"
KEY = "sandbox-key-for-browser-test"
TEXT = " ".join(f"w{i % 911}y{i % 7}" for i in range(1500))


def serve(tmp):
    env = {k: v for k, v in os.environ.items() if k not in ("ENGINE_URL", "PRICING_TIERS", "PRIVATE_PILOT_USER",
                                                            "PRIVATE_PILOT_PASSWORD", "SMTP_HOST")}
    env.update(TELEGRAM_BOT_TOKEN="123456789:AAtest-token-for-plagiq-browser_0123456789", TELEGRAM_BOT_USERNAME="plagiq_bot",
               MAIL_BACKEND="dev", USERS_DB=f"{tmp}/users.sqlite3", REPORTS_DB=f"{tmp}/reports.sqlite3",
               METRICS_DB=f"{tmp}/metrics.sqlite3", TEMPLATES_DB=f"{tmp}/templates.sqlite3", PUBLIC_ORIGIN=BASE,
               PAYMENTS_ENABLED="1", PAYMENTS_MODE="test", PAYME_MERCHANT_ID="5e730e8e0b852a417aa49ceb", PAYME_TEST_KEY=KEY)
    log = open(f"{tmp}/server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urlopen(BASE + "/health", timeout=1).close()
            return proc
        except OSError:
            time.sleep(.1)
    proc.terminate()
    raise RuntimeError(Path(f"{tmp}/server.log").read_text())


def payme(method, params):
    body = json.dumps({"method": method, "params": params, "id": 1}).encode()
    auth = "Basic " + base64.b64encode(f"Paycom:{KEY}".encode()).decode()
    with urlopen(Request(BASE + "/payments/payme", body, {"Authorization": auth, "Content-Type": "application/json"})) as r:
        reply = json.loads(r.read())
    assert "result" in reply, reply
    return reply


with tempfile.TemporaryDirectory() as tmp:
    web = serve(tmp)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"), headless=True,
                                        args=["--no-sandbox"])
            page = browser.new_context(viewport={"width": 390, "height": 900}, locale="en-US").new_page()
            errors, sandbox = [], []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" else None)
            page.route("https://test.paycom.uz/**", lambda route: (sandbox.append(route.request.url),
                                                                  route.fulfill(body="<p>Payme sandbox (stub)</p>",
                                                                                content_type="text/html")))
            page.add_init_script("try{localStorage.getItem('plagiq-ui-lang')||localStorage.setItem('plagiq-ui-lang','en')}catch(e){}")  # an English-speaking visitor
            page.goto(BASE)
            page.wait_for_function("() => document.querySelectorAll('#price-cards .price').length === 6")
            page.locator("#account summary").click()
            page.get_by_role("button", name="Continue with Email").click()
            page.locator(".emailflow input[type=email]").fill("payer@example.uz")
            page.get_by_role("button", name="Send code").click()
            page.wait_for_function("() => !document.querySelector('.emailflow input[name=code]').closest('[hidden]')")
            code = re.findall(r"sign-in code: (\d{6})", Path(f"{tmp}/server.log").read_text())[-1]
            page.locator(".emailflow input[name=code]").fill(code)
            page.locator(".emailflow").get_by_role("button", name="Sign in").click()
            page.wait_for_function("() => document.querySelector('#account summary').textContent.includes('👤')")
            page.locator("[name=mode][value=reference]").check()
            page.locator("#reference_text").fill(TEXT)
            page.locator("#submission").set_input_files({"name": "article.txt", "mimeType": "text/plain", "buffer": TEXT.encode()})
            page.wait_for_function("() => [...document.querySelectorAll('#quote .paybtn')].some(b => b.textContent.includes('Payme'))")
            assert "Total: 15,000 UZS" in page.locator("#quote").inner_text()
            assert "from your balance" not in page.locator("#quote").inner_text()          # balance is 0
            page.locator("#submit").click()                                                   # no balance: choose a provider
            page.wait_for_function("() => document.querySelector('#status').textContent.includes('Choose how to pay')")
            page.locator("#quote .paybtn", has_text="Payme").click()
            page.wait_for_function("() => location.href.startsWith('https://test.paycom.uz/')")
            params = base64.b64decode(sandbox[-1].rsplit("/", 1)[1]).decode()
            order_id = re.search(r"ac\.order_id=(\d+)", params).group(1)
            assert "a=1500000" in params, params                                                 # 15,000 so'm in tiyin
            # the browser comes back before paying: nothing runs
            page.goto(f"{BASE}/?paid={order_id}")
            page.wait_for_function("() => document.querySelector('#pending-box').textContent.includes('Waiting for')")
            time.sleep(6)
            assert "Waiting for" in page.locator("#pending-box").inner_text()
            # Payme's signed server-to-server calls confirm the payment
            payme("CreateTransaction", {"amount": 1500000, "account": {"order_id": order_id}, "id": "b" * 24, "time": 5})
            payme("PerformTransaction", {"id": "b" * 24})
            page.wait_for_function("() => document.querySelector('#pending-box').textContent.includes('has been checked')",
                                   timeout=60000)
            page.locator("#pending-box button").click()
            page.wait_for_function("() => document.querySelector('#score').textContent==='100%'")
            assert page.locator("#report").is_visible()                                      # paid: PDF available
            page.locator("#account summary").click()
            menu = page.locator("#account .menu").inner_text()
            assert "Balance: 0" in menu.replace(" ", " ") and "Top up" not in menu and "package" not in menu.lower(), menu
            assert not errors, errors
            browser.close()
    finally:
        web.terminate()
        web.wait(5)
print("browser per-check payment ok")
