"""Who may run which check, and what it costs: the one-time free trial, paid checks, discounted rechecks and the
owner's pilot-test allowances. All decisions are made here, on the server, from pricing.py; the browser only shows
them.

Free trial
  One per account, for life: a plagiarism check of up to pricing.trial_max_words() (1,000) words, results on screen
  only (no PDF, no QR certificate, no CSV). It is reserved in one SQLite transaction (BEGIN IMMEDIATE) before the
  check runs, so two checks started at once cannot both use it; it is used up only when the check produced a result
  and given back when the check failed for a technical reason. It is tied to the account, not to the browser, so
  clearing cookies or using another device does not bring it back. Linked sign-in methods (Telegram and e-mail, see
  auth.link_identity) are one account and share it. When an account is deleted, a keyed hash of each of its sign-in
  identities is kept (trial_markers), so deleting and re-creating the account does not restore the trial either.
  No IP address or device fingerprint is used.

Paid checks and rechecks
  A finished paid check (one that produced a percentage) is remembered with a fingerprint of the manuscript: a sample
  of hashed three-word sequences (about one in four), never the text. Within pricing.recheck_days() (30) of it, the
  same account may recheck a revised version of the same manuscript at pricing.recheck_percent() (80 %) of the price,
  at most pricing.recheck_limit() (2) times. "The same manuscript" means that at least SIMILARITY (35 %) of the
  smaller document's sampled sequences also occur in the other one; a revision that rewrote much of the text still
  qualifies, a different paper on the same topic does not (they share only a few per cent). A revision that has grown
  into a larger tier is not a discounted recheck: the larger tier's full price is shown before anything is paid.
  A recheck is a complete new check. It counts against the two rechecks only when it finishes with a result.
  Fingerprints are deleted with the check record when the account is deleted, and are not kept beyond the 30 days
  for any other purpose (purge()).

Pilot mode (PAYMENTS_ENABLED is not 1)
  Nothing is charged and prices are shown as future prices. Beyond the free trial, a check needs a test allowance the
  site owner granted on /admin (grant()): a number of checks up to a word limit, with an expiry. There are no
  unlimited free checks. A pilot check gives the full paid-check result (PDF and certificate) and is recorded as
  "pilot", never as a payment. Its rechecks follow the recheck rules and do not use further allowance.

Every reservation (trial, allowance or recheck) is one row in `reservations`, keyed by the check's job id:
reserved -> used (the check produced a result) or released (it failed). Reservations still open when the server
starts were cut off by a restart and are released.
"""
import hashlib
import hmac
import re
import sqlite3
import threading
import time
from contextlib import closing

import pricing

SIMILARITY = 0.35
SAMPLE = 4                      # keep shingles whose hash is divisible by SAMPLE
MAX_FINGERPRINT = 20000
STALE_SECONDS = 6 * 3600        # a reservation this old belongs to a check that cannot still be running
_lock = threading.Lock()
WORD = re.compile(r"[^\W_]+")
DAY = 86400


class EntitlementError(Exception):
    def __init__(self, code, message, status=402):
        super().__init__(message)
        self.code, self.status = code, status


# ------------------------------------------------------------- storage

