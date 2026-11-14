"""Operational figures for the owner dashboard: one row per check, no document text, no file name, no user.

Recorded for every check that started (finished or failed): mode, language, word count, status, error code,
duration, coverage shortfalls, OpenAlex queries and failures, full-text downloads, web-search errors, library
state, OCR pages and time, AI-detector use, edited-copy words, amount charged. Rows older than METRICS_DAYS
(default 90) are deleted.
"""
import math
import os
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

_lock = threading.Lock()
PANGRAM_USD_PER_100_WORDS = 0.05          # list price per started 100 words (aidetect.py)
COLUMNS = ("time", "mode", "language", "words", "status", "error", "seconds", "issues", "queries", "queries_failed",
           "downloads", "downloads_ok", "downloads_cut", "web_searches", "web_errors", "library", "ocr_pages",
           "ocr_seconds", "ai", "ai_words", "edited_words", "charged")


def days():
    try:
        return min(max(int(os.environ.get("METRICS_DAYS", "90")), 7), 730)
    except ValueError:
        return 90


def _path():
    configured = os.environ.get("METRICS_DB")
    if configured:
        return configured
    import auth
    return str(Path(auth._db_path()).with_name("plagiq-metrics.sqlite3"))


def _connect():
    db = sqlite3.connect(_path(), timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS checks (time REAL NOT NULL, mode TEXT, language TEXT, words INTEGER, status TEXT, "
               "error TEXT, seconds REAL, issues TEXT, queries INTEGER, queries_failed INTEGER, downloads INTEGER, "
               "downloads_ok INTEGER, downloads_cut INTEGER, web_searches INTEGER, web_errors INTEGER, library TEXT, "
               "ocr_pages INTEGER, ocr_seconds REAL, ai TEXT, ai_words INTEGER, edited_words INTEGER, charged INTEGER)")
    db.execute("CREATE INDEX IF NOT EXISTS checks_time ON checks (time)")
    return db


def _insert(row):
    try:
        with _lock, closing(_connect()) as db, db:
            db.execute(f"INSERT INTO checks ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                       tuple(row.get(c) for c in COLUMNS))
            db.execute("DELETE FROM checks WHERE time < ?", (time.time() - days() * 86400,))
    except sqlite3.Error:
        pass                                   # figures are never worth failing a check for


def record_check(result, seconds, charged=None):
    fulltext = result.get("fulltext") or {}
    reasons = fulltext.get("reasons") or {}
    web = result.get("web") or {}
    library = result.get("library") or {}
    ocr = result.get("ocr") or {}
    ai = result.get("ai") or {}
    _insert({"time": time.time(), "mode": result.get("mode"), "language": result.get("language"),
             "words": int(result.get("total_tokens") or 0), "status": result.get("status"), "seconds": round(seconds, 2),
             "issues": ",".join(i["code"] for i in result.get("coverage_issues") or []) or None,
             "queries": result.get("queries"), "queries_failed": result.get("failed_queries"),
             "downloads": fulltext.get("attempted"), "downloads_ok": fulltext.get("compared"),
             "downloads_cut": sum(n for k, n in reasons.items() if k in ("timed out", "website not reachable")) or None,
             "web_searches": web.get("searches") if web.get("enabled") else None,
             "web_errors": len(web.get("errors") or []) if web.get("enabled") else None,
             "library": ("error:" + str(library["error"])) if library.get("error") else ("ok" if library.get("searched") else None),
             "ocr_pages": ocr.get("pages"), "ocr_seconds": ocr.get("seconds"),
             "ai": ai.get("status"), "ai_words": int(result.get("total_tokens") or 0) if ai.get("status") == "done" else None,
             "edited_words": result.get("edited_tokens"), "charged": charged})


def record_failure(mode, status, code, seconds):
    _insert({"time": time.time(), "mode": mode, "status": status, "error": code, "seconds": round(seconds, 2)})


def _percentile(values, share):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, max(0, math.ceil(share * len(values)) - 1))]


