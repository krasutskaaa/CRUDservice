"""Спільні дрібниці для скриптів (лише стандартна бібліотека)."""
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
RAW = RESULTS / "raw"


def call(method, url, body=None, timeout=10):
    """Повертає (status, headers, parsed_body). Не кидає виняток на 4xx/5xx."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, headers, raw = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        status, headers, raw = e.code, dict(e.headers), e.read()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode(errors="replace")
    return status, headers, parsed


def wait_health(base, timeout=120):
    """Чекає першої відповіді 200 на /healthz; повертає час очікування, с."""
    t = time.perf_counter()
    while time.perf_counter() - t < timeout:
        try:
            if call("GET", base + "/healthz", timeout=2)[0] == 200:
                return time.perf_counter() - t
        except Exception:
            pass
        time.sleep(0.05)
    raise TimeoutError("/healthz не відповів 200 вчасно")


def compose(*args, env=None, check=True):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env, check=check)
