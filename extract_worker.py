"""Runs one file extraction in its own process (see sandbox.py). Reads a JSON header line and the file bytes from
stdin, writes one JSON line to stdout. Limits are applied first, before the file is touched; progress lines
("progress <key> <detail>") go to stderr."""
import json
import os
import resource
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # run with -I: only PlagIQ's own folder


def limit(memory_mb, cpu_seconds):
    resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)        # address space: no runaway memory
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))       # CPU time
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)            # no large files written
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))                              # no core dumps


def main():
    header = json.loads(sys.stdin.buffer.readline())
    limit(int(header["memory_mb"]), int(header["cpu_seconds"]))
    content = sys.stdin.buffer.read()
    import extract
    info = {"progress": lambda key, detail=None: (sys.stderr.write(f"progress {key} {detail or ''}\n"), sys.stderr.flush())}
    try:
        text = extract.extract_text(header["name"], content, max_pages=header["max_pages"], ocr=header["ocr"], info=info)
        info.pop("progress", None)
        out = {"ok": True, "text": text, "info": info}
    except extract.ExtractError as exc:
        out = {"ok": False, "status": exc.status, "code": exc.code, "message": str(exc)}
    if header.get("report_memory"):
        with open("/proc/self/status") as f:
            out["memory"] = {k: v.strip() for k, v in (line.split(":", 1) for line in f) if k in ("VmPeak", "VmHWM")}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
