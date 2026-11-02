#!/usr/bin/env python3
"""Генератор навантаження.

Відкритий контур (--mode open): задана інтенсивність ділиться між процесами за
кількістю ядер; кожен процес наперед обчислює моменти відправки
t_k = t0 + k/rate і віддає кожен запит пулу потоків, тож цикл планування
не чекає на відповіді. Затримка міряється від ЗАПЛАНОВАНОГО моменту
відправки, тому черга всередині самого генератора теж видима в числах.

Закритий контур (--mode closed): --concurrency потоків, кожен шле наступний
запит лише після відповіді на попередній.

Прогрів (--warmup) іде тією самою інтенсивністю й не входить у результат.
Лише стандартна бібліотека Python.
"""
import argparse
import csv
import gzip
import http.client
import json
import multiprocessing as mp
import os
import platform
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def worker(idx, cfg, ids, ready, go, t0v, q):
    u = urlparse(cfg["base_url"])
    host, port = u.hostname, u.port or 80
    tls = threading.local()
    records = []  # (запланований, фактичний старт, завершення, статус)

    def do_request(sched, path):
        conn = getattr(tls, "conn", None)
        if conn is None:
            conn = tls.conn = http.client.HTTPConnection(host, port, timeout=cfg["timeout"])
        start = time.time()
        status = 0
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            r.read()
            status = r.status
        except Exception:
            conn.close()  # наступний запит перепідключиться сам
        records.append((sched if sched is not None else start, start, time.time(), status))

    ready.wait(timeout=120)
    go.wait(timeout=120)
    t0 = t0v.value
    rng = random.Random(cfg["seed"] + idx)
    tmpl = cfg["path"]

    if cfg["mode"] == "open":
        procs = cfg["procs"]
        interval = procs / cfg["rate"]               # інтервал між запитами цього процесу
        n_total = int((cfg["warmup"] + cfg["duration"]) / interval)
        offset = idx * interval / procs              # зсув фаз між процесами
        ex = ThreadPoolExecutor(max_workers=cfg["threads"])
        for k in range(n_total):
            t_s = t0 + offset + k * interval
            d = t_s - time.time()
            if d > 0:
                time.sleep(d)
            ex.submit(do_request, t_s, tmpl.format(id=rng.choice(ids)))
        ex.shutdown(wait=True)
    else:
        t_end = t0 + cfg["warmup"] + cfg["duration"]
        n_threads = cfg["conc_split"][idx]

        def loop(seed):
            r = random.Random(seed)
            while time.time() < t_end:
                do_request(None, tmpl.format(id=r.choice(ids)))

        ts = [threading.Thread(target=loop, args=(cfg["seed"] + idx * 1000 + j,))
              for j in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    q.put((idx, records))


def summarize(records, t0, warmup, duration):
    a, b = t0 + warmup, t0 + warmup + duration
    meas = [r for r in records if a <= r[0] < b]
    lat_all = sorted((r[2] - r[0]) * 1000 for r in meas)
    ok = lambda r: 200 <= r[3] < 300  # noqa: E731
    lat_ok = sorted((r[2] - r[0]) * 1000 for r in meas if ok(r))
    done_in = [r for r in records if a <= r[2] < b]
    lags = sorted((r[1] - r[0]) * 1000 for r in meas)
    # L: середнє за вікном число запитів у системі = інтеграл N(t)dt / T,
    # де запит у системі від запланованого моменту відправки до завершення.
    area = sum(max(0.0, min(r[2], b) - max(r[0], a)) for r in records)
    lam = len(meas) / duration
    w = (sum(lat_all) / len(lat_all) / 1000) if lat_all else float("nan")
    return {
        "measured_requests": len(meas),
        "achieved_rps": len(done_in) / duration,
        "achieved_2xx_rps": sum(1 for r in done_in if ok(r)) / duration,
        "non2xx_share": (sum(1 for r in meas if not ok(r)) / len(meas)) if meas else float("nan"),
        "p50_ms": percentile(lat_all, 50), "p95_ms": percentile(lat_all, 95),
        "p99_ms": percentile(lat_all, 99), "mean_ms": w * 1000,
        "ok_p50_ms": percentile(lat_ok, 50), "ok_p95_ms": percentile(lat_ok, 95),
        "ok_p99_ms": percentile(lat_ok, 99),
        "generator_lag_p99_ms": percentile(lags, 99),
        "little": {"lambda_rps": lam, "W_s": w, "lambda_times_W": lam * w, "L_measured": area / duration},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--path", default="/admissions/{id}", help="шаблон шляху; /noop для перевірки генератора")
    ap.add_argument("--ids-file", default=str(ROOT / "results" / "ids.json"))
    ap.add_argument("--mode", choices=["open", "closed"], default="open")
    ap.add_argument("--rate", type=float, default=100, help="запитів/с (open)")
    ap.add_argument("--concurrency", type=int, default=0, help="одночасних запитів (closed)")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--warmup", type=float, default=10)
    ap.add_argument("--procs", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--threads", type=int, default=64, help="потоків на процес (open)")
    ap.add_argument("--timeout", type=float, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--instances", type=int, default=0, help="метадані: кількість вебінстансів")
    ap.add_argument("--out", help="JSON із результатом; поруч пишеться .csv.gz із сирими записами")
    a = ap.parse_args()

    ids = json.loads(Path(a.ids_file).read_text()) if "{id}" in a.path else [0]
    procs = max(1, a.procs)
    conc_split = [a.concurrency // procs + (1 if i < a.concurrency % procs else 0) for i in range(procs)]
    if a.mode == "closed":
        assert a.concurrency >= 1, "для closed потрібен --concurrency"
        procs = min(procs, a.concurrency)
        conc_split = conc_split[:procs]
    cfg = dict(base_url=a.base_url, path=a.path, mode=a.mode, rate=a.rate, duration=a.duration,
               warmup=a.warmup, procs=procs, threads=a.threads, timeout=a.timeout, seed=a.seed,
               conc_split=conc_split)

    ctx = mp.get_context("spawn")
    ready, go, q = ctx.Barrier(procs + 1), ctx.Event(), ctx.Queue()
    t0v = ctx.Value("d", 0.0)
    ps = [ctx.Process(target=worker, args=(i, cfg, ids, ready, go, t0v, q)) for i in range(procs)]
    for p in ps:
        p.start()
    ready.wait(timeout=120)
    t0v.value = time.time() + 1.0
    go.set()
    t0 = t0v.value
    records = []
    for _ in ps:
        _, rec = q.get(timeout=a.warmup + a.duration + a.timeout + 60)
        records.extend(rec)
    for p in ps:
        p.join()

    s = summarize(records, t0, a.warmup, a.duration)
    result = {
        "config": {**{k: v for k, v in cfg.items() if k != "conc_split"}, "instances": a.instances,
                   "target_rps": a.rate if a.mode == "open" else None,
                   "concurrency": a.concurrency if a.mode == "closed" else None,
                   "cpu_count": os.cpu_count(), "platform": platform.platform(),
                   "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))},
        "summary": s,
    }
    print(json.dumps({"mode": a.mode, "target": a.rate if a.mode == "open" else a.concurrency,
                      "achieved_rps": round(s["achieved_rps"], 1), "p50": round(s["p50_ms"], 2),
                      "p95": round(s["p95_ms"], 2), "p99": round(s["p99_ms"], 2),
                      "non2xx": round(s["non2xx_share"], 4),
                      "gen_lag_p99_ms": round(s["generator_lag_p99_ms"], 2)}, ensure_ascii=False))
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        with gzip.open(out.with_suffix(".csv.gz"), "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(["scheduled", "started", "finished", "status"])
            w.writerows(records)


if __name__ == "__main__":
    main()
