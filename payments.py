"""Balance top-up with Payme and Click, and per-check charging. TEST (sandbox) MODE ONLY.

This version accepts only PAYMENTS_MODE=test. With any other value payments stay off, so
no real money can be taken until a later, separately reviewed change enables production.

How it works
  * Each signed-in user has a balance in so'm (a ledger: top-ups +, charges -, refunds +).
  * Top-up: the user picks an amount (or a package) and a provider; PlagIQ creates an order
    and sends the user to the provider's checkout. The provider then calls PlagIQ's
    merchant endpoint, which credits the balance when the payment is completed.
  * A check costs its plan price (pricing.quote) plus the AI-detector price when requested.
    The price is taken from the balance before the check runs and given back if the check
    fails or the AI detector did not run.

Payme (Merchant API, JSON-RPC 2.0, developer.help.paycom.uz):
    endpoint  POST /payments/payme      (set it as the endpoint URL in the Payme sandbox cabinet)
    account   order_id                  (account field "order_id", one-time account type)
    checkout  https://test.paycom.uz/<base64 of m=..;ac.order_id=..;a=<tiyin>;c=<return>>
Click (SHOP API, docs.click.uz/shop-api):
    endpoints POST /payments/click/prepare and POST /payments/click/complete
    checkout  https://my.click.uz/services/pay?service_id&merchant_id&amount&transaction_param&return_url
    Click has no separate sandbox host: use the test service Click issues for the sandbox and its
    Playground (docs.click.uz/testing/playground). Never put a live service's keys here.
Uzum Bank (Merchant API webhooks, developer.uzumbank.uz/merchant):
    endpoints POST /payments/uzum/check, /create, /confirm, /reverse, /status (Basic auth)
    account   params.account = order number. There is no checkout link: the customer pays in the
              Uzum Bank app (Payments -> the PlagIQ service -> order number). Uzum has no public
              sandbox: use the test service and credentials Uzum's team issues.

Settings (website service; keys are secrets, set them only in Railway):
    PAYMENTS_ENABLED=1, PAYMENTS_MODE=test
    PAYME_MERCHANT_ID, PAYME_TEST_KEY          (Payme sandbox merchant ID and test key)
    CLICK_SERVICE_ID, CLICK_MERCHANT_ID, CLICK_SECRET_KEY
    UZUM_SERVICE_ID, UZUM_LOGIN, UZUM_PASSWORD, UZUM_SERVICE_NAME (name shown in the Uzum Bank app)
    TOPUP_MIN=1000, TOPUP_MAX=2000000          (so'm)
    PAYME_IKPU, PAYME_PACKAGE_CODE, PAYME_VAT_PERCENT   (optional fiscal data; both codes from tasnif.soliq.uz)
Data is kept in the accounts database (auth.db_file()), tables orders, payme_tx, click_tx, ledger.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from urllib.parse import urlencode

import pricing

PAYME_CHECKOUT = "https://test.paycom.uz"           # sandbox only
CLICK_CHECKOUT = "https://my.click.uz/services/pay"
UZUM_CONFIRM_MS = 30 * 60 * 1000                  # Uzum: unconfirmed after 30 minutes means failed
PAYME_TIMEOUT_MS = 43_200_000                      # 12 h: Payme cancels unperformed transactions after this
ORDER_DAYS = 1                                     # an unpaid order can be started within a day
_lock = threading.Lock()


class PaymentError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------- settings

def mode():
    return os.environ.get("PAYMENTS_MODE", "").strip().lower()


def problem():
    """Why payments are off ("" when on)."""
    if os.environ.get("PAYMENTS_ENABLED", "") != "1":
        return "PAYMENTS_ENABLED is not 1"
    if mode() != "test":
        return "PAYMENTS_MODE must be test (this version supports only the providers' test mode)"
    import auth
    if not auth.enabled():
        return "Telegram sign-in is needed for balances"
    if not providers():
        return ("no provider configured (PAYME_MERCHANT_ID + PAYME_TEST_KEY, CLICK_SERVICE_ID + CLICK_MERCHANT_ID + "
                "CLICK_SECRET_KEY, or UZUM_SERVICE_ID + UZUM_LOGIN + UZUM_PASSWORD)")
    return ""


def enabled():
    return not problem()


def payme_ready():
    return bool(os.environ.get("PAYME_MERCHANT_ID", "").strip() and os.environ.get("PAYME_TEST_KEY", ""))


def click_ready():
    return all(os.environ.get(k, "").strip() for k in ("CLICK_SERVICE_ID", "CLICK_MERCHANT_ID", "CLICK_SECRET_KEY"))


def uzum_ready():
    return all(os.environ.get(k, "").strip() for k in ("UZUM_SERVICE_ID", "UZUM_LOGIN", "UZUM_PASSWORD"))


def providers():
    return [name for name, ok in (("payme", payme_ready()), ("click", click_ready()), ("uzum", uzum_ready())) if ok]


def _int_env(name, default):
    try:
        return max(int(os.environ.get(name, default)), 1)
    except ValueError:
        return default


def limits():
    low, high = _int_env("TOPUP_MIN", 1000), _int_env("TOPUP_MAX", 2_000_000)
    return low, max(low, high)


def package_options():
    """Packages as top-ups: pay `checks` x plan price, receive (checks + bonus) x plan price."""
    out = []
    for p_index, package in enumerate(pricing.packages()):
        for tier in pricing.tiers():
            if tier["price_uzs"] > 0:
                out.append({"id": f"{p_index}:{tier['id']}", "tier": tier["id"], "checks": package["checks"],
                            "bonus": package["bonus"], "pay_uzs": package["checks"] * tier["price_uzs"],
                            "credit_uzs": (package["checks"] + package["bonus"]) * tier["price_uzs"]})
    return out


def public_config():
    low, high = limits()
    return {"enabled": enabled(), "mode": mode() if enabled() else "", "providers": providers() if enabled() else [],
            "min_uzs": low, "max_uzs": high, "packages": package_options() if enabled() else []}


def price_for(mode_name, words, ai_check):
    """So'm a check costs, or None when the document is larger than every plan."""
    total = 0
    if mode_name != "ai":
        quote = pricing.quote(words)
        if not quote["fits"]:
            return None
        total += quote["price_uzs"]
    if ai_check:
        total += pricing.ai_price(words)
    return total


