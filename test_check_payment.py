"""One document, one payment (TEST mode, simulated provider calls, no network): the order is for exactly the check's
server-side price, the check runs only after the provider's signed confirmation, never because the browser came
back, and a failed check leaves the payment on the balance."""
import asyncio
import secrets
import sqlite3

import pytest

import app as web
import auth
import entitlements
import payments
import pending_checks
import reports_store
from test_auth_ai import EN, sign_in
from test_payments import balance, pay, rpc, top_up, uzum, uzum_body  # noqa: F401  (fixture)

ORIGINAL = web.analyse
LONG = EN * 7                                   # 1,176 words: Student plan, 15,000 so'm


def start(client, text=LONG, price="15000", provider="payme", request_id=None, **form):
    data = {"language": "en", "mode": "reference", "reference_text": EN, "expected_price": price, "provider": provider,
            "request_id": request_id or secrets.token_hex(16), **form}
    return client.post("/api/check/pay", data=data, files={"submission": ("thesis.txt", text.encode(), "text/plain")})


def payme_pays(client, order_id, amount_uzs, tx="a" * 24):
    params = {"amount": amount_uzs * 100, "account": {"order_id": order_id}, "id": tx, "time": 5}
    assert "result" in rpc(client, "CreateTransaction", params)
    assert "result" in rpc(client, "PerformTransaction", {"id": tx})


def run_now():
    return asyncio.run(web.run_paid_checks(wait=True))


@pytest.fixture()
def signed(pay):
    sign_in(pay)
    with entitlements.closing(entitlements._connect()) as db:            # the free check is already used
        db.execute("INSERT INTO reservations (job, user, kind, state, created) VALUES ('t', 42, 'trial', 'used', 0)")
    return pay


def test_order_is_exact_price_and_nothing_runs_before_payment(signed):
    r = start(signed)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pay_uzs"] == 15000 and body["pay_url"].startswith("https://test.paycom.uz/") and body["state"] == "awaiting"
    order = signed.get(f"/api/payments/order/{body['order_id']}").json()
    assert order["pay_uzs"] == order["credit_uzs"] == 15000 and order["state"] == "pending" and order["purpose"] == "check"
    assert run_now() == 0
    signed.get(f"/?paid={body['order_id']}")                             # the browser coming back changes nothing
    pending = signed.get("/api/check/pending").json()["checks"][0]
    assert pending["state"] == "awaiting" and pending["order_state"] == "pending" and pending["report_id"] is None
    assert balance(signed) == 0 and not reports_store.listing("", 42)


def test_provider_confirmation_runs_the_check_and_charges_once(signed):
    body = start(signed).json()
    payme_pays(signed, body["order_id"], 15000)
    assert balance(signed) == 15000                                     # credited by the signed provider call
    assert run_now() == 1
    pending = signed.get("/api/check/pending").json()["checks"][0]
    assert pending["state"] == "done" and pending["report_id"]
    report = signed.get(f"/api/report/{pending['report_id']}").json()
    assert report["percent"] is not None and report["charged_uzs"] == 15000 and report["entitlement"]["kind"] == "paid"
    assert signed.get(f"/api/report/{pending['report_id']}.pdf").status_code == 200   # a paid check has the PDF
    assert balance(signed) == 0
    kinds = [e["kind"] for e in signed.get("/api/payments").json()["account"]["ledger"]]
    assert kinds == ["charge", "topup"]
    assert run_now() == 0                                               # never runs or charges twice
    with sqlite3.connect(reports_store._path()) as db:                 # the waiting copy of the text is gone
        assert db.execute("SELECT COUNT(*) FROM pending_checks WHERE text IS NOT NULL").fetchone()[0] == 0
    mine = signed.get("/api/my-checks").json()["checks"][0]
    assert mine["kind"] == "paid" and mine["payment"] == "paid" and mine["amount_uzs"] == 15000


def test_same_request_returns_same_order(signed):
    rid = secrets.token_hex(16)
    first, second = start(signed, request_id=rid).json(), start(signed, request_id=rid).json()
    assert first["order_id"] == second["order_id"] and second["repeated"] is True
    assert len(signed.get("/api/payments").json()["account"]["orders"]) == 1


