"""Word-count based price quotes. Charged only when payments are on (payments.py, test mode only).

Tiers come from the PRICING_TIERS environment variable (JSON list) so the owner
can change them in Railway without code changes, e.g.

  [{"id": "free", "max_words": 1000, "price_uzs": 0},
   {"id": "researcher", "max_words": 3500, "price_uzs": 15000},
   {"id": "extended", "max_words": 15000, "price_uzs": 25000},
   {"id": "thesis", "max_words": 40000, "price_uzs": 35000},
   {"id": "phd", "max_words": 150000, "price_uzs": 39000}]

Other services (also display only):
  AI_PRICE_PER_WORD=3      AI detector, so'm per word
  AI_MIN_PRICE=6000        AI detector minimum per document
  PRICING_PACKAGES         JSON list, e.g. [{"checks": 10, "bonus": 2}, {"checks": 20, "bonus": 5}]
The certificate of check with QR code is included in every check.

Cost note: Pangram's list price is $0.05 per 100 words (about 6 so'm per word at
12,500 so'm/$). 3 so'm per word covers the cost only with Pangram's Professional
plan ($65/month including $200 of API credits, about 2 so'm per word when used up).

Prices are labelled "planned" and checks stay free while payments are off
(the default). With PAYMENTS_ENABLED=1 and PAYMENTS_MODE=test, checks are charged
from a balance topped up through the Payme or Click sandbox (no real money).
"""
import json
import os
import re

DEFAULT_TIERS = [
    {"id": "free", "max_words": 1000, "price_uzs": 0},
    {"id": "researcher", "max_words": 3500, "price_uzs": 15000},
    {"id": "extended", "max_words": 15000, "price_uzs": 25000},
    {"id": "thesis", "max_words": 40000, "price_uzs": 35000},
    {"id": "phd", "max_words": 150000, "price_uzs": 39000},         # PhD/DSc dissertations
]
DEFAULT_PACKAGES = [{"checks": 10, "bonus": 2}, {"checks": 20, "bonus": 5}]
# A word: letters/digits, with apostrophes inside (Uzbek ta'lim, o‘qish; English don't).
WORD = re.compile(r"[^\W_]+(?:['’ʼʻ‘`][^\W_]+)*")


def count_words(text):
    return len(WORD.findall(text or ""))


def tiers():
    """Validated tiers sorted by size; falls back to defaults if the setting is invalid."""
    raw = os.environ.get("PRICING_TIERS", "").strip()
    if not raw:
        return DEFAULT_TIERS
    try:
        data = json.loads(raw)
        cleaned = []
        for tier in data:
            if (not isinstance(tier, dict) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", str(tier.get("id", "")))
                    or type(tier.get("max_words")) is not int or tier["max_words"] <= 0
                    or type(tier.get("price_uzs")) is not int or tier["price_uzs"] < 0):
                raise ValueError(tier)
            cleaned.append({"id": tier["id"], "max_words": tier["max_words"], "price_uzs": tier["price_uzs"]})
        if not cleaned or len({t["max_words"] for t in cleaned}) != len(cleaned):
            raise ValueError("empty or duplicate sizes")
        return sorted(cleaned, key=lambda t: t["max_words"])
    except (ValueError, TypeError):
        return DEFAULT_TIERS


def quote(words):
    """The smallest tier that fits; 'contact' when the document is larger than every tier."""
    for tier in tiers():
        if words <= tier["max_words"]:
            return {**tier, "words": words, "fits": True}
    return {"id": "contact", "max_words": None, "price_uzs": None, "words": words, "fits": False}


def _int_env(name, default):
    try:
        value = int(os.environ.get(name, default))
        return value if value >= 0 else default
    except ValueError:
        return default


def ai_price(words):
    """Planned AI-detector price for a document of `words` words."""
    return max(_int_env("AI_MIN_PRICE", 6000), _int_env("AI_PRICE_PER_WORD", 3) * words)


def packages():
    raw = os.environ.get("PRICING_PACKAGES", "").strip()
    if not raw:
        return DEFAULT_PACKAGES
    try:
        data = [{"checks": int(p["checks"]), "bonus": int(p["bonus"])} for p in json.loads(raw)]
        if all(1 <= p["checks"] <= 1000 and 0 <= p["bonus"] <= p["checks"] for p in data):
            return data
    except (ValueError, TypeError, KeyError):
        pass
    return DEFAULT_PACKAGES


def public_config():
    return {"currency": "UZS", "payments_enabled": False, "status": "planned", "tiers": tiers(),
            "ai": {"per_word": _int_env("AI_PRICE_PER_WORD", 3), "minimum": _int_env("AI_MIN_PRICE", 6000)},
            "certificate": 0, "packages": packages()}