# ------------------------------------------------------------- storage

def _connect():
    import auth
    db = sqlite3.connect(auth.db_file(), timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, user INTEGER NOT NULL, provider TEXT NOT NULL,
            pay_uzs INTEGER NOT NULL, credit_uzs INTEGER NOT NULL, package TEXT, mode TEXT NOT NULL,
            state TEXT NOT NULL, created INTEGER NOT NULL, paid INTEGER, provider_ref TEXT);
        CREATE INDEX IF NOT EXISTS orders_user ON orders (user, created);
        CREATE TABLE IF NOT EXISTS payme_tx (id TEXT PRIMARY KEY, order_id TEXT NOT NULL, amount INTEGER NOT NULL,
            payme_time INTEGER NOT NULL, create_time INTEGER NOT NULL, perform_time INTEGER NOT NULL DEFAULT 0,
            cancel_time INTEGER NOT NULL DEFAULT 0, state INTEGER NOT NULL, reason INTEGER);
        CREATE INDEX IF NOT EXISTS payme_order ON payme_tx (order_id);
        CREATE TABLE IF NOT EXISTS click_tx (prepare_id INTEGER PRIMARY KEY AUTOINCREMENT, click_trans_id TEXT NOT NULL,
            order_id TEXT NOT NULL, amount TEXT NOT NULL, state TEXT NOT NULL, created INTEGER NOT NULL,
            confirmed INTEGER);
        CREATE TABLE IF NOT EXISTS uzum_tx (id TEXT PRIMARY KEY, order_id TEXT NOT NULL, amount INTEGER NOT NULL,
            state TEXT NOT NULL, trans_time INTEGER NOT NULL, confirm_time INTEGER, reverse_time INTEGER);
        CREATE INDEX IF NOT EXISTS uzum_order ON uzum_tx (order_id);
        CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER NOT NULL,
            time INTEGER NOT NULL, amount_uzs INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT, note TEXT,
            mode TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ledger_user ON ledger (user, id);
    """)
    return db


def _ms():
    return int(time.time() * 1000)


def _balance(db, user_id):
    return db.execute("SELECT COALESCE(SUM(amount_uzs), 0) FROM ledger WHERE user=?", (user_id,)).fetchone()[0]


def balance(user):
    with _lock, closing(_connect()) as db:
        return _balance(db, user["id"])


def account(user, limit=20):
    """Balance, recent ledger entries and recent orders for the account menu."""
    with _lock, closing(_connect()) as db:
        ledger = [dict(r) for r in db.execute("SELECT time, amount_uzs, kind, ref, note, mode FROM ledger WHERE user=? "
                                              "ORDER BY id DESC LIMIT ?", (user["id"], limit))]
        orders = [dict(r) for r in db.execute("SELECT id, provider, pay_uzs, credit_uzs, state, created, paid, mode "
                                              "FROM orders WHERE user=? ORDER BY created DESC LIMIT ?", (user["id"], limit))]
        return {"balance_uzs": _balance(db, user["id"]), "ledger": ledger, "orders": orders}


def create_order(user, provider, amount_uzs=None, package=None):
    """A pending order; returns {"id", "pay_uzs", "credit_uzs", "provider", "pay_url"}."""
    if not enabled():
        raise PaymentError("payments_off", "Payments are not switched on.")
    if provider not in providers():
        raise PaymentError("provider_unavailable", "This payment method is not available.")
    if package:
        match = next((p for p in package_options() if p["id"] == package), None)
        if not match:
            raise PaymentError("invalid_amount", "Unknown package.")
        pay, credit = match["pay_uzs"], match["credit_uzs"]
    else:
        low, high = limits()
        if type(amount_uzs) is not int or not low <= amount_uzs <= high:
            raise PaymentError("invalid_amount", f"Choose an amount between {low} and {high} so'm.")
        pay = credit = amount_uzs
    with _lock, closing(_connect()) as db:
        while True:
            order_id = str(secrets.randbelow(9 * 10 ** 11) + 10 ** 11)       # 12 digits
            if not db.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
                break
        db.execute("INSERT INTO orders (id, user, provider, pay_uzs, credit_uzs, package, mode, state, created) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)", (order_id, user["id"], provider, pay, credit, package, mode(), _ms()))
    result = {"id": order_id, "provider": provider, "pay_uzs": pay, "credit_uzs": credit,
              "pay_url": checkout_url(provider, order_id, pay)}
    if provider == "uzum":                         # paid inside the Uzum Bank app with the order number
        result["service_name"] = os.environ.get("UZUM_SERVICE_NAME", "").strip() or "PlagIQ"
    return result


def _return_url(order_id):
    origin = os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")
    return f"{origin}/?paid={order_id}" if origin else ""


def checkout_url(provider, order_id, pay_uzs):
    """The provider's payment page, or None for Uzum (no link: the customer pays in the Uzum Bank app)."""
    if provider == "uzum":
        return None
    back = _return_url(order_id)
    if provider == "payme":
        params = f"m={os.environ['PAYME_MERCHANT_ID'].strip()};ac.order_id={order_id};a={pay_uzs * 100};l=uz" + (f";c={back}" if back else "")
        return PAYME_CHECKOUT + "/" + base64.b64encode(params.encode()).decode()
    query = {"service_id": os.environ["CLICK_SERVICE_ID"].strip(), "merchant_id": os.environ["CLICK_MERCHANT_ID"].strip(),
             "amount": pay_uzs, "transaction_param": order_id}
    if back:
        query["return_url"] = back
    return CLICK_CHECKOUT + "?" + urlencode(query)


