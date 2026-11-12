"""Payments in test mode: Payme Merchant API and Click SHOP API scenarios (simulated, no network),
balance top-up, per-check charging and refunds."""
import base64
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import payments
from test_auth_ai import EN, TOKEN, sign_in

KEY = "sandbox-test-key-not-real"
CLICK_SECRET = "click-sandbox-secret"


@pytest.fixture()
def pay(monkeypatch, tmp_path):
    for name, value in {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_BOT_USERNAME": "plagiq_bot",
                        "USERS_DB": str(tmp_path / "users.sqlite3"), "PAYMENTS_ENABLED": "1", "PAYMENTS_MODE": "test",
                        "PAYME_MERCHANT_ID": "5e730e8e0b852a417aa49ceb", "PAYME_TEST_KEY": KEY,
                        "CLICK_SERVICE_ID": "101", "CLICK_MERCHANT_ID": "202", "CLICK_SECRET_KEY": CLICK_SECRET,
                        "PUBLIC_ORIGIN": "https://plagiq.example"}.items():
        monkeypatch.setenv(name, value)
    for name in ("LOGIN_REQUIRED", "SESSION_SECRET", "PRIVATE_PILOT_USER", "PRIVATE_PILOT_PASSWORD", "PAYME_IKPU", "PAYME_PACKAGE_CODE"):
        monkeypatch.delenv(name, raising=False)
    web.RATE.clear(); web.results.clear()
    return TestClient(web.app, base_url="https://plagiq.example")


