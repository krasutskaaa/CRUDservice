"""Healthcheck контейнера без curl/wget: лише стандартна бібліотека Python.
Код виходу 0 тільки якщо /healthz повернув 200 (тобто база доступна)."""
import os
import sys
import urllib.request

port = os.environ.get("PORT", "8000")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:  # 503 теж потрапляє сюди (HTTPError)
    sys.exit(1)
