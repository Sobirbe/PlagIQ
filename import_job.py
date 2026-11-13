"""Unattended OpenAlex import for the engine service (no shell needed on Railway).

Each run: collect up to `batch` new open-licence works (CC BY, CC BY-SA, CC0,
public domain) for each language through oa_import, then import every staged
work individually: language checked against the text, validated by the same
rules as corpus.py, PDF deleted after its text is indexed. Progress and every
failure reason are written to <staging>/import-status.json.

Configured on the engine with environment variables:
  AUTO_IMPORT=uz            languages, comma-separated (uz, ru, en)
  AUTO_IMPORT_BATCH=500     new works per language per run
  AUTO_IMPORT_HOURS=24      hours between runs
  AUTO_IMPORT_MAX_COST=0.20 OpenAlex spending cap per language per run (USD)
  AUTO_IMPORT_OPENALEX_PDF=1 when a work has no reachable PDF, use OpenAlex's cached copy
                            (about $0.01 each, always within AUTO_IMPORT_MAX_COST)
  STAGING_DIR=/data/staging
  AUTO_IMPORT_UZ_FROM_RU=1  with "uz" in AUTO_IMPORT (default on): also list open-licence works that OpenAlex
                            labels Russian but whose authors are at Uzbek institutions (country UZ). Titles
                            with Russian-only letters (ы) are skipped before download; the language of each
                            downloaded text is detected, and only texts that really are Uzbek are imported, as
                            Uzbek (Russian ones too if "ru" is in AUTO_IMPORT). Status key "uz-from-ru".
  AUTO_IMPORT_UZ_FROM_RU_BATCH=100, AUTO_IMPORT_UZ_FROM_RU_MAX_COST=0.05 (USD per run)
Manual run: python import_job.py uz --batch 50   (or: python import_job.py uz-from-ru --batch 20)
"""
import argparse
import json
import os
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import oa_import
from corpus import connect, corpus_version, detect_language, ingest, inventory
from extract import ExtractError, extract_text
from harvest import Fetcher, HarvestError


def staging_root():
    return Path(os.environ.get("STAGING_DIR") or Path(os.environ.get("CORPUS_DB", "corpus.sqlite3")).resolve().parent / "staging")


def run(language, batch=500, max_cost=0.20, db_path=None, root=None, fetcher=None, api=oa_import.OPENALEX,
        label=None, country=None, accept=None, status_key=None):
    """One import run. `label`/`country` list works by another OpenAlex language label (and authors'
    country); `accept` limits which detected text languages are imported (others fail with a reason)."""
    root = Path(root or staging_root())
    folder = root / (f"openalex-{status_key}" if status_key else f"openalex-{language}")
    args = argparse.Namespace(language=label or language, country=country, since=None, source_id=None, query=None, download=True,
                              prefer=language if label else None,
                              pdf_from="publisher", unpaywall_email=os.environ.get("UNPAYWALL_EMAIL") or None,
                              max_records=batch * 20, max_new=batch, resume=True,
                              openalex_fallback=os.environ.get("AUTO_IMPORT_OPENALEX_PDF") == "1")
    stage, budget = oa_import.Stage(folder), oa_import.Budget(max_cost)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    error = None
    try:
        oa_import.run_openalex(args, fetcher or Fetcher(delay=1.0), budget, stage, api=api)
    except (HarvestError, OSError, ValueError) as exc:
        error = str(exc)[:300]
    stage.save(budget)
    imported = stage.state.setdefault("imported", {})
    counts = {"imported": 0, "failed": 0, "language_corrected": 0, **({"relabelled": 0} if label else {})}
    with closing(connect(db_path)) as db:
        for entry in stage.manifest:
            key = entry["text_file"]
            path = folder / key
            if key in imported or not path.exists():
                continue
            meta = {k: v for k, v in entry.items() if k != "text_file"}
            try:
                text = extract_text(path.name, path.read_bytes(), max_pages=5000)
                detected = detect_language(text)
                if accept and (detected or meta["language"]) not in accept:
                    raise ValueError(f"full text is {detected or 'in no supported language'}; this run imports only "
                                     + "/".join(accept))
                if label and detected == language and meta["language"] != language:
                    counts["relabelled"] += 1
                if detected and detected != meta["language"]:
                    meta["language"] = detected
                    meta["permission_reference"] += f"; language set to {detected} from the full text"
                    counts["language_corrected"] += 1
                ingest(db, meta, text)
                imported[key] = "imported"
                counts["imported"] += 1
            except (ValueError, ExtractError) as exc:
                if "Index limit" in str(exc):
                    error = str(exc)
                    break
                imported[key] = "failed: " + str(exc)[:200]
                counts["failed"] += 1
            path.unlink(missing_ok=True)                   # text is in the index; keep disk use small
        stage.save(budget)
        sources = inventory(db)
        version = corpus_version(db)
    reasons = {}
    for outcome in imported.values():
        if outcome.startswith("failed"):
            reason = re.sub(r"\d+", "N", outcome.split(":", 1)[-1].strip())[:90]
            reasons[reason] = reasons.get(reason, 0) + 1
    status = {"language": language, "started": started,
              "failure_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5]),
              "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "staged_total": len(stage.manifest), **counts,
              "failed_total": sum(v.startswith("failed") for v in imported.values()),
              "listing_complete": bool(stage.state.get("listing_complete")),
              "estimated_cost_usd": round(budget.spent, 4), "error": error,
              "corpus_sources": len(sources), "corpus_version": version}
    write_status(root, status_key or language, status)
    return status


