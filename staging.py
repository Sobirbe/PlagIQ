"""Disk housekeeping for the engine's importers.

Downloaded texts and PDFs are only needed until they are in the library. A full volume
stops every import ("database or disk is full"), so:
  * import_and_clear() indexes staged files and deletes each one once it is indexed (or
    rejected), keeping only files that could not be processed because the disk is full;
  * leftover() finds staged files from earlier runs, so the next run retries and clears them;
  * low_disk() tells the engine to pause imports while free space is below
    max(IMPORT_MIN_FREE_MB, 10% of the volume), with a message for /admin.
"""
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path


def disk(path):
    """(free bytes, total bytes) of the volume holding `path`, or (None, None)."""
    try:
        usage = shutil.disk_usage(Path(path).resolve().parent if not Path(path).is_dir() else path)
        return usage.free, usage.total
    except OSError:
        return None, None


def low_disk(db_path=None):
    """A message when imports should pause for lack of space, else None."""
    db_path = db_path or os.environ.get("CORPUS_DB", "corpus.sqlite3")
    free, total = disk(db_path)
    if free is None:
        return None
    try:
        minimum = int(os.environ.get("IMPORT_MIN_FREE_MB", "100")) * 1024 * 1024
    except ValueError:
        minimum = 100 * 1024 * 1024
    if free < max(minimum, total // 10):
        return (f"Paused: only {free // (1024 * 1024)} MB free of {total // (1024 * 1024)} MB on the engine volume. "
                "Enlarge the volume (Railway: engine-volume, Settings) or lower CORPUS_MAX_SOURCES.")
    return None


def size(path):
    """Bytes used by all files under `path` (0 if it does not exist)."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def leftover(folder):
    """Manifest entries whose staged file is still on disk (not yet indexed and cleared)."""
    manifest = Path(folder) / "manifest.json"
    if not manifest.exists():
        return []
    try:
        entries = json.loads(manifest.read_text("utf-8"))
    except ValueError:
        return []
    return [e for e in entries if isinstance(e, dict) and (Path(folder) / str(e.get("text_file", ""))).is_file()]


def _full(exc):
    text = str(exc).lower()
    return "disk is full" in text or "no space left" in text or "index limit" in text


def import_and_clear(folder, entries, db_path=None):
    """Index entries one by one and delete each staged file once indexed or rejected.
    Returns {"imported", "failed", "stopped"}; "stopped" explains an early stop (full disk or limit)."""
    import corpus
    folder = Path(folder)
    result = {"imported": 0, "failed": 0, "stopped": None}
    batch = folder / ".batch-manifest.json"
    try:
        with closing(corpus.connect(db_path)) as db:
            for entry in entries:
                path = folder / str(entry.get("text_file", ""))
                if not path.is_file():
                    continue
                batch.write_text(json.dumps([entry], ensure_ascii=False), "utf-8")
                try:
                    corpus.import_manifest(db, batch)
                    result["imported"] += 1
                except (ValueError, sqlite3.OperationalError, OSError) as exc:
                    if _full(exc):
                        result["stopped"] = str(exc)[:200]
                        break
                    result["failed"] += 1
                path.unlink(missing_ok=True)
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")      # give the write-ahead log's space back
            except sqlite3.Error:
                pass
    except (sqlite3.OperationalError, OSError) as exc:
        result["stopped"] = str(exc)[:200]
    finally:
        batch.unlink(missing_ok=True)
    return result


def purge_leftovers(root):
    """Free space when the disk is low: delete staged files that were never indexed (they can be
    downloaded again) and forget them in the importers' state, so a later run fetches them anew.
    Returns bytes freed."""
    freed = 0
    root = Path(root)
    for manifest in list(root.glob("*/manifest.json")) + list(root.glob("*/*/manifest.json")):
        folder = manifest.parent
        stale = {str(e.get("text_file")) for e in leftover(folder)}
        if not stale:
            continue
        for name in stale:
            path = folder / name
            try:
                freed += path.stat().st_size
                path.unlink()
            except OSError:
                pass
        entries = [e for e in json.loads(manifest.read_text("utf-8")) if str(e.get("text_file")) not in stale]
        manifest.write_text(json.dumps(entries, ensure_ascii=False, indent=1), "utf-8")
        for state_name in ("state.json", "harvest-state.json"):     # cyberleninka / harvest.py resume state
            state_path = folder / state_name
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text("utf-8"))
                except ValueError:
                    continue
                done = state.get("done") or {}
                state["done"] = {k: v for k, v in done.items() if v not in stale}
                state_path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    return freed
