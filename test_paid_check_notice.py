""""Your check is ready" for a check the server runs after its payment is confirmed: sent when the account's page has
not asked for the paid checks' status for 20 s, in the page's last language; not sent while the page keeps asking."""
import time

import notify
from test_check_payment import payme_pays, run_now, signed, start  # noqa: F401  (fixtures)
from test_payments import pay  # noqa: F401


def capture(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_start", lambda target, *args: sent.append(args))
    notify._PAGE_SEEN.clear()
    return sent


def test_sent_when_the_page_has_left(signed, monkeypatch):  # noqa: F811
    sent = capture(monkeypatch)
    body = start(signed).json()
    signed.get("/api/check/pending?lang=ru")                     # the page was open in Russian when paying ...
    payme_pays(signed, body["order_id"], 15000)
    seen, lang = notify._PAGE_SEEN[42]
    notify._PAGE_SEEN[42] = (seen - 60, lang)                    # ... then closed a minute ago
    assert run_now() == 1
    assert len(sent) == 1
    user, filename, ok, lang, origin = sent[0]
    assert user["id"] == 42 and filename == "thesis.txt" and ok is True and lang == "ru"


def test_not_sent_while_the_page_keeps_asking(signed, monkeypatch):  # noqa: F811
    sent = capture(monkeypatch)
    body = start(signed).json()
    payme_pays(signed, body["order_id"], 15000)
    signed.get("/api/check/pending?lang=uz")                    # asking also starts the paid check
    run_now()
    assert sent == []


def test_never_asked_means_uzbek_and_unknown_language_is_ignored(monkeypatch):
    sent = capture(monkeypatch)
    assert notify.after_paid_check({"id": 7}, "a.pdf", False, "https://x.uz")
    assert sent[0][3] == "uz" and sent[0][2] is False
    notify.page_seen(8, "<script>")
    assert notify._PAGE_SEEN[8][1] == "uz" and not notify.after_paid_check({"id": 8}, "a.pdf", True, "https://x.uz")
    assert time.monotonic() - notify._PAGE_SEEN[8][0] < 5
