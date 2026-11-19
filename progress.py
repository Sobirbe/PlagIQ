"""What a running check is doing now, for the progress list on the page.

The browser sends a random token (32 hex characters) with the check and polls GET /api/progress/{token}.
Steps are recorded by the code that actually does the work, so only stages that really run are shown:

  read      reading the file (extraction)           ocr       scanned pages read by OCR ("3 of 12")
  discover  searching OpenAlex                       fulltext  downloading open-access full texts
  web       web search                               compare   comparing passages word by word
  library   searching the PlagIQ library             ai        AI detector (Pangram)
  archive   adding the work to the archive           report    preparing the report

Kept in memory only, without any document text, and forgotten PROGRESS_SECONDS after the last update.
"""
import contextvars
import re
import threading
import time

PROGRESS_SECONDS = 1800
TOKEN = re.compile(r"[0-9a-f]{32}")
_lock = threading.Lock()
_runs = {}
current = contextvars.ContextVar("plagiq_progress", default=None)


def _purge(now):
    for key in [k for k, v in _runs.items() if now - v["updated"] > PROGRESS_SECONDS]:
        del _runs[key]


def begin(token):
    """Start tracking a check (an invalid or missing token turns progress off for it). Returns the token or None."""
    if not token or not TOKEN.fullmatch(token):
        current.set(None)
        return None
    now = time.monotonic()
    with _lock:
        _purge(now)
        if len(_runs) > 5000:
            return None
        _runs[token] = {"steps": [], "started": now, "updated": now, "done": False}
    current.set(token)
    return token


def step(key, detail=None, token=None):
    """Enter a stage (the previous one is complete). Calling it again for the stage that is running updates its detail."""
    token = token or current.get()
    if not token:
        return
    now = time.monotonic()
    with _lock:
        run = _runs.get(token)
        if run is None or run["done"]:
            return
        steps = run["steps"]
        if steps and steps[-1]["key"] == key and steps[-1]["state"] == "active":
            steps[-1]["detail"] = detail
        else:
            for s in steps:
                if s["state"] == "active":
                    s["state"], s["seconds"] = "done", round(now - s["at"], 1)
            steps.append({"key": key, "state": "active", "detail": detail, "at": now})
        run["updated"] = now


def finish(token=None, failed=False):
    """The check ended; with failed=True the stage that was running is marked as where it stopped."""
    token = token or current.get()
    if not token:
        return
    now = time.monotonic()
    with _lock:
        run = _runs.get(token)
        if run and not run["done"]:
            for s in run["steps"]:
                if s["state"] == "active":
                    s["state"], s["seconds"] = ("failed" if failed else "done"), round(now - s["at"], 1)
            run["done"], run["failed"], run["updated"] = True, bool(failed), now


def callback(token=None):
    """A function for worker threads (OCR): progress.callback()(key, detail) records into this check."""
    token = token or current.get()
    return (lambda key, detail=None: step(key, detail, token=token)) if token else None


def read(token):
    """The steps so far, for the browser (None when the token is unknown or expired)."""
    now = time.monotonic()
    with _lock:
        run = _runs.get(token)
        if run is None:
            return None
        run["seen"] = now
        return {"done": run["done"], "failed": run.get("failed", False), "seconds": round(now - run["started"], 1),
                "steps": [{"key": s["key"], "state": s["state"], "detail": s["detail"],
                           "seconds": s.get("seconds", round(now - s["at"], 1))} for s in run["steps"]]}


def watching(token, seconds=15):
    """Whether the page that started the check asked for its progress in the last `seconds` (it polls every
    1.2 s while open and in view). None when the check has no progress token, so it cannot be told."""
    if not token or not TOKEN.fullmatch(token):
        return None
    now = time.monotonic()
    with _lock:
        run = _runs.get(token)
        if run is None:
            return None
        return now - run.get("seen", run["started"]) < seconds
