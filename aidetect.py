"""AI-writing detector: "Hujjatni AI detektoriga tekshirish" (Pangram API).

The figures are Pangram's estimate, reported as such; PlagIQ does not compute an AI
score itself. Pangram supports 24 languages including Russian and English, but not
Uzbek, so Uzbek documents are never sent (no cost) and the report says "not available
for Uzbek".

Privacy: the FULL manuscript text is sent to Pangram (a US company) for this check,
so it runs only with its own consent tick. The flagged passages are shown once on
the results page and are never cached or put in the PDF.

Settings (website service):
  PANGRAM_API_KEY        API key from Pangram's API dashboard (secret).
  PANGRAM_MODEL=pangram-4  model selector (see GET /models).
  AI_MAX_WORDS=15000     longest document accepted (cost cap; Pangram bills per started
                         100 words, $0.05 at the list price).
  AI_SECONDS=90          time limit for one check (10-180).
  PANGRAM_URL            override the API address (tests).
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

API = "https://text.external-api.pangram.com"
SUPPORTED = {"en", "ru"}                # Pangram languages among PlagIQ's; no Uzbek
MAX_RESPONSE = 20 * 1024 * 1024
LABELS = {"AI-Generated": "ai", "AI-Assisted": "assisted", "Human Written": "human"}


class DetectorError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _bounded(name, default, low, high):
    try:
        return min(max(int(os.environ.get(name, default)), low), high)
    except ValueError:
        return default


def configured():
    return bool(os.environ.get("PANGRAM_API_KEY"))


def settings():
    return {"enabled": configured(), "provider": "Pangram", "languages": sorted(SUPPORTED),
            "max_words": _bounded("AI_MAX_WORDS", 15000, 100, 100000)}


def _request(method, path, body=None, timeout=20):
    url = (os.environ.get("PANGRAM_URL") or API).rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": os.environ.get("PANGRAM_API_KEY", ""), "Content-Type": "application/json",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        raise DetectorError({401: "key_invalid", 402: "no_credits", 403: "model_not_enabled", 413: "too_long",
                             422: "text_rejected", 429: "busy"}.get(exc.code, "provider_error")) from None
    except (urllib.error.URLError, OSError):
        raise DetectorError("provider_unreachable") from None
    if len(raw) > MAX_RESPONSE:
        raise DetectorError("provider_error")
    try:
        return json.loads(raw)
    except ValueError:
        raise DetectorError("provider_error") from None


def billed_units(words):
    """Pangram 4 bills one unit per started block of 100 words."""
    return max(1, -(-words // 100))


def _summary(data, words):
    windows = []
    for w in data.get("windows") or []:
        if not isinstance(w, dict):
            continue
        windows.append({"label": LABELS.get(str(w.get("label")), "human"),
                        "score": round(float(w.get("ai_assistance_score") or 0), 3),
                        "confidence": str(w.get("confidence") or "")[:8],
                        "humanized": bool(w.get("is_humanized")),
                        "words": int(w.get("word_count") or 0),
                        "text": " ".join(str(w.get("text") or "").split())[:600]})
    pct = lambda key: round(100 * min(max(float(data.get(key) or 0), 0.0), 1.0), 1)
    flagged = [w for w in windows if w["label"] != "human"]
    return {"status": "done", "provider": "Pangram", "version": str(data.get("version") or "")[:16],
            "verdict": {"AI": "ai", "Human": "human", "Mixed": "mixed"}.get(str(data.get("prediction_short")), "mixed"),
            "fraction_ai": pct("fraction_ai"), "fraction_ai_assisted": pct("fraction_ai_assisted"),
            "fraction_human": pct("fraction_human"), "segments": len(windows),
            "segments_flagged": len(flagged), "humanized_segments": sum(1 for w in windows if w["humanized"]),
            "words": words, "billed_units": billed_units(words)}, flagged


def detect(text, language, words, poll=1.5):
    """(summary for the report, flagged passages for the page only)."""
    if not configured():
        return {"status": "not_configured", "provider": "Pangram"}, []
    if language not in SUPPORTED:
        return {"status": "unsupported_language", "provider": "Pangram", "language": language}, []
    limit = settings()["max_words"]
    if words > limit:
        return {"status": "too_long", "provider": "Pangram", "max_words": limit, "words": words}, []
    deadline = time.monotonic() + _bounded("AI_SECONDS", 90, 10, 180)
    try:
        task = _request("POST", "/task", {"text": text, "model": os.environ.get("PANGRAM_MODEL", "pangram-4"),
                                          "public_dashboard_link": False})
        task_id = str(task.get("task_id", ""))
        if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", task_id):
            raise DetectorError("provider_error")
        while True:
            data = _request("GET", "/task/" + task_id)
            stage = data.get("stage")
            if stage == "STAGE_SUCCESS":
                return _summary(data, words)
            if stage == "STAGE_FAILED":
                raise DetectorError("text_rejected")
            if time.monotonic() + poll > deadline:
                raise DetectorError("timeout")
            time.sleep(poll)
    except DetectorError as exc:
        return {"status": "error", "provider": "Pangram", "error": exc.code}, []
