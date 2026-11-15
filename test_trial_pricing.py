"""Free trial, plans, rechecks, pilot allowances, passwordless e-mail sign-in and export rules.

All documents are synthetic (generated words); no real publication is used. Payments stay off unless a test turns
on the providers' TEST mode with dummy keys (no network call is made: only the balance ledger is used).
"""
import random
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import entitlements
import mailer
import payments
import pricing
from test_auth_ai import TOKEN, sign_in

VOCAB = [f"w{i}x" for i in range(5000)]
ORIGINAL_ANALYSE = web.analyse


def doc(words, seed=1):
    rng = random.Random(seed)
    return " ".join(rng.choice(VOCAB) for _ in range(words))


def revise(text, share, seed=9):
    """Replace `share` of the words, in runs (as an author rewriting sentences would)."""
    rng = random.Random(seed)
    words = text.split()
    i = 0
    while i < len(words):
        if rng.random() < share:
            for k in range(i, min(i + 8, len(words))):
                words[k] = rng.choice(VOCAB) + "r"
            i += 8
        else:
            i += 8
    return " ".join(words)


@pytest.fixture()
def site(monkeypatch, tmp_path):
    for name, value in {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_BOT_USERNAME": "plagiq_bot",
                        "USERS_DB": str(tmp_path / "users.sqlite3"), "PUBLIC_ORIGIN": "https://plagiq.example",
                        "MAIL_BACKEND": "dev", "VERIFY_SECRET": "v" * 48}.items():
        monkeypatch.setenv(name, value)
    for name in ("LOGIN_REQUIRED", "SESSION_SECRET", "EMAIL_LOGIN", "PAYMENTS_ENABLED", "PRICING_TIERS", "SMTP_HOST",
                 "EMAIL_SIGNIN", "RECHECK_PERCENT", "RECHECK_DAYS", "RECHECK_LIMIT", "PASSWORD_SIGNUP"):
        monkeypatch.delenv(name, raising=False)
    web.RATE.clear(); web.results.clear(); web.QUOTES.clear(); auth._FAILS.clear(); auth._SENDS.clear()
    mailer.OUTBOX.clear()
    monkeypatch.setattr(web, "RATE_LIMIT", 1000)
    return TestClient(web.app, base_url="https://plagiq.example")


def run(client, text, reference=None, **extra):
    return client.post("/api/check", data={"mode": "reference", "language": "en",
                                           "reference_text": reference if reference is not None else text[:4000], **extra},
                       files={"submission": ("m.txt", text.encode())})


def quote(client, text, **extra):
    return client.post("/api/quote", data={"mode": "reference", **extra}, files={"submission": ("m.txt", text.encode())}).json()


def payments_on(monkeypatch):
    for k, v in {"PAYMENTS_ENABLED": "1", "PAYMENTS_MODE": "test", "PAYME_MERCHANT_ID": "test-merchant",
                 "PAYME_TEST_KEY": "test-key"}.items():
        monkeypatch.setenv(k, v)


def top_up(user_id, amount):
    with payments._lock, payments.closing(payments._connect()) as db:
        db.execute("INSERT INTO ledger (user, time, amount_uzs, kind, ref, note, mode) VALUES (?, ?, ?, 'topup', 't', "
                   "'test top-up', 'test')", (user_id, payments._ms(), amount))


# ------------------------------------------------------------- prices (Part 3) and word boundaries

def test_price_table_and_recheck_prices(site):
    tiers = {t["id"]: (t["max_words"], t["price_uzs"]) for t in pricing.tiers()}
    assert tiers == {"free": (1000, 0), "student": (3500, 15000), "researcher": (10000, 25000),
                     "extended": (25000, 39000), "masters": (40000, 49000), "phd": (150000, 79000)}
    assert [pricing.recheck_price(p) for _, p in list(tiers.values())[1:]] == [12000, 20000, 31200, 39200, 63200]
    config = site.get("/api/pricing").json()
    assert config["recommended"] == "researcher" and config["trial_max_words"] == 1000
    assert "packages" not in config and config["payments_enabled"] is False
    assert [t["recheck_uzs"] for t in config["tiers"]] == [None, 12000, 20000, 31200, 39200, 63200]


