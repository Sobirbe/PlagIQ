"""Read uploaded and downloaded files in a separate, limited process.

PDF, Word and scanned files are parsed by large native libraries (MuPDF, Tesseract, lxml). A malformed or hostile
file could make them use all memory, run forever or crash. Here each file is read by a fresh helper process
(extract_worker.py) that limits itself before it touches the file:
  memory    EXTRACT_MEMORY_MB (768) of address space: OCR at 300 dpi measured at 441 MB, a 500-page PDF at 128 MB
  CPU time  OCR_MAX_SECONDS + 60 (60 s without OCR); wall clock EXTRACT_SECONDS (60) without OCR
  files     at most 64 MB written; no core dumps
and is killed when it runs longer than its wall-clock limit. It gets a minimal environment (no API keys, payment
keys or other secrets). If it crashes or hits a limit, the website keeps running and the user gets
"file_too_complex". At most EXTRACT_CONCURRENCY (2) helpers run at once; OCR itself stays one job at a time
across all helpers (a lock file, see extract._ocr_lock).

EXTRACT_SANDBOX=0 reads files in the website process instead (as before).
"""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import extract

WORKER = str(Path(__file__).with_name("extract_worker.py"))
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX", "OCR_ENABLED", "OCR_MAX_PAGES", "OCR_MAX_SECONDS", "OCR_DPI",
            "OCR_QUEUE_SECONDS", "TMPDIR")
log = logging.getLogger("plagiq.check")
TOO_COMPLEX = "This file needs more memory or time to read than PlagIQ allows. Save it again (for example as a new PDF) or split it."


def _bounded(name, default, low, high):
    try:
        return min(max(int(os.environ.get(name, default)), low), high)
    except ValueError:
        return default


def enabled():
    return os.environ.get("EXTRACT_SANDBOX", "1") != "0" and sys.platform != "win32"


_slots = None
_slots_lock = threading.Lock()


def _semaphore():
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(_bounded("EXTRACT_CONCURRENCY", 2, 1, 8))
        return _slots


def extract_text(name, content, max_pages=extract.MAX_PDF_PAGES, ocr=False, info=None):
    """Same contract as extract.extract_text, run in a limited helper process."""
    info = {} if info is None else info
    if not enabled():
        return extract.extract_text(name, content, max_pages=max_pages, ocr=ocr, info=info)
    ocr_seconds = _bounded("OCR_MAX_SECONDS", 240, 10, 3600)
    queue = _bounded("OCR_QUEUE_SECONDS", 60, 0, 600)
    wall = (ocr_seconds + queue + 60) if ocr else _bounded("EXTRACT_SECONDS", 60, 5, 600)
    slots = _semaphore()
    if not slots.acquire(timeout=60):
        raise extract.ExtractError(503, "server_busy", "PlagIQ is reading other files. Try again in a minute.")
    try:
        return _run(name, content, max_pages, ocr, info, wall, ocr_seconds)
    finally:
        slots.release()


def _run(name, content, max_pages, ocr, info, wall, ocr_seconds):
    header = {"name": name, "max_pages": max_pages, "ocr": ocr, "memory_mb": _bounded("EXTRACT_MEMORY_MB", 768, 256, 4096),
              "cpu_seconds": (ocr_seconds + 60) if ocr else 60}
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
    env.update(PYTHONDONTWRITEBYTECODE="1", OMP_THREAD_LIMIT="1")
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-I", WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=str(Path(WORKER).parent), start_new_session=True)
    progress = info.get("progress")
    out, tail = [], []

    def feed():
        try:
            proc.stdin.write(json.dumps(header).encode() + b"\n" + content)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def read_out():
        out.append(proc.stdout.read())

    def read_err():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("progress ") and callable(progress):
                _, key, *detail = line.split(" ", 2)
                progress(key, (detail[0] or None) if detail else None)
            else:
                tail.append(line)
                del tail[:-20]

    threads = [threading.Thread(target=f, daemon=True) for f in (feed, read_out, read_err)]
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=wall)
    except subprocess.TimeoutExpired:
        _kill(proc)
        log.warning("file reading stopped after %ss (%s)", wall, Path(name).suffix.lower())
        raise extract.ExtractError(400, "file_too_complex", TOO_COMPLEX)
    for t in threads:
        t.join(5)
    raw = out[0] if out else b""
    if proc.returncode != 0 or not raw:
        reason = "killed by signal %s" % -proc.returncode if proc.returncode < 0 else (tail[-1][:200] if tail else "no output")
        log.warning("file reading failed (%s, %.1fs): %s", Path(name).suffix.lower(), time.monotonic() - started, reason)
        limit_hit = proc.returncode < 0 or any("MemoryError" in line or "memory" in line.lower() for line in tail)
        raise extract.ExtractError(400, "file_too_complex" if limit_hit else "extract_failed",
                                   TOO_COMPLEX if limit_hit else "Could not read this file. Check that it is a valid DOCX or PDF.")
    result = json.loads(raw)
    if not result.get("ok"):
        raise extract.ExtractError(result["status"], result["code"], result["message"])
    info.update(result.get("info") or {})
    return result["text"]


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)               # the helper and anything it started
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait(5)