def run_uz_from_ru(batch=100, max_cost=0.05, db_path=None, root=None, fetcher=None, api=oa_import.OPENALEX):
    """Find Uzbek works that OpenAlex labels Russian (authors at Uzbek institutions) and import those whose
    extracted text is Uzbek, relabelled. Russian texts are imported only if "ru" is in AUTO_IMPORT."""
    accept = ("uz", "ru") if "ru" in settings()[0] else ("uz",)
    return run("uz", batch, max_cost, db_path, root, fetcher, api, label="ru", country="UZ", accept=accept,
               status_key="uz-from-ru")


def uz_from_ru_settings():
    on = "uz" in settings()[0] and os.environ.get("AUTO_IMPORT_UZ_FROM_RU", "1") != "0"
    return (on, _bounded("AUTO_IMPORT_UZ_FROM_RU_BATCH", 100, 1, 2000, int),
            _bounded("AUTO_IMPORT_UZ_FROM_RU_MAX_COST", 0.05, 0.0, 5.0, float))


def write_status(root, language, status):
    """Store a run's status. The time of the last successful run and of the last error are kept
    separately, so /admin can tell a current problem from an old one."""
    path = Path(root) / "import-status.json"
    data = json.loads(path.read_text("utf-8")) if path.exists() else {}
    previous = data.get(language) if isinstance(data.get(language), dict) else {}
    when = status.get("finished") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = {**status, "last_success": when if not status.get("error") else previous.get("last_success"),
              "error_at": when if status.get("error") else None}
    data[language] = status
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


def read_status(root=None):
    path = Path(root or staging_root()) / "import-status.json"
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _bounded(name, default, low, high, cast):
    try:
        return min(max(cast(os.environ.get(name, default)), low), high)
    except ValueError:
        return default


def settings():
    """(languages, batch, hours, max_cost) with safe bounds, so a typo cannot make the engine import non-stop."""
    langs = [l.strip() for l in os.environ.get("AUTO_IMPORT", "").split(",") if l.strip() in ("uz", "ru", "en")]
    return (langs, _bounded("AUTO_IMPORT_BATCH", 500, 1, 5000, int), _bounded("AUTO_IMPORT_HOURS", 24, 1, 720, float),
            _bounded("AUTO_IMPORT_MAX_COST", 0.20, 0.0, 5.0, float))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect and import open-licence OpenAlex works now.")
    parser.add_argument("language", choices=["uz", "ru", "en", "uz-from-ru"])
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--max-cost-usd", type=float, default=0.20)
    a = parser.parse_args()
    result = run_uz_from_ru(a.batch, a.max_cost_usd) if a.language == "uz-from-ru" else run(a.language, a.batch, a.max_cost_usd)
    print(json.dumps(result, ensure_ascii=False, indent=1))
