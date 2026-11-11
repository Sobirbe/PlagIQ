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
MODES = {"reference": "r", "academic": "a", "corpus": "c"}
PATTERNS = {"r": r"[0-9a-f]{12}", "d": r"\d{8}T\d{4}", "h": r"[0-9a-f]{16}", "m": r"[rac]", "p": r"NE|\d{1,3}(\.\d{1,2})?",
            "w": r"\d{1,7}", "n": r"\d{1,7}", "l": r"uz|ru|en"}


def secret():
    value = os.environ.get("VERIFY_SECRET", "")
    return value.encode() if len(value) >= 32 else None


def _sign(fields, key):
    message = "&".join(f"{k}={fields[k]}" for k in FIELDS).encode()
    return base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()[:18]).decode().rstrip("=")


def facts(result, identifier):
    """The signed facts for one report."""
    mode = result.get("mode", "reference")
    if mode == "academic":
        compared = (result.get("fulltext") or {}).get("compared", 0) + sum(
            1 for s in result.get("sources") or [] if s.get("coverage") == "abstract")
    elif mode == "corpus":
        compared = result.get("source_count") or 0
    else:
        compared = 1
    percent = result.get("percent")
    return {"r": identifier[:12], "d": re.sub(r"[-:]", "", str(result.get("date", "")))[:13],
            "h": str(result.get("sha256", ""))[:16], "m": MODES.get(mode, "r"),
            "p": "NE" if percent is None else f"{float(percent):g}", "w": str(int(result.get("total_tokens") or 0)),
            "n": str(int(compared)), "l": result.get("language", "en") if result.get("language") in ("uz", "ru", "en") else "en"}


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
    if not key or not all(re.fullmatch(PATTERNS[k], fields[k]) for k in FIELDS):
        return False, fields
    return hmac.compare_digest(_sign(fields, key), str(params.get("s", ""))), fields
