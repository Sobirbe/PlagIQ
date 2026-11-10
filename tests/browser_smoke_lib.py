"""Shared helper: start an isolated uvicorn process and wait for /health."""
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def start(module, port, env):
    proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', module+':app', '--host', '127.0.0.1',
                             '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            urlopen(f'http://127.0.0.1:{port}/health', timeout=1).close()
            return proc
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(proc.stderr.read().decode())
            time.sleep(.1)
    proc.terminate()
    raise RuntimeError('Test server did not start')