def rpc(client, method, params, key=KEY, request_id=1):
    headers = {"Authorization": "Basic " + base64.b64encode(f"Paycom:{key}".encode()).decode()} if key else {}
    r = client.post("/payments/payme", content=json.dumps({"method": method, "params": params, "id": request_id}),
                    headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 200                    # Payme requires HTTP 200 for every answer
    return r.json()


def new_order(client, provider="payme", amount=20000, **extra):
    r = client.post("/api/payments/order", json={"provider": provider, "amount_uzs": amount, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def balance(client):
    return client.get("/api/payments").json()["account"]["balance_uzs"]


# ------------------------------------------------------------- settings

def test_off_unless_test_mode(monkeypatch, pay):
    assert payments.enabled()
    monkeypatch.setenv("PAYMENTS_MODE", "production")
    assert not payments.enabled() and "test" in payments.problem()
    monkeypatch.setenv("PAYMENTS_MODE", "test")
    monkeypatch.setenv("PAYMENTS_ENABLED", "0")
    assert not payments.enabled()
    assert pay.get("/api/payments").json()["enabled"] is False


def test_checkout_urls_are_sandbox(pay):
    sign_in(pay)
    order = new_order(pay)
    assert order["pay_url"].startswith("https://test.paycom.uz/")
    decoded = base64.b64decode(order["pay_url"][len("https://test.paycom.uz/"):]).decode()
    assert decoded == (f"m=5e730e8e0b852a417aa49ceb;ac.order_id={order['id']};a=2000000;l=uz;"
                       f"c=https://plagiq.example/?paid={order['id']}")
    click = new_order(pay, "click", 15000)
    assert click["pay_url"].startswith("https://my.click.uz/services/pay?service_id=101&merchant_id=202&amount=15000"
                                       f"&transaction_param={click['id']}")


def test_order_needs_login_and_valid_amount(pay):
    assert pay.post("/api/payments/order", json={"provider": "payme", "amount_uzs": 5000}).status_code == 401
    sign_in(pay)
    assert pay.post("/api/payments/order", json={"provider": "payme", "amount_uzs": 10}).json()["code"] == "invalid_amount"
    assert pay.post("/api/payments/order", json={"provider": "uzum", "amount_uzs": 5000}).json()["code"] == "provider_unavailable"


# ------------------------------------------------------------- Payme sandbox scenarios

def test_payme_wrong_key_and_bad_json(pay):
    assert rpc(pay, "CheckPerformTransaction", {}, key="wrong")["error"]["code"] == -32504
    assert rpc(pay, "CheckPerformTransaction", {}, key=None)["error"]["code"] == -32504
    r = pay.post("/payments/payme", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["error"]["code"] == -32700
    error = rpc(pay, "NoSuchMethod", {})["error"]
    assert error["code"] == -32601 and set(error["message"]) == {"ru", "uz", "en"}


def test_payme_wrong_amount_and_unknown_order(pay):
    sign_in(pay)
    order = new_order(pay)
    for method in ("CheckPerformTransaction", "CreateTransaction"):
        wrong = rpc(pay, method, {"id": "a" * 24, "time": 1, "amount": 1, "account": {"order_id": order["id"]}})
        assert wrong["error"]["code"] == -31001
        missing = rpc(pay, method, {"id": "a" * 24, "time": 1, "amount": 2000000, "account": {"order_id": "999"}})
        assert -31099 <= missing["error"]["code"] <= -31050 and missing["error"]["data"] == "order_id"


def test_payme_full_cycle_credits_balance_once(pay):
    sign_in(pay)
    order = new_order(pay)
    params = {"amount": 2000000, "account": {"order_id": order["id"]}}
    assert rpc(pay, "CheckPerformTransaction", params)["result"] == {"allow": True}
    tx = "5305e3bab097f420a62ced0b"
    created = rpc(pay, "CreateTransaction", {**params, "id": tx, "time": int(time.time() * 1000)})["result"]
    assert created["state"] == 1 and created["transaction"] == tx
    assert rpc(pay, "CreateTransaction", {**params, "id": tx, "time": 1})["result"] == created     # repeat: same answer
    other = rpc(pay, "CreateTransaction", {**params, "id": "b" * 24, "time": 2})
    assert other["error"]["code"] == -31008                    # order already awaiting payment
    performed = rpc(pay, "PerformTransaction", {"id": tx})["result"]
    assert performed["state"] == 2
    assert rpc(pay, "PerformTransaction", {"id": tx})["result"] == performed                        # repeat
    assert balance(pay) == 20000
    check = rpc(pay, "CheckTransaction", {"id": tx})["result"]
    assert check["state"] == 2 and check["perform_time"] == performed["perform_time"] and check["reason"] is None
    statement = rpc(pay, "GetStatement", {"from": 0, "to": int(time.time() * 1000) + 1000})["result"]["transactions"]
    assert [t["id"] for t in statement] == [tx] and statement[0]["account"] == {"order_id": order["id"]}
    assert rpc(pay, "CheckPerformTransaction", params)["error"]["code"] == -31051                  # already paid
    # Cancelling a performed top-up takes the credit back while it is unspent.
    cancelled = rpc(pay, "CancelTransaction", {"id": tx, "reason": 5})["result"]
    assert cancelled["state"] == -2
    assert rpc(pay, "CancelTransaction", {"id": tx, "reason": 5})["result"] == cancelled           # repeat
    assert balance(pay) == 0
    assert rpc(pay, "CheckTransaction", {"id": tx})["result"]["reason"] == 5
    receipt = pay.get(f"/payments/receipt/{order['id']}")
    assert receipt.status_code == 200 and "TEST PAYMENT" in receipt.text and "refunded" in receipt.text


def test_payme_cancel_created_and_unknown(pay):
    sign_in(pay)
    order = new_order(pay)
    params = {"amount": 2000000, "account": {"order_id": order["id"]}, "id": "c" * 24, "time": 5}
    rpc(pay, "CreateTransaction", params)
    result = rpc(pay, "CancelTransaction", {"id": "c" * 24, "reason": 3})["result"]
    assert result["state"] == -1
    assert rpc(pay, "PerformTransaction", {"id": "c" * 24})["error"]["code"] == -31008
    assert rpc(pay, "CancelTransaction", {"id": "d" * 24, "reason": 3})["error"]["code"] == -31003
    assert rpc(pay, "CheckTransaction", {"id": "d" * 24})["error"]["code"] == -31003
    assert balance(pay) == 0


def test_payme_cannot_cancel_spent_top_up(pay):
    sign_in(pay)
    order = new_order(pay, amount=15000)
    params = {"amount": 1500000, "account": {"order_id": order["id"]}, "id": "e" * 24, "time": 5}
    rpc(pay, "CreateTransaction", params)
    rpc(pay, "PerformTransaction", {"id": "e" * 24})
    pay.post("/api/check", data={"language": "en", "mode": "reference", "reference_text": EN},
             files={"submission": ("a.txt", (EN * 7).encode(), "text/plain")})      # spends 15,000 (Researcher)
    assert balance(pay) == 0
    assert rpc(pay, "CancelTransaction", {"id": "e" * 24, "reason": 5})["error"]["code"] == -31007


def test_payme_timeout_cancels_after_12_hours(pay, monkeypatch):
    sign_in(pay)
    order = new_order(pay)
    params = {"amount": 2000000, "account": {"order_id": order["id"]}, "id": "f" * 24, "time": 5}
    rpc(pay, "CreateTransaction", params)
    later = time.time() + 12 * 3600 + 60
    monkeypatch.setattr(payments.time, "time", lambda: later)
    assert rpc(pay, "PerformTransaction", {"id": "f" * 24})["error"]["code"] == -31008
    check = rpc(pay, "CheckTransaction", {"id": "f" * 24})["result"]
    assert check["state"] == -1 and check["reason"] == 4


def test_payme_fiscal_detail_only_when_configured(pay, monkeypatch):
    sign_in(pay)
    order = new_order(pay)
    params = {"amount": 2000000, "account": {"order_id": order["id"]}}
    assert "detail" not in rpc(pay, "CheckPerformTransaction", params)["result"]
    monkeypatch.setenv("PAYME_IKPU", "12345678901234567")
    monkeypatch.setenv("PAYME_PACKAGE_CODE", "000000")    # dummy test values, not real codes
    item = rpc(pay, "CheckPerformTransaction", params)["result"]["detail"]["items"][0]
    assert item["code"] == "12345678901234567" and item["price"] == 2000000 and item["count"] == 1


# ------------------------------------------------------------- Click

def click_form(order_id, amount, action, prepare_id=None, secret=CLICK_SECRET, error=0, trans="777"):
    form = {"click_trans_id": trans, "service_id": "101", "click_paydoc_id": "888", "merchant_trans_id": order_id,
            "amount": amount, "action": str(action), "error": str(error), "error_note": "Success",
            "sign_time": "2026-11-12 10:00:00"}
    if prepare_id is not None:
        form["merchant_prepare_id"] = str(prepare_id)
    form["sign_string"] = payments.click_sign(form, secret, prepare_id is not None)
    return form


def test_click_sign_matches_documented_formula():
    form = {"click_trans_id": "1", "service_id": "2", "merchant_trans_id": "3", "merchant_prepare_id": "4",
            "amount": "1000", "action": "1", "sign_time": "2026-11-12 10:00:00"}
    assert payments.click_sign(form, "S", True) == hashlib.md5(b"12S3410001" + b"2026-11-12 10:00:00").hexdigest()
    assert payments.click_sign(form, "S", False) == hashlib.md5(b"12S310001" + b"2026-11-12 10:00:00").hexdigest()


def test_click_prepare_complete(pay):
    sign_in(pay)
    order = new_order(pay, "click", 15000)
    bad = pay.post("/payments/click/prepare", data=click_form(order["id"], "15000", 0, secret="wrong")).json()
    assert bad["error"] == -1
    assert pay.post("/payments/click/prepare", data=click_form(order["id"], "100", 0)).json()["error"] == -2
    assert pay.post("/payments/click/prepare", data=click_form("123", "15000", 0)).json()["error"] == -5
    assert pay.post("/payments/click/prepare", data=click_form(order["id"], "15000", 1)).json()["error"] == -3
    prepared = pay.post("/payments/click/prepare", data=click_form(order["id"], "15000.00", 0)).json()
    assert prepared["error"] == 0 and prepared["merchant_trans_id"] == order["id"]
    prepare_id = prepared["merchant_prepare_id"]
    assert pay.post("/payments/click/complete", data=click_form(order["id"], "15000.00", 1, 99999)).json()["error"] == -6
    done = pay.post("/payments/click/complete", data=click_form(order["id"], "15000.00", 1, prepare_id)).json()
    assert done == {"error": 0, "error_note": "Success", "click_trans_id": "777", "merchant_trans_id": order["id"],
                    "merchant_confirm_id": prepare_id}
    assert pay.post("/payments/click/complete", data=click_form(order["id"], "15000.00", 1, prepare_id)).json()["error"] == -4
    assert balance(pay) == 15000
    assert "Click" in pay.get(f"/payments/receipt/{order['id']}").text


def test_click_failed_payment_is_cancelled(pay):
    sign_in(pay)
    order = new_order(pay, "click", 15000)
    prepare_id = pay.post("/payments/click/prepare", data=click_form(order["id"], "15000", 0)).json()["merchant_prepare_id"]
    failed = pay.post("/payments/click/complete", data=click_form(order["id"], "15000", 1, prepare_id, error=-5017)).json()
    assert failed["error"] == -9
    assert pay.post("/payments/click/complete", data=click_form(order["id"], "15000", 1, prepare_id)).json()["error"] == -9
    assert balance(pay) == 0


def test_order_for_one_provider_only(pay):
    sign_in(pay)
    order = new_order(pay, "click", 15000)
    params = {"amount": 1500000, "account": {"order_id": order["id"]}}
    assert rpc(pay, "CheckPerformTransaction", params)["error"]["code"] == -31050


# ------------------------------------------------------------- charging checks

def top_up(client, amount):
    order = new_order(client, amount=amount)
    tx = order["id"].rjust(24, "0")
    rpc(client, "CreateTransaction", {"id": tx, "time": 1, "amount": amount * 100, "account": {"order_id": order["id"]}})
    rpc(client, "PerformTransaction", {"id": tx})


def run_check(client, text, mode="reference"):
    return client.post("/api/check", data={"language": "en", "mode": mode, "reference_text": EN},
                       files={"submission": ("a.txt", text.encode(), "text/plain")})


def test_free_check_needs_no_balance(pay):
    sign_in(pay)
    r = run_check(pay, EN)                         # under 1,000 words: Free plan
    assert r.status_code == 200 and r.json().get("charged_uzs") in (None, 0)


def test_paid_check_charges_and_low_balance_refused(pay):
    sign_in(pay)
    long_text = EN * 7                             # over 1,000 words: Researcher plan, 15,000 so'm
    refused = run_check(pay, long_text)
    assert refused.status_code == 402 and refused.json()["code"] == "insufficient_balance"
    top_up(pay, 20000)
    r = run_check(pay, long_text)
    assert r.status_code == 200 and r.json()["charged_uzs"] == 15000
    assert balance(pay) == 5000
    ledger = pay.get("/api/payments").json()["account"]["ledger"]
    assert [e["kind"] for e in ledger] == ["charge", "topup"] and ledger[0]["ref"]           # linked to the report


def test_failed_check_is_refunded(pay):
    sign_in(pay)
    top_up(pay, 20000)
    r = pay.post("/api/check", data={"language": "en", "mode": "reference"},                 # no reference: fails
                 files={"submission": ("a.txt", (EN * 7).encode(), "text/plain")})
    assert r.status_code == 400
    assert balance(pay) == 20000
    kinds = [e["kind"] for e in pay.get("/api/payments").json()["account"]["ledger"]]
    assert kinds == ["refund", "charge", "topup"]


def test_payments_off_keeps_checks_free(pay, monkeypatch):
    monkeypatch.setenv("PAYMENTS_ENABLED", "0")
    sign_in(pay)
    r = run_check(pay, EN * 7)
    assert r.status_code == 200 and "charged_uzs" not in r.json()


def test_callbacks_bypass_pilot_password_but_site_does_not(pay, monkeypatch):
    monkeypatch.setenv("PRIVATE_PILOT_USER", "pilot")
    monkeypatch.setenv("PRIVATE_PILOT_PASSWORD", "a-long-pilot-password")
    assert pay.get("/api/payments").status_code == 401
    assert rpc(pay, "CheckTransaction", {"id": "x" * 24})["error"]["code"] == -31003
    assert pay.post("/payments/click/prepare", data={}).json()["error"] == -8


def test_receipt_only_for_owner(pay):
    sign_in(pay)
    top_up(pay, 20000)
    order_id = pay.get("/api/payments").json()["account"]["orders"][0]["id"]
    assert pay.get(f"/payments/receipt/{order_id}").status_code == 200
    sign_in(pay, user_id=43)
    assert pay.get(f"/payments/receipt/{order_id}").status_code == 404
    assert pay.get(f"/api/payments/order/{order_id}").status_code == 404


def test_packages_credit_bonus(pay):
    sign_in(pay)
    options = pay.get("/api/payments").json()["packages"]
    first = next(p for p in options if p["tier"] == "researcher" and p["checks"] == 10)
    assert first["pay_uzs"] == 150000 and first["credit_uzs"] == 180000
    order = new_order(pay, package=first["id"], amount=None)
    assert order["pay_uzs"] == 150000 and order["credit_uzs"] == 180000


# ------------------------------------------------------------- erasure

def test_delete_account_erases_payment_records(pay):
    sign_in(pay)
    top_up(pay, 20000)
    run_check(pay, EN * 7)
    click_order = new_order(pay, "click", 15000)
    assert balance(pay) == 5000
    assert pay.delete("/api/me").status_code == 200
    import sqlite3
    with sqlite3.connect(auth.db_file()) as db:
        for table in ("orders", "ledger", "payme_tx", "click_tx"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    sign_in(pay)                                   # same Telegram ID signs in again: nothing is left
    account = pay.get("/api/payments").json()["account"]
    assert account == {"balance_uzs": 0, "ledger": [], "orders": []}
    assert pay.post("/payments/click/prepare", data=click_form(click_order["id"], "15000", 0)).json()["error"] == -5


def test_delete_refused_while_payment_in_progress(pay):
    sign_in(pay)
    order = new_order(pay)
    rpc(pay, "CreateTransaction", {"id": "g" * 24, "time": 1, "amount": 2000000, "account": {"order_id": order["id"]}})
    r = pay.delete("/api/me")
    assert r.status_code == 409 and r.json()["code"] == "payment_in_progress"
    assert pay.get("/api/me").json()["user"]                                     # account kept
    rpc(pay, "CancelTransaction", {"id": "g" * 24, "reason": 3})
    assert pay.delete("/api/me").status_code == 200


def test_delete_without_payments_tables(pay, monkeypatch):
    monkeypatch.setenv("PAYMENTS_ENABLED", "0")
    sign_in(pay)
    assert pay.delete("/api/me").status_code == 200


def test_fiscal_detail_needs_both_codes(pay, monkeypatch):
    sign_in(pay)
    order = new_order(pay)
    params = {"amount": 2000000, "account": {"order_id": order["id"]}}
    monkeypatch.setenv("PAYME_IKPU", "12345678901234567")
    monkeypatch.delenv("PAYME_PACKAGE_CODE", raising=False)
    assert "detail" not in rpc(pay, "CheckPerformTransaction", params)["result"]