def test_price_must_match_server_and_free_checks_need_no_payment(signed, pay):
    assert start(signed, price="1000").json()["code"] == "price_changed"
    assert start(signed, price="15000", request_id="x" * 32).json()["code"] == "invalid_request"
    assert not signed.get("/api/payments").json()["account"]["orders"]
    fresh = pay.__class__(web.app, base_url="https://plagiq.example")
    sign_in(fresh, user_id=55)                                          # still has the free check
    assert start(fresh, text=EN, price="1").json()["code"] == "no_payment_needed"


def test_failed_check_keeps_payment_on_balance_and_pays_a_rerun(signed, monkeypatch):
    body = start(signed).json()
    payme_pays(signed, body["order_id"], 15000)

    async def broken(*a, **k):
        raise web.ApiError(503, "engine_unavailable", "down")
    monkeypatch.setattr(web, "analyse", broken)
    run_now()
    pending = signed.get("/api/check/pending").json()["checks"][0]
    assert pending["state"] == "failed" and pending["error"] == "engine_unavailable"
    assert balance(signed) == 15000                                     # given back to the balance
    monkeypatch.setattr(web, "analyse", ORIGINAL)
    rerun = signed.post("/api/check", data={"language": "en", "mode": "reference", "reference_text": EN,
                                            "expected_price": "15000"},
                        files={"submission": ("thesis.txt", LONG.encode(), "text/plain")})
    assert rerun.status_code == 200 and rerun.json()["charged_uzs"] == 15000 and balance(signed) == 0


def test_unpaid_check_expires_and_late_payment_stays_on_balance(signed):
    body = start(signed).json()
    import time
    assert pending_checks.purge(time.time() + 7 * 3600) == 1
    pending = signed.get("/api/check/pending").json()["checks"][0]
    assert pending["state"] == "expired"
    payme_pays(signed, body["order_id"], 15000)
    assert run_now() == 0 and balance(signed) == 15000


def test_restart_runs_a_paid_check_again(signed):
    body = start(signed).json()
    payme_pays(signed, body["order_id"], 15000)
    claimed = pending_checks.claim(body["pending_id"])                  # running when the server stopped
    assert claimed and claimed["text"]
    web.recover_interrupted()
    assert run_now() == 1
    assert signed.get("/api/check/pending").json()["checks"][0]["state"] == "done"


def test_recheck_is_paid_at_the_discount(signed):
    first = start(signed).json()
    payme_pays(signed, first["order_id"], 15000)
    run_now()
    revised = LONG.replace("Fergana", "Namangan", 3)
    quote = signed.post("/api/quote", data={"mode": "reference"},
                        files={"submission": ("t.txt", revised.encode(), "text/plain")}).json()["decision"]
    assert quote["kind"] == "recheck" and quote["total_uzs"] == 12000
    second = start(signed, text=revised, price="12000").json()
    payme_pays(signed, second["order_id"], 12000, tx="b" * 24)
    run_now()
    done = signed.get("/api/check/pending").json()["checks"][0]
    assert done["state"] == "done"
    assert signed.get(f"/api/report/{done['report_id']}").json()["entitlement"]["kind"] == "recheck"


def test_uzum_order_number_and_click(signed):
    click = start(signed, provider="click").json()
    assert click["pay_url"].startswith("https://my.click.uz/services/pay") and "amount=15000" in click["pay_url"]
    body = start(signed, provider="uzum").json()
    assert body["pay_url"] is None and body["service_name"] and body["order_id"]
    status, created = uzum(signed, "create", uzum_body(body["order_id"], transId="u1", amount=1500000))
    assert status == 200
    uzum(signed, "confirm", {"serviceId": 101202, "timestamp": 1, "transId": "u1"})
    assert run_now() == 1                                               # only the Uzum order was paid


def test_only_the_owner_sees_pending_and_deletion_removes_them(signed, pay):
    start(signed)
    other = pay.__class__(web.app, base_url="https://plagiq.example")
    sign_in(other, user_id=43)
    assert other.get("/api/check/pending").json()["checks"] == []
    assert pay.__class__(web.app).get("/api/check/pending").status_code == 401
    assert signed.delete("/api/me").status_code == 200
    with sqlite3.connect(reports_store._path()) as db:
        assert db.execute("SELECT COUNT(*) FROM pending_checks").fetchone()[0] == 0


def test_payments_off_refuses(signed, monkeypatch):
    monkeypatch.setenv("PAYMENTS_ENABLED", "0")
    assert start(signed).json()["code"] == "payments_off"
    assert payments.enabled() is False
