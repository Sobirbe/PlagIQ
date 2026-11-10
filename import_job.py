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
Manual run: python import_job.py uz --batch 50
"""
import argparse
import json
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import oa_import
from corpus import connect, corpus_version, detect_language, ingest, inventory
from extract import ExtractError, extract_text
from harvest import Fetcher, HarvestError


def staging_root():
    return Path(os.environ.get("STAGING_DIR") or Path(os.environ.get("CORPUS_DB", "corpus.sqlite3")).resolve().parent / "staging")


def run(language, batch=500, max_cost=0.20, db_path=None, root=None, fetcher=None, api=oa_import.OPENALEX):
    root = Path(root or staging_root())
    folder = root / f"openalex-{language}"
    args = argparse.Namespace(language=language, country=None, since=None, source_id=None, query=None, download=True,
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
    counts = {"imported": 0, "failed": 0, "language_corrected": 0}
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
    status = {"language": language, "started": started,
              "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "staged_total": len(stage.manifest), **counts,
              "failed_total": sum(v.startswith("failed") for v in imported.values()),
              "listing_complete": bool(stage.state.get("listing_complete")),
              "estimated_cost_usd": round(budget.spent, 4), "error": error,
              "corpus_sources": len(sources), "corpus_version": version}
    write_status(root, language, status)
    return status


def write_status(root, language, status):
    path = Path(root) / "import-status.json"
    data = json.loads(path.read_text("utf-8")) if path.exists() else {}
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
    parser.add_argument("language", choices=["uz", "ru", "en"])
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--max-cost-usd", type=float, default=0.20)
    a = parser.parse_args()
    print(json.dumps(run(a.language, a.batch, a.max_cost_usd), ensure_ascii=False, indent=1))
