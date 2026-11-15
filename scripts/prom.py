"""Клієнт HTTP API Prometheus (лише стандартна бібліотека) і запити лабораторної 2."""
import json
import os
import time
import urllib.parse
import urllib.request

DB = os.environ.get("POSTGRES_DB", "admissions")
SCENARIO = 'method="GET",path="/admissions/{admission_id}"'

QUERIES = {
    "bucket": f'sum by (le) (http_request_duration_seconds_bucket{{{SCENARIO}}})',
    "rps_by_instance": f'sum by (instance) (rate(http_requests_total{{{SCENARIO}}}[15s]))',
    "cpu_by_instance": 'sum by (instance) (rate(process_cpu_seconds_total{job="web"}[15s]))',
    "inprogress_by_instance": 'sum by (instance) (http_requests_in_progress)',
    "pg_connections_by_state": 'sum by (state) (pg_connections_count)',
    "pg_xact_per_s": (f'sum(rate(pg_stat_database_xact_commit{{datname="{DB}"}}[15s])) + '
                      f'sum(rate(pg_stat_database_xact_rollback{{datname="{DB}"}}[15s]))'),
}


def _get(base, path, params=None):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def range_q(base, query, start, end, step=5):
    return _get(base, "/api/v1/query_range",
                {"query": query, "start": start, "end": end, "step": step})["data"]["result"]


def web_targets(base):
    data = _get(base, "/api/v1/targets")["data"]["activeTargets"]
    return [t for t in data if t["labels"].get("job") == "web"]


def wait_targets(base, n, timeout=180):
    """Чекає, поки рівно n цілей job=web мають стан UP."""
    t = time.time()
    last = []
    while time.time() - t < timeout:
        try:
            last = web_targets(base)
            if len(last) == n and all(x["health"] == "up" for x in last):
                return [x["labels"]["instance"] for x in last]
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"очікувалось {n} цілей web UP, зараз: "
                       f"{[(x['labels'].get('instance'), x['health']) for x in last]}")


def collect(base, start, end, step=5):
    out = {}
    for name, q in QUERIES.items():
        try:
            out[name] = {"query": q, "result": range_q(base, q, start, end, step)}
        except Exception as e:
            out[name] = {"query": q, "error": str(e), "result": []}
    return out
