"""Messages to the site owner when something needs attention, so problems are not found by users first.

  * checks failing: ALERT_FAILURES (3) checks in a row ended with a search or server failure (failed_search,
    failed_internal; a file the user's computer could not give us text for does not count), and again when checks
    work again;
  * the private engine not answering on two checks in a row, 5 minutes apart, and again when it answers;
  * little room left: the website's volume or the engine's volume has less than ALERT_FREE_SHARE (10 %) free.

Sent by the Telegram bot to every OWNER_TELEGRAM_IDS (the owner must have pressed Start in the bot once), or, when
that is not possible, by e-mail to ALERT_EMAIL. The same problem is repeated at most every ALERT_REPEAT_HOURS (6)
while it lasts. Messages carry error codes and counts only: no document text, file names or user details.
OWNER_ALERTS=0 turns them off. The state is kept in memory; a restart starts afresh.
"""
import logging
import os
import threading
import time

log = logging.getLogger("plagiq.alerts")

BAD = {"failed_search", "failed_internal"}
GOOD = {"completed_matches", "completed_no_matches", "incomplete_coverage", "ai_only"}
_lock = threading.Lock()
_state = {"failures": [], "engine_down": 0, "active": {}, "sent": {}}
SENT = []                                  # the last messages, newest last (for tests and /admin)


def enabled():
    return os.environ.get("OWNER_ALERTS", "1").strip() != "0"


def _number(name, default, low, high):
    try:
        return min(max(float(os.environ.get(name, default)), low), high)
    except ValueError:
        return default


def owner_chats():
    return [int(x) for x in os.environ.get("OWNER_TELEGRAM_IDS", "").replace(" ", "").split(",") if x.isdigit()]


def _deliver(text, opener=None):
    """Telegram to each owner; e-mail to ALERT_EMAIL when no Telegram message went through. Never raises."""
    import auth
    delivered = False
    if auth.telegram_enabled():
        import telegram_bot
        for chat in owner_chats():
            try:
                delivered |= bool(telegram_bot._call("sendMessage", {"chat_id": chat, "text": text,
                                                                     "disable_web_page_preview": True}, opener).get("ok"))
            except Exception as exc:               # the owner never pressed Start, network
                log.warning("owner alert not sent by Telegram: %s", type(exc).__name__)
    address = os.environ.get("ALERT_EMAIL", "").strip()
    if not delivered and address:
        import mailer
        try:
            mailer.send(address, "PlagIQ alert", text)
            delivered = True
        except Exception as exc:
            log.warning("owner alert not e-mailed: %s", type(exc).__name__)
    if not delivered:
        log.warning("owner alert (nowhere to send it): %s", text)
    return delivered


def _start(text):
    threading.Thread(target=_deliver, args=(text,), daemon=True).start()


def raise_alert(key, text, now=None):
    """Send `text` unless the same problem was reported less than ALERT_REPEAT_HOURS ago. True when sent."""
    if not enabled():
        return False
    now = now or time.time()
    with _lock:
        _state["active"][key] = True
        if now - _state["sent"].get(key, 0) < _number("ALERT_REPEAT_HOURS", 6, 0.5, 168) * 3600:
            return False
        _state["sent"][key] = now
    SENT.append(text)
    del SENT[:-20]
    _start("⚠️ PlagIQ: " + text)
    return True


def clear(key, text):
    """The problem behind `key` is over: say so once if it had been reported."""
    with _lock:
        was = _state["active"].pop(key, False)
        reported = _state["sent"].pop(key, None)
    if was and reported and enabled():
        SENT.append(text)
        del SENT[:-20]
        _start("✅ PlagIQ: " + text)
        return True
    return False


def check_outcome(mode, status, code=None, now=None):
    """Called for every check that ran (metrics.record_check / record_failure)."""
    if status in BAD:
        limit = int(_number("ALERT_FAILURES", 3, 2, 50))
        with _lock:
            _state["failures"].append(f"{code or status} ({mode})")
            recent = list(_state["failures"])
        if len(recent) >= limit:
            raise_alert("checks", f"the last {len(recent)} checks failed. Latest: {', '.join(recent[-3:])}. "
                                  "See /admin → Monitoring.", now)
    elif status in GOOD:
        with _lock:
            _state["failures"].clear()
        clear("checks", "checks are finishing normally again.")


def engine_result(reason, now=None):
    """The engine's answer to the 5-minute probe: "" when it answered, else the reason (engine_client.probe)."""
    if reason:
        with _lock:
            _state["engine_down"] += 1
            count = _state["engine_down"]
        if count >= 2:
            raise_alert("engine", f"the search engine (library) is not answering: {reason}", now)
    else:
        with _lock:
            _state["engine_down"] = 0
        clear("engine", "the search engine (library) answers again.")


def disk(name, free, total, now=None):
    """Free space on a volume; alerts below ALERT_FREE_SHARE (10 %) free."""
    if not free and free != 0 or not total:
        return
    share = _number("ALERT_FREE_SHARE", 10, 1, 50) / 100
    key = "disk:" + name
    if free / total < share:
        raise_alert(key, f"{name} volume is almost full: {free // 2**20:,} MB free of {total // 2**20:,} MB.", now)
    elif free / total > share + 0.05:
        clear(key, f"{name} volume has room again ({free // 2**20:,} MB free).")


async def watch_round():
    """One round of the 5-minute watch (engine and volumes); never raises."""
    if not enabled():
        return
    from fastapi.concurrency import run_in_threadpool
    import engine_client
    import metrics
    try:
        if engine_client.configured():
            reason = await engine_client.probe()
            engine_result(reason)
            if not reason:
                lib = await engine_client.library()
                d = (lib or {}).get("disk") or {}
                disk("Engine", d.get("free_bytes"), d.get("total_bytes"))
        store = await run_in_threadpool(metrics.storage)
        disk("Website", store.get("free"), store.get("total"))
    except Exception as exc:
        log.warning("owner alert watch failed: %s", type(exc).__name__)


def reset():
    with _lock:
        _state.update(failures=[], engine_down=0, active={}, sent={})
    SENT.clear()
