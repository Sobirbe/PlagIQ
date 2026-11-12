"""Signed, storage-free report verification for the QR code on PDF reports.

The QR code holds a link with the report's key facts and an HMAC-SHA256
signature made with VERIFY_SECRET (set on the website service, at least 32
characters). The /verify page recomputes the signature: if any fact was changed
the link is reported as not valid. Nothing is stored, so links keep working
after the report itself is deleted (15 minutes) and after restarts. The link
contains no file name and no manuscript text: only the date, the first 16
characters of the document's SHA-256, the mode, the result, word count,
sources compared and the language.
"""
import base64
import hashlib
import hmac
import os
import re
from urllib.parse import urlencode

FIELDS = ("r", "d", "h", "m", "p", "w", "n", "l")
OPTIONAL = ("x",)                      # x = AI detector estimate (AI + AI-assisted %), only when it ran
MODES = {"reference": "r", "academic": "a", "corpus": "c", "ai": "i"}
PATTERNS = {"r": r"[0-9a-f]{12}", "d": r"\d{8}T\d{4}", "h": r"[0-9a-f]{16}", "m": r"[raci]", "x": r"\d{1,3}(\.\d)?", "p": r"NE|\d{1,3}(\.\d{1,2})?",
            "w": r"\d{1,7}", "n": r"\d{1,7}", "l": r"uz|ru|en"}


def secret():
    value = os.environ.get("VERIFY_SECRET", "")
    return value.encode() if len(value) >= 32 else None


def _sign(fields, key):
    message = "&".join(f"{k}={fields[k]}" for k in FIELDS + OPTIONAL if k in fields).encode()
    return base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()[:18]).decode().rstrip("=")


def facts(result, identifier):
    """The signed facts for one report."""
    mode = result.get("mode", "reference")
    if mode == "academic":
        compared = (result.get("fulltext") or {}).get("compared", 0) + (result.get("web") or {}).get("compared", 0) + (
            result["abstract_count"] if "abstract_count" in result else
            sum(1 for s in result.get("sources") or [] if s.get("coverage") == "abstract"))
    elif mode == "corpus":
        compared = result.get("source_count") or 0
    else:
        compared = 1
    percent = result.get("percent")
    return {"r": identifier[:12], "d": re.sub(r"[-:]", "", str(result.get("date", "")))[:13],
            "h": str(result.get("sha256", ""))[:16], "m": MODES.get(mode, "r"),
            "p": "NE" if percent is None else f"{float(percent):g}", "w": str(int(result.get("total_tokens") or 0)),
            "n": str(int(compared)), "l": result.get("language", "en") if result.get("language") in ("uz", "ru", "en") else "en",
            **({"x": f"{ai_share(result):g}"} if ai_share(result) is not None else {})}


def ai_share(result):
    """Pangram's AI + AI-assisted share in %, or None when the detector did not run to completion."""
    ai = result.get("ai") or {}
    if ai.get("status") != "done":
        return None
    return round(min(100.0, float(ai.get("fraction_ai", 0)) + float(ai.get("fraction_ai_assisted", 0))), 1)


def link(result, identifier, base_url):
    """Verification URL for the QR code, or None when VERIFY_SECRET is not set."""
    key = secret()
    if not key:
        return None
    fields = facts(result, identifier)
    return base_url.rstrip("/") + "/verify?" + urlencode({**fields, "s": _sign(fields, key)})


def check(params):
    """(valid, fields). Valid only with every field well formed and a matching signature."""
    key = secret()
    fields = {k: str(params.get(k, "")) for k in FIELDS}
    fields.update({k: str(params[k]) for k in OPTIONAL if k in params})
    if not key or not all(re.fullmatch(PATTERNS[k], fields[k]) for k in fields):
        return False, fields
    return hmac.compare_digest(_sign(fields, key), str(params.get("s", ""))), fields