def order(user, order_id):
    with _lock, closing(_connect()) as db:
        row = db.execute("SELECT id, provider, pay_uzs, credit_uzs, state, created, paid, mode, provider_ref FROM orders "
                         "WHERE id=? AND user=?", (str(order_id), user["id"])).fetchone()
        return dict(row) if row else None


def _credit(db, row, ref):
    """Mark an order paid and credit the balance (inside an open transaction)."""
    db.execute("UPDATE orders SET state='paid', paid=?, provider_ref=? WHERE id=?", (_ms(), ref, row["id"]))
    db.execute("INSERT INTO ledger (user, time, amount_uzs, kind, ref, note, mode) VALUES (?, ?, ?, 'topup', ?, ?, ?)",
               (row["user"], _ms(), row["credit_uzs"], row["id"], row["provider"], row["mode"]))


def _reverse(db, row, ref):
    """Undo a paid top-up (provider cancelled it). False when the money was already spent."""
    if _balance(db, row["user"]) < row["credit_uzs"]:
        return False
    db.execute("UPDATE orders SET state='refunded' WHERE id=?", (row["id"],))
    db.execute("INSERT INTO ledger (user, time, amount_uzs, kind, ref, note, mode) VALUES (?, ?, ?, 'reversal', ?, ?, ?)",
               (row["user"], _ms(), -row["credit_uzs"], row["id"], ref, row["mode"]))
    return True


