"""Browser check of the sign-in, free-trial, pricing and "My checks" flow (synthetic documents only).

Upload → word count → sign in with an e-mail code (development mail backend: the code is read from the server log)
→ the file is still selected → free check → result on screen without PDF/CSV buttons → "My checks" → a second
check is refused (trial used) → owner-free pilot allowance cannot be created from the page. Payments stay off.

Usage: CHROMIUM_PATH=... python tests/browser_trial.py [screenshot_dir]
"""
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
PORT = 8151
TEXT = " ".join(f"w{i % 397}x{i % 13}" for i in range(400))          # 400 synthetic words


def serve(tmp):
    env = {k: v for k, v in os.environ.items() if k not in (
        "ENGINE_URL", "PRICING_TIERS", "PRIVATE_PILOT_USER", "PRIVATE_PILOT_PASSWORD", "PAYMENTS_ENABLED", "SMTP_HOST",
        "PUBLIC_ORIGIN")}
    env.update(TELEGRAM_BOT_TOKEN="123456789:AAtest-token-for-plagiq-browser_0123456789", TELEGRAM_BOT_USERNAME="plagiq_bot",
               MAIL_BACKEND="dev", USERS_DB=f"{tmp}/users.sqlite3", REPORTS_DB=f"{tmp}/reports.sqlite3",
               METRICS_DB=f"{tmp}/metrics.sqlite3", TEMPLATES_DB=f"{tmp}/templates.sqlite3")
    log = open(f"{tmp}/server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1).close()
            return proc
        except OSError:
            time.sleep(.1)
    proc.terminate()
    raise RuntimeError(Path(f"{tmp}/server.log").read_text())


def last_code(tmp):
    for _ in range(50):
        codes = re.findall(r"sign-in code: (\d{6})", Path(f"{tmp}/server.log").read_text())
        if codes:
            return codes[-1]
        time.sleep(.1)
    raise AssertionError("no code in the development mail log")


with tempfile.TemporaryDirectory() as tmp:
    web = serve(tmp)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"), headless=True,
                                        args=["--no-sandbox"])
            for n, width in enumerate((390, 1440)):
                page = browser.new_context(viewport={"width": width, "height": 900}, locale="en-US",
                                           device_scale_factor=2 if SHOTS else 1).new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.add_init_script("try{localStorage.getItem('plagiq-ui-lang')||localStorage.setItem('plagiq-ui-lang','en')}catch(e){}")  # an English-speaking visitor
                page.goto(f"http://127.0.0.1:{PORT}")
                page.wait_for_function("() => document.querySelectorAll('#price-cards .price').length === 6")
                cards = page.locator("#price-cards").inner_text()
                for needle in ("Free trial", "Student", "15,000", "Researcher", "25,000", "Extended", "39,000",
                               "Master's Thesis", "49,000", "PhD / DSc", "79,000", "Recommended for Journal Articles",
                               "Recheck after revision: 12,000", "Recheck after revision: 63,200", "Up to 150,000 words"):
                    assert needle in cards, (needle, cards)
                assert "Most Popular" not in page.content()
                assert page.locator("#pricing-title").inner_text() == "One document. One payment. No subscription."
                assert "For Universities and Organizations" in page.locator("#orgs").inner_text()
                assert "not an accredited certificate" in page.locator("#cert-note").inner_text()
                assert "Packages" not in page.locator("#svc-cards").inner_text()
                for words, plan in (("1000", "free check"), ("1001", "Student"), ("3501", "Researcher"), ("10001", "Extended"),
                                    ("25001", "Master's Thesis"), ("40001", "PhD / DSc"), ("150001", "contact us")):
                    page.locator("#calc-words").fill(words)
                    assert plan in page.locator("#calc-out").inner_text(), (words, page.locator("#calc-out").inner_text())
                # upload first, then sign in: the file must stay selected
                page.locator("[name=mode][value=reference]").check()
                page.locator("#submission").set_input_files({"name": "essay.txt", "mimeType": "text/plain", "buffer": TEXT.encode()})
                page.wait_for_function("() => document.querySelector('#quote').textContent.includes('400')")
                assert "fits the free check" in page.locator("#quote").inner_text().lower()
                page.locator("#reference_text").fill(TEXT)
                page.locator("#submit").click()
                page.wait_for_function("() => document.querySelector('#account details') && document.querySelector('#account details').open")
                assert "Sign in to run a check" in page.locator("#status").inner_text() or "Sign in" in page.content()
                menu = page.locator("#account .signin")
                assert "Continue with Telegram" in menu.inner_text() and "Continue with Email" in menu.inner_text()
                assert "phone" not in menu.inner_text().lower() and "Password (at least" not in menu.inner_text()
                page.get_by_role("button", name="Continue with Email").click()
                page.locator(".emailflow input[type=email]").fill(f"student{n}@example.uz")
                page.get_by_role("button", name="Send code").click()
                page.wait_for_function("() => !document.querySelector('.emailflow input[name=code]').closest('[hidden]')")
                page.locator(".emailflow input[name=code]").fill(last_code(tmp))
                page.locator(".emailflow").get_by_role("button", name="Sign in").click()
                page.wait_for_function("() => document.querySelector('#quote').textContent.includes('This will be your free check')")
                assert page.evaluate("document.querySelector('#submission').files[0].name") == "essay.txt"
                page.locator("#submit").click()
                page.wait_for_function("() => document.querySelector('#score').textContent==='100%'")
                for button in ("#report", "#report-full", "#sources-csv"):
                    assert page.locator(button).is_hidden(), button
                assert "result is shown on screen only" in page.locator("#plan-note").inner_text()
                page.wait_for_function("() => !document.querySelector('#mychecks').hidden && document.querySelector('#mychecks-body').textContent.includes('Free check')")
                row = page.locator("#mychecks-body tr").first.inner_text()
                assert "Free check" in row and "400" in row and "On screen only" in row and "Free" in row, row
                assert "kept until deleted" in page.locator("#mychecks-retention").inner_text()
                assert "Free check: used" in page.locator("#mychecks-state").inner_text()
                # the server refuses the PDF of a free check even when asked directly
                rid = page.evaluate("lastResult.report_id")
                status = page.evaluate(f"fetch('/api/report/{rid}.pdf').then(r => r.status)")
                assert status == 403, status
                # a second check: the trial is used, payments are off
                page.locator("#submission").set_input_files({"name": "second.txt", "mimeType": "text/plain",
                                                             "buffer": (TEXT + " more words here").encode()})
                page.wait_for_function("() => document.querySelector('#quote').textContent.includes('free check has been used')")
                page.locator("#submit").click()
                page.wait_for_function("() => document.querySelector('#status').textContent.includes('already been used')")
                for code, title in (("uz", "Bitta hujjat. Bitta to‘lov. Obunasiz."), ("uzc", "Битта ҳужжат. Битта тўлов. Обунасиз."),
                                    ("ru", "Один документ. Одна оплата. Без подписки.")):
                    page.locator(f"[data-lang={code}]").click()
                    assert page.locator("#pricing-title").inner_text() == title, (code, page.locator("#pricing-title").inner_text())
                    assert page.locator("#mychecks-title").inner_text()
                assert "15 000" in page.locator("#price-cards").inner_text().replace(" ", " ").replace(" ", " ")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, "overflow")
                if SHOTS:
                    page.locator("[data-lang=en]").click()
                    page.locator("#pricing").screenshot(path=str(SHOTS / f"pricing-{width}.png"))
                    page.locator("#mychecks").screenshot(path=str(SHOTS / f"mychecks-{width}.png"))
                assert not errors, errors
                page.context.close()
            browser.close()
    finally:
        web.terminate()
        web.wait(5)
print("browser trial flow ok")