@pytest.mark.parametrize("words,tier,price", [
    (1000, "free", 0), (1001, "student", 15000), (3500, "student", 15000), (3501, "researcher", 25000),
    (10000, "researcher", 25000), (10001, "extended", 39000), (25000, "extended", 39000), (25001, "masters", 49000),
    (40000, "masters", 49000), (40001, "phd", 79000), (150000, "phd", 79000), (150001, "contact", None)])
def test_every_word_boundary(words, tier, price):
    q = pricing.quote(words)
    assert (q["id"], q["price_uzs"]) == (tier, price)
    paid = pricing.paid_quote(words)
    assert paid["id"] == ("student" if words <= 3500 else tier)


def test_word_count_of_uploaded_file_at_boundaries(site):
    for words in (1000, 1001, 3500, 3501, 150000):
        q = quote(site, doc(words))
        assert q["words"] == words
        assert q["trial_fits"] is (words <= 1000)
    assert quote(site, doc(150001))["fits"] is False


# ------------------------------------------------------------- sign-in required, free trial (Part 2)

def test_every_check_needs_sign_in(site):
    r = run(site, doc(200))
    assert r.status_code == 401 and r.json()["code"] == "login_required"


def test_free_trial_once_then_blocked_in_pilot(site):
    sign_in(site)
    text = doc(400)
    first = run(site, text)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["entitlement"]["kind"] == "trial" and body["entitlement"]["exports"] is False
    assert body["plan"]["id"] == "free"
    rid = body["report_id"]
    for url in (f"/api/report/{rid}.pdf", f"/api/report/{rid}.pdf?full=1", f"/api/report/{rid}/sources.csv"):
        denied = site.get(url)
        assert denied.status_code == 403 and denied.json()["code"] == "trial_no_exports"
    assert site.get(f"/api/report/{rid}").status_code == 200             # on-screen result can be reopened
    second = run(site, doc(300, seed=2))
    assert second.status_code == 402 and second.json()["code"] == "trial_used"
    assert quote(site, doc(300, seed=2))["decision"]["kind"] == "blocked"


def test_trial_word_limit_1000_and_1001(site):
    sign_in(site)
    too_long = run(site, doc(1001))
    assert too_long.status_code == 402 and too_long.json()["code"] == "trial_too_long"
    assert quote(site, doc(1001))["decision"]["future_price_uzs"] == 15000
    ok = run(site, doc(1000))
    assert ok.status_code == 200 and ok.json()["entitlement"]["kind"] == "trial"


def test_trial_excludes_ai_detector_and_archive(site):
    sign_in(site)
    q = quote(site, doc(300), ai_check="true")
    assert q["decision"]["kind"] == "blocked" and q["decision"]["reason"] == "trial_no_extras"


def test_failed_check_releases_trial(site, monkeypatch):
    sign_in(site)

    async def broken(*a, **k):
        raise web.ApiError(503, "engine_unavailable", "down")
    monkeypatch.setattr(web, "analyse", broken)
    assert run(site, doc(300)).status_code == 503
    monkeypatch.setattr(web, "analyse", ORIGINAL_ANALYSE)
    assert run(site, doc(300)).json()["entitlement"]["kind"] == "trial"


def test_nothing_compared_keeps_trial(site, monkeypatch):
    sign_in(site)

    async def nothing(*a, **k):
        return {"mode": "reference", "percent": None, "sources": [], "total_tokens": 300, "status": "failed_search",
                "not_evaluated_reason": "provider_unavailable"}
    monkeypatch.setattr(web, "analyse", nothing)
    r = run(site, doc(300))
    assert r.status_code == 200 and r.json()["entitlement"]["consumed"] is False
    monkeypatch.setattr(web, "analyse", ORIGINAL_ANALYSE)
    assert run(site, doc(300)).json()["entitlement"]["kind"] == "trial"


