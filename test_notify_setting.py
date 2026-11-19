"""The "check is ready" message can be switched off (and on) in the account menu; off means none is sent, and the
setting goes with the account."""
from fastapi.testclient import TestClient

import app as web
import auth
import notify
from test_auth_ai import login, sign_in  # noqa: F401  (fixture)


def test_switch_off_and_on(login, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setattr(notify, "_start", lambda target, *args: sent.append(args))
    sign_in(login)
    assert login.get("/api/me").json()["notify"] is True                     # on by default
    assert login.post("/api/me/notify", json={"on": False}, headers={"Origin": "http://testserver"}).json() == {"notify": False}
    me = login.get("/api/me").json()
    assert me["notify"] is False
    user = me["user"]
    assert not notify.after_check(user, "", True, "a.pdf", True, "uz", "https://x.uz")
    assert not notify.after_paid_check(user, "a.pdf", True, "https://x.uz") and sent == []
    login.post("/api/me/notify", json={"on": True}, headers={"Origin": "http://testserver"})
    assert notify.after_check(user, "", True, "a.pdf", True, "uz", "https://x.uz") and len(sent) == 1
    assert login.post("/api/me/notify", json={"on": "no"}, headers={"Origin": "http://testserver"}).status_code == 400
    assert TestClient(web.app).post("/api/me/notify", json={"on": False}).status_code in (401, 403)
    login.post("/api/me/notify", json={"on": False}, headers={"Origin": "http://testserver"})
    auth.forget(user)                                                          # deleted with the account
    with auth._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM notify_off").fetchone()[0] == 0


def test_menu_and_privacy_page_mention_it():
    client = TestClient(web.app)
    assert "Sahifani yopgan bo‘lsam, tekshiruv tugaganda Telegram yoki elektron pochta orqali xabar bering" in client.get("/").text
    assert "hisob menyusida o‘chirib qo‘yish mumkin" in client.get("/privacy").text