def _connect():
    import auth
    db = sqlite3.connect(auth.db_file(), timeout=15, isolation_level=None)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS reservations (job TEXT PRIMARY KEY, user INTEGER NOT NULL, kind TEXT NOT NULL,
            ref TEXT, state TEXT NOT NULL, created INTEGER NOT NULL, closed INTEGER);
        CREATE INDEX IF NOT EXISTS reservations_user ON reservations (user, kind, state);
        CREATE INDEX IF NOT EXISTS reservations_ref ON reservations (kind, ref, state);
        CREATE TABLE IF NOT EXISTS trial_markers (marker TEXT PRIMARY KEY, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS grants (id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER NOT NULL,
            checks INTEGER NOT NULL, max_words INTEGER NOT NULL, note TEXT, created INTEGER NOT NULL,
            expires INTEGER NOT NULL, revoked INTEGER);
        CREATE INDEX IF NOT EXISTS grants_user ON grants (user);
        CREATE TABLE IF NOT EXISTS paid_checks (job TEXT PRIMARY KEY, user INTEGER NOT NULL, kind TEXT NOT NULL,
            tier TEXT NOT NULL, words INTEGER NOT NULL, amount_uzs INTEGER NOT NULL, root TEXT,
            fingerprint BLOB, created INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS paid_checks_user ON paid_checks (user, created);
    """)
    return db


def _begin(db):
    db.execute("BEGIN IMMEDIATE")          # one writer at a time: the check-and-reserve below cannot interleave


# ------------------------------------------------------------- manuscript fingerprints

def fingerprint(text):
    """Sorted sample of hashed word trigrams (8-byte ints). Not reversible into the text."""
    words = [w.casefold() for w in WORD.findall(text or "")]
    out = set()
    for i in range(len(words) - 2):
        h = int.from_bytes(hashlib.blake2b(" ".join(words[i:i + 3]).encode(), digest_size=8).digest(), "big")
        if h % SAMPLE == 0:
            out.add(h)
    return sorted(out)[:MAX_FINGERPRINT]


def similarity(a, b):
    """Share of the smaller fingerprint that the other one contains (0..1)."""
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _pack(fp):
    return b"".join(h.to_bytes(8, "big") for h in fp)


def _unpack(blob):
    blob = blob or b""
    return [int.from_bytes(blob[i:i + 8], "big") for i in range(0, len(blob), 8)]


# ------------------------------------------------------------- trial

def _markers(identities):
    import auth
    key = auth.marker_key()
    return [hmac.new(key, ident.encode(), hashlib.sha256).hexdigest() for ident in identities]


def _trial_taken(db, user_id, identities, now):
    row = db.execute("SELECT state, created FROM reservations WHERE user=? AND kind='trial' AND state IN "
                     "('reserved', 'used') ORDER BY created DESC LIMIT 1", (user_id,)).fetchone()
    if row and (row[0] == "used" or now - row[1] < STALE_SECONDS):
        return "used" if row[0] == "used" else "reserved"
    marks = _markers(identities)
    if marks and db.execute(f"SELECT 1 FROM trial_markers WHERE marker IN ({','.join('?' * len(marks))})",
                            marks).fetchone():
        return "used"
    return None


def trial_state(user, identities=()):
    """'available', 'reserved' (a trial check is running) or 'used'."""
    with _lock, closing(_connect()) as db:
        return _trial_taken(db, user["id"], identities, int(time.time())) or "available"


# ------------------------------------------------------------- pilot allowances (owner-granted)

def grant(user_id, checks, max_words, days, note=""):
    if not 1 <= int(checks) <= 50 or not 1 <= int(max_words) <= 150000 or not 1 <= int(days) <= 90:
        raise EntitlementError("invalid_grant", "1-50 checks, up to 150,000 words, 1-90 days.", 400)
    now = int(time.time())
    with _lock, closing(_connect()) as db:
        cur = db.execute("INSERT INTO grants (user, checks, max_words, note, created, expires) VALUES (?, ?, ?, ?, ?, ?)",
                         (int(user_id), int(checks), int(max_words), str(note)[:200], now, now + int(days) * DAY))
        return cur.lastrowid


def revoke(grant_id):
    with _lock, closing(_connect()) as db:
        return db.execute("UPDATE grants SET revoked=? WHERE id=? AND revoked IS NULL",
                          (int(time.time()), int(grant_id))).rowcount > 0


def _grant_rows(db, user_id, now):
    rows = db.execute("SELECT id, checks, max_words, expires FROM grants WHERE user=? AND revoked IS NULL AND expires>? "
                      "ORDER BY expires", (user_id, now)).fetchall()
    out = []
    for gid, checks, max_words, expires in rows:
        taken = db.execute("SELECT COUNT(*) FROM reservations WHERE kind='pilot' AND ref=? AND (state='used' OR "
                           "(state='reserved' AND created>?))", (str(gid), now - STALE_SECONDS)).fetchone()[0]
        out.append({"id": gid, "remaining": max(0, checks - taken), "max_words": max_words, "expires": expires})
    return out


def grants_for(user_id):
    with _lock, closing(_connect()) as db:
        return _grant_rows(db, user_id, int(time.time()))


def all_grants(limit=100):
    now = int(time.time())
    with _lock, closing(_connect()) as db:
        rows = db.execute("SELECT id, user, checks, max_words, note, created, expires, revoked FROM grants "
                          "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            used = db.execute("SELECT COUNT(*) FROM reservations WHERE kind='pilot' AND ref=? AND state='used'",
                              (str(r[0]),)).fetchone()[0]
            out.append(dict(zip(("id", "user", "checks", "max_words", "note", "created", "expires", "revoked"), r),
                            used=used, active=r[7] is None and r[6] > now))
        return out


# ------------------------------------------------------------- rechecks

def _recheck_candidates(db, user_id, fp, now):
    """Original paid checks of this account still open for rechecks, with their best similarity to `fp`."""
    roots = db.execute("SELECT job, tier, words, amount_uzs, expires, kind FROM paid_checks WHERE user=? AND root IS NULL "
                       "AND expires>? ORDER BY created DESC LIMIT 50", (user_id, now)).fetchall()
    out = []
    for job, tier, words, amount, expires, kind in roots:
        taken = db.execute("SELECT COUNT(*) FROM reservations WHERE kind='recheck' AND ref=? AND (state='used' OR "
                           "(state='reserved' AND created>?))", (job, now - STALE_SECONDS)).fetchone()[0]
        remaining = pricing.recheck_limit() - taken
        if remaining <= 0:
            continue
        prints = [_unpack(r[0]) for r in db.execute("SELECT fingerprint FROM paid_checks WHERE job=? OR root=?", (job, job))]
        score = max((similarity(fp, p) for p in prints), default=0.0)
        out.append({"root": job, "tier": tier, "words": words, "expires": expires, "remaining": remaining,
                    "similarity": round(score, 3), "pilot": kind == "pilot"})
    return out


# ------------------------------------------------------------- the decision

def decide(user, words, text, ai_only=False, extras=False, payments_on=False, identities=()):
    """What this check would be and cost. Never reserves anything.

    kind: "trial" (free, on-screen only), "recheck" (discounted), "paid" (full price), "pilot" (owner allowance,
    nothing charged) or "blocked" (with `reason`). `extras` = AI detector or archive requested (not part of a trial).
    """
    now = int(time.time())
    base = pricing.paid_quote(words)
    out = {"words": words, "tier": base["id"], "max_words": base["max_words"], "full_price_uzs": base["price_uzs"],
           "price_uzs": base["price_uzs"], "fits": base["fits"], "recheck": None, "exports": True,
           "payments_enabled": bool(payments_on)}
    fp = fingerprint(text)
    with _lock, closing(_connect()) as db:
        trial = _trial_taken(db, user["id"], identities, now) or "available"
        out["trial"] = trial
        if (trial == "available" and not ai_only and not extras and 0 < words <= pricing.trial_max_words()):
            out.update(kind="trial", tier="free", price_uzs=0, exports=False)
            return out
        if not base["fits"]:
            out.update(kind="blocked", reason="too_long_for_plans")
            return out
        candidates = [c for c in _recheck_candidates(db, user["id"], fp, now) if c["similarity"] >= SIMILARITY]
        candidates.sort(key=lambda c: (-c["similarity"], c["expires"]))
        if candidates and not ai_only:
            best = candidates[0]
            original = pricing.tier(best["tier"]) or {}
            info = {**best, "days_left": max(0, (best["expires"] - now + DAY - 1) // DAY),
                    "original_tier": best["tier"], "original_words": best["words"]}
            if (original.get("max_words") or 0) >= words:
                price = pricing.recheck_price(original.get("price_uzs") or base["price_uzs"])
                out.update(kind="recheck", tier=best["tier"], price_uzs=price, recheck=info,
                           full_price_uzs=original.get("price_uzs") or base["price_uzs"])
                if not payments_on:
                    out.update(price_uzs=0, future_price_uzs=price) if best["pilot"] else out.update(
                        kind="blocked", reason="payments_off", future_price_uzs=price)
                return out
            out["recheck_upgrade"] = {**info, "new_tier": base["id"]}     # grew into a larger tier: full price
        if payments_on:
            out["kind"] = "paid"
            return out
        usable = [g for g in _grant_rows(db, user["id"], now) if g["remaining"] > 0 and g["max_words"] >= words]
        out["future_price_uzs"] = base["price_uzs"]
        if usable:
            out.update(kind="pilot", price_uzs=0, grant=usable[0]["id"])
        else:
            out.update(kind="blocked", reason="trial_used" if trial != "available" else
                       ("trial_too_long" if words > pricing.trial_max_words() else "trial_no_extras"))
        return out


def reserve(user, job, decision, identities=()):
    """Reserve what `decision` needs for check `job`, atomically re-checking that it is still free. Returns the
    decision actually reserved (which may differ if something changed meanwhile) or raises EntitlementError."""
    kind = decision.get("kind")
    if kind == "blocked":
        raise EntitlementError(decision.get("reason") or "payment_required", "This check is not available.")
    if kind == "paid":
        return decision                         # paid from the balance (payments.charge); nothing to reserve here
    now = int(time.time())
    with _lock, closing(_connect()) as db:
        _begin(db)
        try:
            if kind == "trial":
                if _trial_taken(db, user["id"], identities, now):
                    raise EntitlementError("trial_used", "The free check has already been used on this account.")
                ref = None
            elif kind == "pilot":
                usable = [g for g in _grant_rows(db, user["id"], now)
                          if g["remaining"] > 0 and g["max_words"] >= decision["words"]]
                if not usable:
                    raise EntitlementError("pilot_allowance_used", "No test checks are left on this account.")
                ref = str(usable[0]["id"])
            elif kind == "recheck":
                root = decision["recheck"]["root"]
                row = db.execute("SELECT expires FROM paid_checks WHERE job=? AND user=? AND root IS NULL",
                                 (root, user["id"])).fetchone()
                taken = db.execute("SELECT COUNT(*) FROM reservations WHERE kind='recheck' AND ref=? AND (state='used' "
                                   "OR (state='reserved' AND created>?))", (root, now - STALE_SECONDS)).fetchone()[0]
                if not row or row[0] <= now or taken >= pricing.recheck_limit():
                    raise EntitlementError("recheck_unavailable", "This recheck is no longer available. Check the price again.", 409)
                ref = root
            else:
                raise EntitlementError("invalid_request", "Unknown check type.", 400)
            db.execute("INSERT INTO reservations (job, user, kind, ref, state, created) VALUES (?, ?, ?, ?, 'reserved', ?)",
                       (job, user["id"], kind, ref, now))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return decision


def consume(job, user, decision, text, identities=(), remember=True):
    """The check produced a result: the reservation is used, and a paid, pilot or recheck run is remembered for
    later rechecks (not AI-detector-only checks: remember=False)."""
    now = int(time.time())
    kind = decision.get("kind")
    with _lock, closing(_connect()) as db:
        _begin(db)
        try:
            db.execute("UPDATE reservations SET state='used', closed=? WHERE job=? AND state='reserved'", (now, job))
            if kind == "trial":
                for marker in _markers(identities):
                    db.execute("INSERT OR IGNORE INTO trial_markers (marker, created) VALUES (?, ?)", (marker, now))
            elif kind in ("paid", "pilot", "recheck") and remember:
                root = decision["recheck"]["root"] if kind == "recheck" else None
                if root:
                    expires = db.execute("SELECT expires FROM paid_checks WHERE job=?", (root,)).fetchone()
                    expires = expires[0] if expires else now
                else:
                    expires = now + pricing.recheck_days() * DAY
                db.execute("INSERT OR REPLACE INTO paid_checks (job, user, kind, tier, words, amount_uzs, root, fingerprint, "
                           "created, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (job, user["id"], "pilot" if kind == "pilot" or (kind == "recheck" and not decision.get(
                               "payments_enabled")) else kind, decision["tier"], int(decision["words"]),
                            int(decision.get("charged_uzs", decision.get("price_uzs") or 0)), root,
                            _pack(fingerprint(text)), now, expires))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def release(job):
    """The check failed: whatever it reserved is free again."""
    with _lock, closing(_connect()) as db:
        return db.execute("UPDATE reservations SET state='released', closed=? WHERE job=? AND state='reserved'",
                          (int(time.time()), job)).rowcount > 0


def release_interrupted():
    """At start-up: reservations of checks that a restart cut off are released."""
    with _lock, closing(_connect()) as db:
        return db.execute("UPDATE reservations SET state='released', closed=? WHERE state='reserved'",
                          (int(time.time()),)).rowcount


def summary(user, identities=()):
    """For "My checks": trial state, open rechecks and pilot allowances."""
    now = int(time.time())
    with _lock, closing(_connect()) as db:
        trial = _trial_taken(db, user["id"], identities, now) or "available"
        rows = db.execute("SELECT job, kind, tier, words, amount_uzs, root, created, expires FROM paid_checks WHERE user=? "
                          "ORDER BY created DESC LIMIT 100", (user["id"],)).fetchall()
        checks = []
        for job, kind, tier, words, amount, root, created, expires in rows:
            used = 0 if root else db.execute("SELECT COUNT(*) FROM reservations WHERE kind='recheck' AND ref=? AND "
                                             "state='used'", (job,)).fetchone()[0]
            checks.append({"job": job, "kind": kind, "tier": tier, "words": words, "amount_uzs": amount, "recheck_of": root,
                           "created": created, "recheck_until": None if root else expires,
                           "rechecks_left": None if root else (max(0, pricing.recheck_limit() - used) if expires > now else 0)})
        return {"trial": trial, "trial_max_words": pricing.trial_max_words(), "paid_checks": checks,
                "grants": _grant_rows(db, user["id"], now)}


def forget(user, identities=()):
    """Account deletion: rechecks, fingerprints, allowances and reservations go. If the trial was used, a keyed hash
    of each sign-in identity stays in trial_markers so the trial is not restored by signing up again."""
    with _lock, closing(_connect()) as db:
        _begin(db)
        try:
            used = db.execute("SELECT 1 FROM reservations WHERE user=? AND kind='trial' AND state='used'",
                              (user["id"],)).fetchone()
            if used:
                for marker in _markers(identities):
                    db.execute("INSERT OR IGNORE INTO trial_markers (marker, created) VALUES (?, ?)", (marker, int(time.time())))
            for table in ("reservations", "paid_checks", "grants"):
                db.execute(f"DELETE FROM {table} WHERE user=?", (user["id"],))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def purge():
    """Fingerprints of checks whose recheck window has closed are dropped (the record of the check stays)."""
    with _lock, closing(_connect()) as db:
        return db.execute("UPDATE paid_checks SET fingerprint=NULL WHERE expires<? AND fingerprint IS NOT NULL",
                          (int(time.time()),)).rowcount