def summary(hours, now=None):
    """Figures for the last `hours`."""
    now = now or time.time()
    try:
        with _lock, closing(_connect()) as db:
            rows = [dict(zip(COLUMNS, r)) for r in db.execute(f"SELECT {','.join(COLUMNS)} FROM checks WHERE time >= ?",
                                                                (now - hours * 3600,))]
    except sqlite3.Error:
        rows = []
    count = lambda pred: sum(1 for r in rows if pred(r))
    statuses, errors, issues = {}, {}, {}
    for r in rows:
        statuses[r["status"] or "unknown"] = statuses.get(r["status"] or "unknown", 0) + 1
        if r["error"]:
            errors[r["error"]] = errors.get(r["error"], 0) + 1
        for code in (r["issues"] or "").split(","):
            if code:
                issues[code] = issues.get(code, 0) + 1
    finished = [r for r in rows if not r["error"]]
    timing = {}
    for mode in sorted({r["mode"] for r in finished if r["mode"]}):
        secs = [r["seconds"] for r in finished if r["mode"] == mode and r["seconds"] is not None]
        timing[mode] = {"checks": len(secs), "p50": _percentile(secs, 0.5), "p95": _percentile(secs, 0.95), "max": max(secs) if secs else None}
    total = lambda key: sum(r[key] or 0 for r in rows)
    ai_words = [r["ai_words"] for r in rows if r["ai_words"]]
    return {
        "checks": len(rows), "statuses": statuses, "errors": errors, "issues": issues, "timing": timing,
        "words": total("words"),
        "openalex": {"checks": count(lambda r: r["queries"] is not None), "queries": total("queries"),
                     "failed": total("queries_failed")},
        "downloads": {"attempted": total("downloads"), "compared": total("downloads_ok"), "cut_off": total("downloads_cut")},
        "web": {"checks": count(lambda r: r["web_searches"] is not None), "searches": total("web_searches"),
                "errors": total("web_errors")},
        "library": {"searched": count(lambda r: r["library"] == "ok"),
                    "errors": count(lambda r: (r["library"] or "").startswith("error:"))},
        "ocr": {"checks": count(lambda r: r["ocr_pages"]), "pages": total("ocr_pages"),
                "seconds_per_page": round(total("ocr_seconds") / total("ocr_pages"), 1) if total("ocr_pages") else None},
        "ai": {"checks": len(ai_words), "words": sum(ai_words),
               "usd": round(sum(math.ceil(w / 100) * PANGRAM_USD_PER_100_WORDS for w in ai_words), 2),
               "failed": count(lambda r: r["ai"] == "error")},
        "edited": {"checks": count(lambda r: r["edited_words"]), "words": total("edited_words")},
        "charged": total("charged"),
    }


def per_day(n=14, now=None):
    """[(Tashkent date, checks, not completed)] for the last n days, oldest first."""
    now = now or time.time()
    zone = timezone(timedelta(hours=5))
    today = datetime.fromtimestamp(now, zone).date()
    start = datetime.combine(today - timedelta(days=n - 1), datetime.min.time(), zone).timestamp()
    try:
        with _lock, closing(_connect()) as db:
            rows = db.execute("SELECT time, status, error FROM checks WHERE time >= ?", (start,)).fetchall()
    except sqlite3.Error:
        rows = []
    out = {today - timedelta(days=k): [0, 0] for k in range(n)}
    for t, status, error in rows:
        day = datetime.fromtimestamp(t, zone).date()
        if day in out:
            out[day][0] += 1
            out[day][1] += 1 if (error or status in ("incomplete_coverage", "failed_search")) else 0
    return [(d.isoformat(), c, bad) for d, (c, bad) in sorted(out.items())]


def storage():
    """Sizes of the website's databases and the free space on their volume."""
    import auth
    import reports_store
    files = {"Accounts and payments": auth._db_path(), "Stored reports (24 h)": reports_store._path(), "Monitoring figures": _path()}
    sizes = {}
    for name, path in files.items():
        sizes[name] = sum(Path(p).stat().st_size for p in (path, path + "-wal") if Path(p).exists())
    try:
        disk = shutil.disk_usage(Path(auth._db_path()).parent)
        free, capacity = disk.free, disk.total
    except OSError:
        free = capacity = None
    try:
        reports = reports_store.stats()
    except sqlite3.Error:
        reports = None
    return {"files": sizes, "free": free, "total": capacity, "reports": reports}
