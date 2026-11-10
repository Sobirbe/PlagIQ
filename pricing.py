"""Word-count based price quotes. Display only: PlagIQ has no payment processing.

Tiers come from the PRICING_TIERS environment variable (JSON list) so the owner
can change them in Railway without code changes, e.g.

  [{"id": "free", "max_words": 2000, "price_uzs": 0},
   {"id": "researcher", "max_words": 15000, "price_uzs": 15000},
   {"id": "extended", "max_words": 45000, "price_uzs": 25000}]

Prices are labelled "planned" until real per-check costs are measured and a
payment provider is integrated; checks stay free during the pilot.
"""
import json
import os
import re

DEFAULT_TIERS = [
    {"id": "free", "max_words": 2000, "price_uzs": 0},
    {"id": "researcher", "max_words": 15000, "price_uzs": 15000},
    {"id": "extended", "max_words": 45000, "price_uzs": 25000},
]
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


def public_config():
    return {"currency": "UZS", "payments_enabled": False, "status": "planned", "tiers": tiers()}
