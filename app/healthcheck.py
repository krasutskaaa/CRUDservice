"""Healthcheck без curl/wget: перевіряє /livez (без бази), щоб не конкурувати із запитами за зʼєднання."""
import os
import sys
import urllib.request

port = os.environ.get("PORT", "8000")
path = os.environ.get("HEALTH_PATH", "/livez")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