def charge(user, amount_uzs, note):
    """Take a check's price from the balance. Returns the ledger id (None for free checks)."""
    if amount_uzs <= 0:
        return None
    with _lock, closing(_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            have = _balance(db, user["id"])
            if have < amount_uzs:
                raise PaymentError("insufficient_balance", f"This check costs {amount_uzs} so'm; your balance is {have} so'm.")
            cursor = db.execute("INSERT INTO ledger (user, time, amount_uzs, kind, note, mode) VALUES (?, ?, ?, 'charge', ?, ?)",
                                (user["id"], _ms(), -amount_uzs, note[:120], mode()))
            db.execute("COMMIT")
            return cursor.lastrowid
        except BaseException:
            db.execute("ROLLBACK")
            raise


def refund(user, charge_id, amount_uzs, note):
    """Give back all or part of a charge (once per charge and amount)."""
    if not charge_id or amount_uzs <= 0:
        return
    with _lock, closing(_connect()) as db:
        db.execute("INSERT INTO ledger (user, time, amount_uzs, kind, ref, note, mode) VALUES (?, ?, ?, 'refund', ?, ?, ?)",
                   (user["id"], _ms(), amount_uzs, str(charge_id), note[:120], mode()))


def has_records(user):
    """True when the accounts database holds payment rows for this user (the tables may not exist yet)."""
    import auth
    try:
        with _lock, closing(sqlite3.connect(auth.db_file(), timeout=10)) as db:
            return bool(db.execute("SELECT 1 FROM ledger WHERE user=? UNION SELECT 1 FROM orders WHERE user=? LIMIT 1",
                                   (user["id"], user["id"])).fetchone())
    except sqlite3.Error:
        return False


def _in_progress(db, orders):
    if not orders:
        return False
    marks = ",".join("?" * len(orders))
    return bool(db.execute(f"SELECT 1 FROM payme_tx WHERE state=1 AND order_id IN ({marks})", orders).fetchone()
                or db.execute(f"SELECT 1 FROM click_tx WHERE state='prepared' AND created>? AND order_id IN ({marks})",
                              [_ms() - 3_600_000] + orders).fetchone()
                or db.execute(f"SELECT 1 FROM uzum_tx WHERE state='CREATED' AND trans_time>? AND order_id IN ({marks})",
                              [_ms() - UZUM_CONFIRM_MS] + orders).fetchone())


def in_progress(user):
    """True while a Payme transaction is open, a Click payment was prepared in the last hour, or an
    Uzum transaction awaits confirmation (30 minutes)."""
    if not has_records(user):
        return False
    with _lock, closing(_connect()) as db:
        return _in_progress(db, [r[0] for r in db.execute("SELECT id FROM orders WHERE user=?", (user["id"],))])


def forget(user):
    """Delete all of a user's payment records (orders, provider transactions, ledger) with the account.
    Refused while a payment is in progress, so a provider never confirms money for a deleted order."""
    with _lock, closing(_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            orders = [r[0] for r in db.execute("SELECT id FROM orders WHERE user=?", (user["id"],))]
            marks = ",".join("?" * len(orders))
            if _in_progress(db, orders):
                raise PaymentError("payment_in_progress", "A payment is still in progress. Try again in an hour.")
            if orders:
                db.execute(f"DELETE FROM payme_tx WHERE order_id IN ({marks})", orders)
                db.execute(f"DELETE FROM click_tx WHERE order_id IN ({marks})", orders)
                db.execute(f"DELETE FROM uzum_tx WHERE order_id IN ({marks})", orders)
            db.execute("DELETE FROM orders WHERE user=?", (user["id"],))
            db.execute("DELETE FROM ledger WHERE user=?", (user["id"],))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def attach_report(charge_id, report_id):
    if charge_id and report_id:
        with _lock, closing(_connect()) as db:
            db.execute("UPDATE ledger SET ref=? WHERE id=? AND kind='charge'", (report_id[:12], charge_id))


def stats():
    """Totals for /admin (no personal data)."""
    try:
        with _lock, closing(_connect()) as db:
            paid = db.execute("SELECT provider, mode, COUNT(*), COALESCE(SUM(pay_uzs), 0) FROM orders WHERE state='paid' "
                              "GROUP BY provider, mode").fetchall()
            pending = db.execute("SELECT COUNT(*) FROM orders WHERE state='pending'").fetchone()[0]
            charged = db.execute("SELECT COUNT(*), COALESCE(-SUM(amount_uzs), 0) FROM ledger WHERE kind='charge'").fetchone()
            refunded = db.execute("SELECT COALESCE(SUM(amount_uzs), 0) FROM ledger WHERE kind='refund'").fetchone()[0]
            held = db.execute("SELECT COALESCE(SUM(amount_uzs), 0) FROM ledger").fetchone()[0]
    except sqlite3.Error:
        return None
    return {"paid": [{"provider": p, "mode": m, "orders": n, "uzs": s} for p, m, n, s in paid], "pending": pending,
            "checks_charged": charged[0], "charged_uzs": charged[1] - refunded, "balances_uzs": held}


# ------------------------------------------------------------- Payme Merchant API

PAYME_MESSAGES = {
    -32504: ("Недостаточно привилегий", "Ruxsat yetarli emas", "Insufficient privileges"),
    -32700: ("Ошибка разбора JSON", "JSON xatosi", "Parse error"),
    -32600: ("Неверный запрос", "Noto‘g‘ri so‘rov", "Invalid request"),
    -32601: ("Метод не найден", "Metod topilmadi", "Method not found"),
    -32400: ("Системная ошибка", "Tizim xatosi", "System error"),
    -31001: ("Неверная сумма", "Noto‘g‘ri summa", "Incorrect amount"),
    -31003: ("Транзакция не найдена", "Tranzaksiya topilmadi", "Transaction not found"),
    -31007: ("Невозможно отменить: услуга оказана", "Bekor qilib bo‘lmaydi: xizmat ko‘rsatilgan",
             "Cannot cancel: the balance was already used"),
    -31008: ("Невозможно выполнить операцию", "Amalni bajarib bo‘lmaydi", "Unable to perform operation"),
    -31050: ("Заказ не найден", "Buyurtma topilmadi", "Order not found"),
    -31051: ("Заказ уже оплачен или ожидает оплаты", "Buyurtma allaqachon to‘langan yoki to‘lov kutilmoqda",
             "Order is already paid or awaiting payment"),
}


class PaymeError(Exception):
    def __init__(self, code, data=None):
        super().__init__(code)
        self.code, self.data = code, data


def _payme_authorised(header):
    key = os.environ.get("PAYME_TEST_KEY", "")
    if not key or not header.startswith("Basic "):
        return False
    expected = "Basic " + base64.b64encode(("Paycom:" + key).encode()).decode()
    return hmac.compare_digest(header.encode(), expected.encode())


def payme(body, auth_header):
    """Handle one Payme JSON-RPC request (raw body bytes). Always returns a dict for an HTTP 200 reply."""
    request_id = None
    try:
        try:
            data = json.loads(body)
        except ValueError:
            raise PaymeError(-32700)
        if not isinstance(data, dict):
            raise PaymeError(-32600)
        request_id = data.get("id")
        if not enabled() or not payme_ready() or not _payme_authorised(auth_header or ""):
            raise PaymeError(-32504)
        method, params = data.get("method"), data.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            raise PaymeError(-32600)
        handler = PAYME_METHODS.get(method)
        if not handler:
            raise PaymeError(-32601, method[:40])
        with _lock, closing(_connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = handler(db, params)
                db.execute("COMMIT")
            except PaymeError:
                db.execute("COMMIT")               # keep a timeout cancellation recorded before the error
                raise
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except PaymeError as exc:
        ru, uz, en = PAYME_MESSAGES.get(exc.code, PAYME_MESSAGES[-32400])
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": exc.code, "message": {"ru": ru, "uz": uz, "en": en}, "data": exc.data}}
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        ru, uz, en = PAYME_MESSAGES[-32400]
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32400, "message": {"ru": ru, "uz": uz, "en": en}}}


def _payme_order(db, params):
    """The order named in params.account, checked against params.amount (tiyin)."""
    account = params.get("account")
    if not isinstance(account, dict):
        raise PaymeError(-31050, "order_id")
    row = db.execute("SELECT * FROM orders WHERE id=? AND provider='payme'", (str(account.get("order_id", "")),)).fetchone()
    if not row:
        raise PaymeError(-31050, "order_id")
    amount = params.get("amount")
    if type(amount) is not int or amount != row["pay_uzs"] * 100:
        raise PaymeError(-31001, "amount")
    return row


def _detail(row):
    """Optional fiscal data (Payme "detail") when the owner set PAYME_IKPU."""
    code, package = os.environ.get("PAYME_IKPU", "").strip(), os.environ.get("PAYME_PACKAGE_CODE", "").strip()
    if not code or not package:                    # Payme requires both for fiscal data
        return None
    return {"receipt_type": 0, "items": [{"title": "PlagIQ balance top-up", "price": row["pay_uzs"] * 100, "count": 1,
                                          "code": code, "package_code": package,
                                          "vat_percent": int(os.environ.get("PAYME_VAT_PERCENT", "0") or 0)}]}


def _check_perform(db, params):
    row = _payme_order(db, params)
    if row["state"] != "pending" or row["created"] < _ms() - ORDER_DAYS * 86_400_000:
        raise PaymeError(-31051 if row["state"] != "pending" else -31050, "order_id")
    active = db.execute("SELECT 1 FROM payme_tx WHERE order_id=? AND state IN (1, 2)", (row["id"],)).fetchone()
    if active:
        raise PaymeError(-31051, "order_id")
    detail = _detail(row)
    return {"allow": True, **({"detail": detail} if detail else {})}


def _tx(db, params):
    tx_id = params.get("id")
    if not isinstance(tx_id, str) or not tx_id:
        raise PaymeError(-31003)
    return db.execute("SELECT * FROM payme_tx WHERE id=?", (tx_id,)).fetchone()


def _timeout(db, tx):
    """Cancel a pending transaction older than 12 h (reason 4). True when it was cancelled."""
    if tx["state"] == 1 and _ms() - tx["create_time"] > PAYME_TIMEOUT_MS:
        db.execute("UPDATE payme_tx SET state=-1, reason=4, cancel_time=? WHERE id=?", (_ms(), tx["id"]))
        return True
    return False


def _create(db, params):
    tx = _tx(db, params)
    if tx:
        if tx["state"] != 1 or _timeout(db, tx):
            raise PaymeError(-31008)
        return {"create_time": tx["create_time"], "transaction": tx["id"], "state": 1}
    row = _payme_order(db, params)                 # order exists (-31050), amount matches (-31001)
    for other in db.execute("SELECT * FROM payme_tx WHERE order_id=? AND state IN (1, 2)", (row["id"],)).fetchall():
        if not _timeout(db, other):
            raise PaymeError(-31008)               # another transaction is already waiting or done (sandbox test)
    _check_perform(db, params)
    payme_time = params.get("time")
    if type(payme_time) is not int:
        raise PaymeError(-32600)
    now = _ms()
    db.execute("INSERT INTO payme_tx (id, order_id, amount, payme_time, create_time, state) VALUES (?, ?, ?, ?, ?, 1)",
               (params["id"], row["id"], params["amount"], payme_time, now))
    return {"create_time": now, "transaction": params["id"], "state": 1}


def _perform(db, params):
    tx = _tx(db, params)
    if not tx:
        raise PaymeError(-31003)
    if tx["state"] == 2:
        return {"transaction": tx["id"], "perform_time": tx["perform_time"], "state": 2}
    if tx["state"] != 1 or _timeout(db, tx):
        raise PaymeError(-31008)
    order_row = db.execute("SELECT * FROM orders WHERE id=?", (tx["order_id"],)).fetchone()
    if order_row["state"] != "pending":
        raise PaymeError(-31008)
    now = _ms()
    db.execute("UPDATE payme_tx SET state=2, perform_time=? WHERE id=?", (now, tx["id"]))
    _credit(db, order_row, "payme:" + tx["id"])
    return {"transaction": tx["id"], "perform_time": now, "state": 2}


def _cancel(db, params):
    tx = _tx(db, params)
    if not tx:
        raise PaymeError(-31003)
    if tx["state"] in (-1, -2):
        return {"transaction": tx["id"], "cancel_time": tx["cancel_time"], "state": tx["state"]}
    reason = params.get("reason")
    reason = reason if type(reason) is int else None
    now = _ms()
    if tx["state"] == 1:
        db.execute("UPDATE payme_tx SET state=-1, reason=?, cancel_time=? WHERE id=?", (reason, now, tx["id"]))
        return {"transaction": tx["id"], "cancel_time": now, "state": -1}
    order_row = db.execute("SELECT * FROM orders WHERE id=?", (tx["order_id"],)).fetchone()
    if not _reverse(db, order_row, "payme-cancel:" + tx["id"]):
        raise PaymeError(-31007)
    db.execute("UPDATE payme_tx SET state=-2, reason=?, cancel_time=? WHERE id=?", (reason, now, tx["id"]))
    return {"transaction": tx["id"], "cancel_time": now, "state": -2}


def _check_tx(db, params):
    tx = _tx(db, params)
    if not tx:
        raise PaymeError(-31003)
    _timeout(db, tx)
    tx = _tx(db, params)
    return {"create_time": tx["create_time"], "perform_time": tx["perform_time"], "cancel_time": tx["cancel_time"],
            "transaction": tx["id"], "state": tx["state"], "reason": tx["reason"]}


def _statement(db, params):
    start, end = params.get("from"), params.get("to")
    if type(start) is not int or type(end) is not int:
        raise PaymeError(-32600)
    rows = db.execute("SELECT * FROM payme_tx WHERE payme_time BETWEEN ? AND ? ORDER BY payme_time", (start, end)).fetchall()
    return {"transactions": [{"id": r["id"], "time": r["payme_time"], "amount": r["amount"],
                              "account": {"order_id": r["order_id"]}, "create_time": r["create_time"],
                              "perform_time": r["perform_time"], "cancel_time": r["cancel_time"],
                              "transaction": r["id"], "state": r["state"], "reason": r["reason"]} for r in rows]}


PAYME_METHODS = {"CheckPerformTransaction": _check_perform, "CreateTransaction": _create,
                 "PerformTransaction": _perform, "CancelTransaction": _cancel,
                 "CheckTransaction": _check_tx, "GetStatement": _statement}


# ------------------------------------------------------------- Click SHOP API

CLICK_NOTES = {0: "Success", -1: "SIGN CHECK FAILED!", -2: "Incorrect parameter amount", -3: "Action not found",
               -4: "Already paid", -5: "User does not exist", -6: "Transaction does not exist",
               -7: "Failed to update user", -8: "Error in request from click", -9: "Transaction cancelled"}


def _click_reply(code, form, **extra):
    return {"error": code, "error_note": CLICK_NOTES[code], "click_trans_id": form.get("click_trans_id", ""),
            "merchant_trans_id": form.get("merchant_trans_id", ""), **extra}


def click_sign(form, secret, with_prepare):
    parts = [form.get("click_trans_id", ""), form.get("service_id", ""), secret, form.get("merchant_trans_id", "")]
    if with_prepare:
        parts.append(form.get("merchant_prepare_id", ""))
    parts += [form.get("amount", ""), form.get("action", ""), form.get("sign_time", "")]
    return hashlib.md5("".join(parts).encode()).hexdigest()


def click(form, stage):
    """Handle a Click Prepare (stage "prepare", action 0) or Complete (stage "complete", action 1) request."""
    form = {k: str(v) for k, v in form.items()}
    required = ["click_trans_id", "service_id", "merchant_trans_id", "amount", "action", "sign_time", "sign_string"]
    if stage == "complete":
        required.append("merchant_prepare_id")
    if not enabled() or not click_ready() or any(not form.get(k) for k in required):
        return _click_reply(-8, form)
    if form["action"] != {"prepare": "0", "complete": "1"}[stage]:
        return _click_reply(-3, form)
    secret = os.environ["CLICK_SECRET_KEY"]
    if (form["service_id"] != os.environ["CLICK_SERVICE_ID"].strip()
            or not hmac.compare_digest(click_sign(form, secret, stage == "complete"), form["sign_string"].lower())):
        return _click_reply(-1, form)
    try:
        amount = float(form["amount"])
        error = int(form.get("error") or 0)
    except ValueError:
        return _click_reply(-8, form)
    with _lock, closing(_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            reply = (_click_prepare if stage == "prepare" else _click_complete)(db, form, amount, error)
            db.execute("COMMIT")
            return reply
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _click_order(db, form, amount):
    row = db.execute("SELECT * FROM orders WHERE id=? AND provider='click'", (form["merchant_trans_id"],)).fetchone()
    if not row:
        return None, -5
    if abs(amount - row["pay_uzs"]) > 0.001:
        return row, -2
    return row, 0


def _click_prepare(db, form, amount, error):
    row, code = _click_order(db, form, amount)
    if code:
        return _click_reply(code, form)
    if row["state"] == "paid":
        return _click_reply(-4, form)
    if row["state"] != "pending" or row["created"] < _ms() - ORDER_DAYS * 86_400_000:
        return _click_reply(-9, form)
    same = db.execute("SELECT prepare_id FROM click_tx WHERE click_trans_id=? AND order_id=? AND state='prepared'",
                      (form["click_trans_id"], row["id"])).fetchone()
    if same:                                       # Click repeated the request
        return _click_reply(0, form, merchant_prepare_id=same["prepare_id"])
    cursor = db.execute("INSERT INTO click_tx (click_trans_id, order_id, amount, state, created) VALUES (?, ?, ?, 'prepared', ?)",
                        (form["click_trans_id"], row["id"], form["amount"], _ms()))
    return _click_reply(0, form, merchant_prepare_id=cursor.lastrowid)


def _click_complete(db, form, amount, error):
    try:
        prepare_id = int(form["merchant_prepare_id"])
    except ValueError:
        return _click_reply(-6, form)
    tx = db.execute("SELECT * FROM click_tx WHERE prepare_id=?", (prepare_id,)).fetchone()
    if not tx or tx["click_trans_id"] != form["click_trans_id"] or tx["order_id"] != form["merchant_trans_id"]:
        return _click_reply(-6, form)
    row, code = _click_order(db, form, amount)
    if code:
        return _click_reply(code, form)
    if tx["state"] == "confirmed":
        return _click_reply(-4, form)
    if tx["state"] == "cancelled":
        return _click_reply(-9, form)
    if error < 0:                                  # Click reports a failed payment: cancel and answer -9
        db.execute("UPDATE click_tx SET state='cancelled' WHERE prepare_id=?", (prepare_id,))
        return _click_reply(-9, form)
    if row["state"] != "pending":
        return _click_reply(-4 if row["state"] == "paid" else -9, form)
    db.execute("UPDATE click_tx SET state='confirmed', confirmed=? WHERE prepare_id=?", (_ms(), prepare_id))
    _credit(db, row, "click:" + form["click_trans_id"])
    return _click_reply(0, form, merchant_confirm_id=prepare_id)


# ------------------------------------------------------------- Uzum Bank Merchant API

UZUM_METHODS = ("check", "create", "confirm", "reverse", "status")


class UzumError(Exception):
    def __init__(self, code, **fields):
        super().__init__(code)
        self.code, self.fields = code, fields


def _uzum_authorised(header):
    login, password = os.environ.get("UZUM_LOGIN", ""), os.environ.get("UZUM_PASSWORD", "")
    if not login or not password or not header.startswith("Basic "):
        return False
    expected = "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()
    return hmac.compare_digest(header.encode(), expected.encode())


def uzum(method, body, auth_header):
    """Handle one Uzum webhook. Returns (HTTP status, JSON body): 200 on success, 400 with
    {"status": "FAILED", "errorCode": "<code>"} on errors, as the Merchant API specifies."""
    service = None
    try:
        if not enabled() or not uzum_ready() or not _uzum_authorised(auth_header or ""):
            raise UzumError("10001")
        try:
            data = json.loads(body)
        except ValueError:
            raise UzumError("10002")
        if not isinstance(data, dict) or method not in UZUM_METHODS:
            raise UzumError("10002" if method in UZUM_METHODS else "10003")
        service = data.get("serviceId")
        needed = ["serviceId", "timestamp"] + (["params"] if method == "check" else
                                                ["transId", "params", "amount"] if method == "create" else ["transId"])
        if any(data.get(k) in (None, "", {}) for k in needed):
            raise UzumError("10005")
        if str(service) != os.environ["UZUM_SERVICE_ID"].strip():
            raise UzumError("10006")
        with _lock, closing(_connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                reply = globals()["_uzum_" + method](db, data)
                db.execute("COMMIT")
            except UzumError:
                db.execute("COMMIT")               # keep a 30-minute expiry recorded before the error
                raise
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return 200, {"serviceId": service, **reply}
    except UzumError as exc:
        return 400, {"serviceId": service, **exc.fields, "status": "FAILED", "errorCode": exc.code}
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        return 400, {"serviceId": service, "status": "FAILED", "errorCode": "99999"}


def _uzum_data(row):
    return {"account": {"value": row["id"]}, "amount": {"value": str(row["pay_uzs"])}}


def _uzum_order(db, params):
    account = params.get("account") if isinstance(params, dict) else None
    row = db.execute("SELECT * FROM orders WHERE id=? AND provider='uzum'", (str(account or ""),)).fetchone()
    if not row:
        raise UzumError("10007")
    if row["state"] == "paid":
        raise UzumError("10008")
    if row["state"] != "pending" or row["created"] < _ms() - ORDER_DAYS * 86_400_000:
        raise UzumError("10009")
    return row


def _uzum_expire(db, tx):
    """An unconfirmed transaction older than 30 minutes is failed (Uzum's state model)."""
    if tx["state"] == "CREATED" and _ms() - tx["trans_time"] > UZUM_CONFIRM_MS:
        db.execute("UPDATE uzum_tx SET state='FAILED' WHERE id=?", (tx["id"],))
        return True
    return False


def _uzum_check(db, data):
    row = _uzum_order(db, data["params"])
    return {"timestamp": _ms(), "status": "OK", "data": _uzum_data(row)}


def _uzum_create(db, data):
    trans_id = str(data["transId"])
    fields = {"transId": trans_id}
    if db.execute("SELECT 1 FROM uzum_tx WHERE id=?", (trans_id,)).fetchone():
        raise UzumError("10010", **fields)
    try:
        row = _uzum_order(db, data["params"])
    except UzumError as exc:
        raise UzumError(exc.code, **fields)
    amount = data["amount"]
    if type(amount) is not int or amount != row["pay_uzs"] * 100:
        raise UzumError("10011", **fields)
    for other in db.execute("SELECT * FROM uzum_tx WHERE order_id=? AND state='CREATED'", (row["id"],)).fetchall():
        if not _uzum_expire(db, other):
            raise UzumError("10008", **fields)     # this one-time order is already being paid
    now = _ms()
    db.execute("INSERT INTO uzum_tx (id, order_id, amount, state, trans_time) VALUES (?, ?, ?, 'CREATED', ?)",
               (trans_id, row["id"], amount, now))
    return {"transId": trans_id, "status": "CREATED", "transTime": now, "data": _uzum_data(row), "amount": amount}


def _uzum_tx(db, data):
    tx = db.execute("SELECT * FROM uzum_tx WHERE id=?", (str(data["transId"]),)).fetchone()
    if not tx:
        raise UzumError("10014", transId=str(data["transId"]))
    return tx


def _uzum_confirm(db, data):
    tx = _uzum_tx(db, data)
    fields = {"transId": tx["id"]}
    if tx["state"] == "CONFIRMED":
        raise UzumError("10016", **fields)
    if tx["state"] != "CREATED" or _uzum_expire(db, tx):
        raise UzumError("10015", **fields)
    row = db.execute("SELECT * FROM orders WHERE id=?", (tx["order_id"],)).fetchone()
    if row["state"] != "pending":
        raise UzumError("10015", **fields)
    now = _ms()
    db.execute("UPDATE uzum_tx SET state='CONFIRMED', confirm_time=? WHERE id=?", (now, tx["id"]))
    _credit(db, row, "uzum:" + tx["id"])
    return {"transId": tx["id"], "status": "CONFIRMED", "confirmTime": now, "data": _uzum_data(row), "amount": tx["amount"]}


def _uzum_reverse(db, data):
    tx = _uzum_tx(db, data)
    fields = {"transId": tx["id"]}
    if tx["state"] == "REVERSED":
        raise UzumError("10018", **fields)
    row = db.execute("SELECT * FROM orders WHERE id=?", (tx["order_id"],)).fetchone()
    if tx["state"] == "CONFIRMED" and not _reverse(db, row, "uzum-reverse:" + tx["id"]):
        raise UzumError("10017", **fields)         # the top-up was already spent on checks
    now = _ms()
    db.execute("UPDATE uzum_tx SET state='REVERSED', reverse_time=? WHERE id=?", (now, tx["id"]))
    return {"transId": tx["id"], "status": "REVERSED", "reverseTime": now, "data": _uzum_data(row), "amount": tx["amount"]}


def _uzum_status(db, data):
    tx = _uzum_tx(db, data)
    _uzum_expire(db, tx)
    tx = _uzum_tx(db, data)
    times = {"transTime": tx["trans_time"], "confirmTime": tx["confirm_time"], "reverseTime": tx["reverse_time"]}
    if tx["state"] == "FAILED":
        raise UzumError("10015", transId=tx["id"], **times)
    row = db.execute("SELECT * FROM orders WHERE id=?", (tx["order_id"],)).fetchone()
    return {"transId": tx["id"], "status": tx["state"], **times, "data": _uzum_data(row), "amount": tx["amount"]}
