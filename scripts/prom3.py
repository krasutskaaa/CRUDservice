"""Запити Prometheus лабораторної 3 (кластер k3d, мітка pod з kubernetes_sd)."""
from prom import SCENARIO, _get, range_q

NS = 'namespace="lab3"'
WEB_C = f'{NS},container="web"'

RANGE = {
    "rps_by_pod": f'sum by (pod) (rate(http_requests_total{{job="web",{SCENARIO}}}[15s]))',
    "cpu_by_pod": f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{WEB_C}}}[30s]))',
    "throttling_by_pod": (f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{WEB_C}}}[30s]))'
                          f' / sum by (pod) (rate(container_cpu_cfs_periods_total{{{WEB_C}}}[30s]))'),
    "ready": f'kube_deployment_status_replicas_ready{{{NS},deployment="web"}}',
    "desired": f'kube_horizontalpodautoscaler_status_desired_replicas{{{NS},horizontalpodautoscaler="web"}}',
    "pg_connections_by_state": 'sum by (state) (pg_connections_count)',
}


def window(w):
    """Запити за вікно сходинки w секунд (інстант-запит у кінці вікна)."""
    return {
        "rps_by_pod": f'sum by (pod) (increase(http_requests_total{{job="web",{SCENARIO}}}[{w}s])) / {w}',
        "bucket_by_pod": f'sum by (pod, le) (increase(http_request_duration_seconds_bucket{{{SCENARIO}}}[{w}s]))',
        "throttled_by_pod": f'sum by (pod) (increase(container_cpu_cfs_throttled_periods_total{{{WEB_C}}}[{w}s]))',
        "periods_by_pod": f'sum by (pod) (increase(container_cpu_cfs_periods_total{{{WEB_C}}}[{w}s]))',
        "cpu_by_pod": f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{WEB_C}}}[{w}s]))',
    }


def instant(base, query, t):
    return _get(base, "/api/v1/query", {"query": query, "time": t})["data"]["result"]


def collect_range(base, start, end, step=1):
    out = {}
    for name, q in RANGE.items():
        try:
            out[name] = {"query": q, "result": range_q(base, q, start, end, step)}
        except Exception as e:  # noqa: BLE001
            out[name] = {"query": q, "error": str(e), "result": []}
    return out


def collect_window(base, t_end, w):
    out = {}
    for name, q in window(w).items():
        try:
            out[name] = {"query": q, "result": instant(base, q, t_end)}
        except Exception as e:  # noqa: BLE001
            out[name] = {"query": q, "error": str(e), "result": []}
    return out


RAW = {
    "cfs_periods": f"container_cpu_cfs_periods_total{{{WEB_C}}}",
    "cfs_throttled": f"container_cpu_cfs_throttled_periods_total{{{WEB_C}}}",
    "cpu_usage": f"container_cpu_usage_seconds_total{{{WEB_C}}}",
}


def collect_raw(base, start, end):
    """Сирі зразки лічильників cAdvisor за [start, end] → {імʼя: {pod: [(t, v)]}}.
    cAdvisor оновлює лічильники раз на 10-15 с і віддає їх із власними мітками часу, тому
    rate/increase за коротке вікно часто порожні; частки рахуються з цих зразків."""
    dur = int(end - start) + 1
    out = {}
    for name, sel in RAW.items():
        by_pod = {}
        for s in instant(base, f"{sel}[{dur}s]", end):
            pod = s["metric"].get("pod", "?")
            pts = [(float(t), float(v)) for t, v in s["values"]]
            if len(pts) > len(by_pod.get(pod, [])):
                by_pod[pod] = pts
        out[name] = by_pod
    return out


def series_count(base, query):
    return len(_get(base, "/api/v1/query", {"query": query})["data"]["result"])