def test_concurrent_trial_requests_reserve_once(site):
    user = {"id": 77, "name": "T", "username": ""}
    decision = entitlements.decide(user, 300, doc(300))
    assert decision["kind"] == "trial"
    outcomes = []

    def take(i):
        try:
            entitlements.reserve(user, f"job{i}", decision)
            outcomes.append("ok")
        except entitlements.EntitlementError as exc:
            outcomes.append(exc.code)
    threads = [threading.Thread(target=take, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1 and outcomes.count("trial_used") == 11


def test_trial_survives_cookies_devices_and_account_deletion(site):
    sign_in(site)
    assert run(site, doc(300)).status_code == 200
    other_device = TestClient(web.app, base_url="https://plagiq.example")        # no cookies at all
    sign_in(other_device)
    assert run(other_device, doc(300, seed=3)).json()["code"] == "trial_used"
    assert other_device.delete("/api/me").json()["deleted"] is True
    sign_in(other_device)                                                    # same Telegram account, new sign-up
    assert run(other_device, doc(300, seed=4)).json()["code"] == "trial_used"


def test_stale_reservations_released_at_start_up(site):
    user = {"id": 78, "name": "T", "username": ""}
    entitlements.reserve(user, "crashed", entitlements.decide(user, 300, doc(300)))
    assert entitlements.trial_state(user) == "reserved"
    assert entitlements.release_interrupted() == 1
    assert entitlements.trial_state(user) == "available"


# ------------------------------------------------------------- pilot allowances (Part 9)

def test_pilot_allowance_gives_full_check_and_runs_out(site):
    sign_in(site)
    assert run(site, doc(300)).status_code == 200                           # the trial
    entitlements.grant(42, 1, 5000, 7, "tester")
    q = quote(site, doc(3000, seed=5))
    assert q["decision"]["kind"] == "pilot" and q["decision"]["future_price_uzs"] == 15000 and q["decision"]["price_uzs"] == 0
    r = run(site, doc(3000, seed=5))
    assert r.status_code == 200 and r.json()["entitlement"]["kind"] == "pilot" and r.json()["entitlement"]["exports"]
    assert site.get(f"/api/report/{r.json()['report_id']}.pdf").status_code == 200
    assert run(site, doc(3000, seed=6)).json()["code"] == "trial_used"     # allowance used up; no unlimited checks
    with payments._lock, payments.closing(payments._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0  # no fictitious transactions


def test_pilot_allowance_word_limit_and_revoke(site):
    sign_in(site)
    run(site, doc(300))
    gid = entitlements.grant(42, 5, 2000, 7)
    assert quote(site, doc(2500))["decision"]["kind"] == "blocked"
    assert quote(site, doc(1500))["decision"]["kind"] == "pilot"
    entitlements.revoke(gid)
    assert quote(site, doc(1500))["decision"]["kind"] == "blocked"


# ------------------------------------------------------------- rechecks (Part 4)

def test_fingerprint_similarity_lenient_for_revisions_strict_for_other_papers():
    base = doc(3000)
    fp = entitlements.fingerprint(base)
    assert entitlements.similarity(fp, entitlements.fingerprint(revise(base, 0.3))) >= entitlements.SIMILARITY
    assert entitlements.similarity(fp, entitlements.fingerprint(doc(3000, seed=99))) < 0.1


def test_recheck_pilot_flow(site):
    sign_in(site)
    run(site, doc(300))                                                     # trial
    entitlements.grant(42, 1, 10000, 7)
    base = doc(3000, seed=11)
    assert run(site, base).json()["entitlement"]["kind"] == "pilot"
    for n in (1, 2):
        revised = revise(base, 0.2, seed=n)
        q = quote(site, revised)["decision"]
        assert q["kind"] == "recheck" and q["future_price_uzs"] == 12000 and q["recheck"]["remaining"] == 3 - n
        r = run(site, revised)
        assert r.status_code == 200 and r.json()["entitlement"]["kind"] == "recheck"
    third = quote(site, revise(base, 0.2, seed=3))["decision"]
    assert third["kind"] == "blocked"                                       # at most two rechecks
    mine = site.get("/api/my-checks").json()
    root = [c for c in mine["checks"] if c["kind"] == "pilot"][0]
    assert root["rechecks_left"] == 0 and root["recheck_until"]


def test_recheck_of_different_document_or_free_trial_not_eligible(site):
    sign_in(site)
    trial_text = doc(800, seed=21)
    run(site, trial_text)
    entitlements.grant(42, 1, 10000, 7)
    assert quote(site, revise(trial_text, 0.1))["decision"]["kind"] == "pilot"     # a free trial is no recheck root
    run(site, doc(3000, seed=22))
    entitlements.grant(42, 1, 10000, 7)
    assert quote(site, doc(3000, seed=23))["decision"]["kind"] == "pilot"


def test_failed_recheck_consumes_nothing(site, monkeypatch):
    sign_in(site)
    run(site, doc(300))
    entitlements.grant(42, 1, 10000, 7)
    base = doc(3000, seed=31)
    run(site, base)

    async def broken(*a, **k):
        raise web.ApiError(500, "internal_error", "boom")
    monkeypatch.setattr(web, "analyse", broken)
    assert run(site, revise(base, 0.2)).status_code == 500
    monkeypatch.setattr(web, "analyse", ORIGINAL_ANALYSE)
    assert quote(site, revise(base, 0.2))["decision"]["recheck"]["remaining"] == 2


def test_recheck_expires_after_30_days(site, monkeypatch):
    sign_in(site)
    run(site, doc(300))
    entitlements.grant(42, 1, 10000, 7)
    base = doc(3000, seed=41)
    run(site, base)
    assert quote(site, revise(base, 0.1))["decision"]["kind"] == "recheck"
    with entitlements.closing(entitlements._connect()) as db:              # 31 days later
        db.execute("UPDATE paid_checks SET expires=expires-31*86400, created=created-31*86400")
    assert quote(site, revise(base, 0.1))["decision"]["kind"] == "blocked"
    assert entitlements.purge() == 1                                        # the fingerprint is dropped


def test_recheck_other_account_not_eligible(site):
    sign_in(site)
    run(site, doc(300))
    entitlements.grant(42, 1, 10000, 7)
    base = doc(3000, seed=51)
    run(site, base)
    other = TestClient(web.app, base_url="https://plagiq.example")
    sign_in(other, user_id=43)
    run(other, doc(300, seed=52))
    entitlements.grant(43, 1, 10000, 7)
    assert quote(other, revise(base, 0.1))["decision"]["kind"] == "pilot"


# ------------------------------------------------------------- payments on (TEST mode, balance only)

def test_paid_check_price_confirmation_and_recheck_discount(site, monkeypatch):
    payments_on(monkeypatch)
    sign_in(site)
    run(site, doc(300))                                                     # trial is free even with payments on
    top_up(42, 100000)
    base = doc(3000, seed=61)
    q = quote(site, base)["decision"]
    assert q["kind"] == "paid" and q["total_uzs"] == 15000
    assert run(site, base).json()["code"] == "price_changed"                 # never charged without the shown price
    paid = run(site, base, expected_price="15000")
    assert paid.status_code == 200 and paid.json()["charged_uzs"] == 15000
    assert payments.balance({"id": 42}) == 85000
    revised = revise(base, 0.2)
    q = quote(site, revised)["decision"]
    assert q["kind"] == "recheck" and q["total_uzs"] == 12000
    assert run(site, revised, expected_price="12000").json()["charged_uzs"] == 12000
    grown = base + " " + doc(1500, seed=62)                                # 4,500 words: now Researcher
    q = quote(site, grown)["decision"]
    assert q["kind"] == "paid" and q["total_uzs"] == 25000 and q["recheck_upgrade"]["new_tier"] == "researcher"
    assert run(site, grown, expected_price="12000").json()["code"] == "price_changed"


def test_paid_check_failure_refunds_and_insufficient_balance(site, monkeypatch):
    payments_on(monkeypatch)
    sign_in(site)
    run(site, doc(300))
    assert run(site, doc(3000), expected_price="15000").json()["code"] == "insufficient_balance"
    top_up(42, 15000)

    async def broken(*a, **k):
        raise web.ApiError(503, "engine_unavailable", "down")
    monkeypatch.setattr(web, "analyse", broken)
    assert run(site, doc(3000), expected_price="15000").status_code == 503
    assert payments.balance({"id": 42}) == 15000                             # given back


# ------------------------------------------------------------- passwordless e-mail (Part 1)

def code_from_outbox():
    import re
    return re.search(r"\b(\d{6})\b", mailer.OUTBOX[-1]["subject"]).group(1)


def test_email_code_sign_in_creates_account_once(site):
    assert site.get("/api/me").json()["auth"]["email_codes"] is True
    r = site.post("/auth/email/start", json={"email": "New.Student@Example.uz"})
    assert r.json() == {"sent": True, "minutes": 15}
    code = code_from_outbox()
    ok = site.post("/auth/email/verify", json={"email": "new.student@example.uz", "code": code})
    assert ok.status_code == 200 and ok.json()["user"]["id"] < 0
    again = site.post("/auth/email/verify", json={"email": "new.student@example.uz", "code": code})
    assert again.status_code == 400 and again.json()["code"] == "code_invalid"          # single use


def test_email_code_wrong_attempts_and_expiry(site, monkeypatch):
    site.post("/auth/email/start", json={"email": "a@example.uz"})
    code = code_from_outbox()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(auth.CODE_ATTEMPTS):
        assert site.post("/auth/email/verify", json={"email": "a@example.uz", "code": wrong}).status_code == 400
    assert site.post("/auth/email/verify", json={"email": "a@example.uz", "code": code}).status_code == 400  # void now
    site.post("/auth/email/start", json={"email": "b@example.uz"})
    code = code_from_outbox()
    real = time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + 16 * 60)
    assert site.post("/auth/email/verify", json={"email": "b@example.uz", "code": code}).status_code == 400


def test_email_no_enumeration_and_rate_limit(site):
    auth.register_email("old@example.uz", "long-password-1")
    a = site.post("/auth/email/start", json={"email": "old@example.uz"})
    b = site.post("/auth/email/start", json={"email": "nobody@example.uz"})
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    for _ in range(2):
        site.post("/auth/email/start", json={"email": "old@example.uz"})
    assert site.post("/auth/email/start", json={"email": "old@example.uz"}).status_code == 429


def test_existing_password_account_signs_in_by_code_to_same_account(site):
    old = auth.register_email("kept@example.uz", "long-password-1")
    site.post("/auth/email/start", json={"email": "kept@example.uz"})
    me = site.post("/auth/email/verify", json={"email": "kept@example.uz", "code": code_from_outbox()}).json()["user"]
    assert me["id"] == old["id"]


def test_email_link_signs_in_once(site):
    import re
    site.post("/auth/email/start", json={"email": "link@example.uz"})
    token = re.search(r"/auth/email/link\?t=(\S+)", mailer.OUTBOX[-1]["text"]).group(1)
    first = site.get(f"/auth/email/link?t={token}", follow_redirects=False)
    assert first.status_code == 200 and auth.COOKIE in first.headers.get("set-cookie", "")
    assert site.get(f"/auth/email/link?t={token}").status_code == 410


def test_password_registration_closed_and_codes_off_without_tested_smtp(site, monkeypatch):
    assert site.post("/auth/register", json={"email": "x@example.uz", "password": "long-password-1"}).status_code == 410
    monkeypatch.delenv("MAIL_BACKEND")
    config = site.get("/api/me").json()["auth"]
    assert config["email_codes"] is False and config["email_password"] is True
    assert site.post("/auth/email/start", json={"email": "x@example.uz"}).status_code == 503
    monkeypatch.setenv("SMTP_HOST", "smtp.example.uz"); monkeypatch.setenv("SMTP_FROM", "PlagIQ <no-reply@example.uz>")
    assert site.get("/api/me").json()["auth"]["email_codes"] is False        # not before the owner tested delivery
    monkeypatch.setenv("EMAIL_SIGNIN", "1")
    assert site.get("/api/me").json()["auth"]["email_codes"] is True


# ------------------------------------------------------------- linked sign-in methods share one trial

def test_linked_methods_share_one_trial(site):
    sign_in(site)                                                           # Telegram 42
    assert site.post("/auth/email/start", json={"email": "linked@example.uz", "link": True}).status_code == 200
    linked = site.post("/auth/email/verify", json={"email": "linked@example.uz", "code": code_from_outbox()})
    assert linked.json() == {"linked": "email"}
    assert site.get("/api/me").json()["methods"] == {"telegram": True, "email": "linked@example.uz"}
    assert run(site, doc(300)).status_code == 200                           # trial used via Telegram
    other = TestClient(web.app, base_url="https://plagiq.example")
    other.post("/auth/email/start", json={"email": "linked@example.uz"})
    me = other.post("/auth/email/verify", json={"email": "linked@example.uz", "code": code_from_outbox()}).json()["user"]
    assert me["id"] == 42                                                   # the e-mail opens the same account
    assert run(other, doc(300, seed=2)).json()["code"] == "trial_used"


def test_cannot_link_identity_of_another_account_with_history(site):
    other = TestClient(web.app, base_url="https://plagiq.example")
    other.post("/auth/email/start", json={"email": "busy@example.uz"})
    other.post("/auth/email/verify", json={"email": "busy@example.uz", "code": code_from_outbox()})
    assert run(other, doc(300)).status_code == 200
    sign_in(site)
    site.post("/auth/email/start", json={"email": "busy@example.uz", "link": True})
    r = site.post("/auth/email/verify", json={"email": "busy@example.uz", "code": code_from_outbox()})
    assert r.status_code == 409 and r.json()["code"] == "identity_in_use"


# ------------------------------------------------------------- My checks (Part 8)

def test_my_checks_dashboard_and_access_control(site):
    sign_in(site)
    rid = run(site, doc(300)).json()["report_id"]
    mine = site.get("/api/my-checks").json()
    assert mine["trial"] == "used" and mine["retention"]["report_hours"] == 24
    row = mine["checks"][0]
    assert row["kind"] == "trial" and row["payment"] == "free" and row["reports"]["pdf"] is False
    assert row["report_id"] == rid and row["words"] == 300
    stranger = TestClient(web.app, base_url="https://plagiq.example")
    sign_in(stranger, user_id=99)
    assert stranger.get("/api/my-checks").json()["checks"] == []
    assert stranger.get(f"/api/report/{rid}").status_code == 404
    assert TestClient(web.app).get("/api/my-checks").status_code == 401


def test_admin_grants_owner_only(site, monkeypatch):
    sign_in(site)
    assert site.post("/admin/grants", data={"user_id": "42", "checks": "2"}).status_code == 404
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "42")
    page = site.post("/admin/grants", data={"user_id": "42", "checks": "2", "max_words": "5000", "days": "7"})
    assert page.status_code == 200 and "Granted" in page.text
    assert entitlements.grants_for(42)[0]["remaining"] == 2
    assert "Granted" not in site.post("/admin/grants", data={"user_id": "42", "checks": "500"}).text


def test_sign_in_mail_failure_is_reported_not_hidden(site, monkeypatch):
    def down(*a, **k):
        raise mailer.MailError("TimeoutError: timed out")
    monkeypatch.setattr(mailer, "send", down)
    for address in ("old@example.uz", "nobody@example.uz"):        # the same answer whether or not an account exists
        r = site.post("/auth/email/start", json={"email": address})
        assert r.status_code == 503 and r.json()["code"] == "mail_failed"
