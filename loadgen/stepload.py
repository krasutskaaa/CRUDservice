#!/usr/bin/env python3
"""Ступінчастий генератор навантаження (відкритий контур, asyncio, лише стандартна бібліотека).

Профіль: прогрів --warmup с на --warmup-rate, далі сходинки по --step-duration с від --start-rate
з приростом --step. Моменти відправки обчислюються наперед від початку прогону; затримка
рахується від запланованого моменту. Інтенсивність ділиться між процесами за кількістю ядер.

Зупинка прогону: двічі поспіль досягнута < 0.9 заданої; або після сходинки, чия задана
інтенсивність >= --gen-limit (межа генератора G); або після --max-steps сходинок.
Досягнута інтенсивність сходинки: середнє за останні --window с (після --transient с
перехідного періоду) числа відповідей 2xx, завершених за секунду.
Запит, що перевищив --timeout с (від запланованого моменту), рахується відповіддю не 2xx;
помилки зʼєднання рахуються окремо.

Вихід: <prefix>_seconds.csv (посекундно), <prefix>_steps.json (сходинки, X(N), розкид).
"""
import argparse
import asyncio
import csv
import json
import multiprocessing as mp
import os
import platform
import random
import re
import statistics as st
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

TIMEOUT_STATUS, CONN_STATUS = -1, -2
CL_RE = re.compile(rb"content-length:\s*(\d+)")


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def make_segments(a):
    segs = [("warmup", 0, 0.0, a.warmup, a.warmup_rate)]
    off = float(a.warmup)
    for i in range(1, a.max_steps + 1):
        segs.append(("step", i, off, float(a.step_duration), a.start_rate + (i - 1) * a.step))
        off += a.step_duration
    return segs


class _Stale(Exception):
    pass


class Client:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.idle = []

    async def _once(self, path, reuse):
        if reuse and self.idle:
            r, w = self.idle.pop()
            fresh = False
        else:
            r, w = await asyncio.open_connection(self.host, self.port)
            fresh = True
        keep = False
        try:
            w.write(f"GET {path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n\r\n".encode())
            head = await r.readuntil(b"\r\n\r\n")
            status = int(head[9:12])
            low = head.lower()
            reusable = b"connection: close" not in low
            m = CL_RE.search(low)
            if b"transfer-encoding: chunked" in low:
                while True:
                    size = int((await r.readuntil(b"\r\n")).strip().split(b";")[0], 16)
                    await r.readexactly(size + 2)
                    if size == 0:
                        break
            elif m:
                n = int(m.group(1))
                if n:
                    await r.readexactly(n)
            elif status in (204, 304) or 100 <= status < 200:
                pass
            else:
                await r.read()
                reusable = False
            if reusable:
                self.idle.append((r, w))
                keep = True
            return status
        except (asyncio.IncompleteReadError, ConnectionError):
            if not fresh:
                raise _Stale()
            raise
        finally:
            if not keep:
                w.close()

    async def get(self, path):
        try:
            return await self._once(path, True)
        except _Stale:  # зʼєднання, яке сервер встиг закрити, повторюємо на новому
            return await self._once(path, False)


async def run_process(idx, cfg, ids, t0, stop_after, ok_arr):
    loop = asyncio.get_running_loop()
    # скасований за тайм-аутом запит на Windows може пізніше кинути ConnectionResetError
    # у «забутому» future; це шум, а не помилка вимірювання
    loop.set_exception_handler(
        lambda lp, ctx: None if isinstance(ctx.get("exception"), ConnectionError)
        else lp.default_exception_handler(ctx))
    client = Client(cfg["host"], cfg["port"])
    records, cpu, tasks = [], [], set()
    rng = random.Random(cfg["seed"] + idx)
    timeout = cfg["timeout"]
    n_sec = len(ok_arr)

    async def one(sched, path):
        start = time.time()
        remaining = timeout - (start - sched)
        if remaining <= 0:
            status = TIMEOUT_STATUS
        else:
            try:
                status = await asyncio.wait_for(client.get(path), remaining)
            except asyncio.TimeoutError:
                status = TIMEOUT_STATUS
            except Exception:
                status = CONN_STATUS
        done = time.time()
        if status >= 0 and done - sched > timeout:
            status = TIMEOUT_STATUS
        records.append((sched, start, done, status))
        if 200 <= status < 300:
            s = int(done - t0)
            if 0 <= s < n_sec:
                with ok_arr.get_lock():
                    ok_arr[s] += 1

    async def cpu_sampler():
        while True:
            cpu.append((time.time(), time.process_time()))
            await asyncio.sleep(1.0)

    sampler = asyncio.create_task(cpu_sampler())
    procs, tmpl = cfg["procs"], cfg["path"]
    try:
        for phase, step_i, off, dur, rate in cfg["segments"]:
            if step_i > stop_after.value:
                break
            per = rate / procs
            interval = 1.0 / per
            n = int(dur * per)
            base = t0 + off + idx * interval / procs
            k = 0
            while k < n:
                if step_i > stop_after.value:
                    break
                now = time.time()
                while k < n and base + k * interval <= now:
                    t = asyncio.create_task(one(base + k * interval, tmpl.format(id=rng.choice(ids))))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                    k += 1
                if k < n:
                    await asyncio.sleep(max(0.0, min(0.002, base + k * interval - time.time())))
        if tasks:
            await asyncio.gather(*list(tasks))
    finally:
        sampler.cancel()
    return records, cpu


def worker(idx, cfg, ids, ready, go, t0v, stop_after, ok_arr, q):
    ready.wait(timeout=120)
    go.wait(timeout=120)
    res = asyncio.run(run_process(idx, cfg, ids, t0v.value, stop_after, ok_arr))
    q.put((idx, res[0], res[1]))


def analyze(records, cpu_series, t0, a, segs, last_step):
    n_sec = int(segs[last_step][2] + segs[last_step][3]) + 1
    sent, ok, bad, conn = [0] * n_sec, [0] * n_sec, [0] * n_sec, [0] * n_sec
    lat = [[] for _ in range(n_sec)]
    for sched, start, done, status in records:
        s = int(start - t0)
        if 0 <= s < n_sec:
            sent[s] += 1
        d = int(done - t0)
        if 0 <= d < n_sec:
            if 200 <= status < 300:
                ok[d] += 1
            elif status == CONN_STATUS:
                conn[d] += 1
            else:
                bad[d] += 1
            if status != CONN_STATUS:
                lat[d].append((done - sched) * 1000)
    gen_cpu = [0.0] * n_sec
    for series in cpu_series:
        for (ta, ca), (tb, cb) in zip(series, series[1:]):
            s = int(tb - t0)
            if 0 <= s < n_sec and tb > ta:
                gen_cpu[s] += (cb - ca) / (tb - ta)
    seg_of = []
    for s in range(n_sec):
        cur = segs[0]
        for sg in segs:
            if sg[2] <= s < sg[2] + sg[3]:
                cur = sg
        seg_of.append(cur)
    rows = []
    for s in range(n_sec):
        l = sorted(lat[s])
        phase, step_i, _, _, rate = seg_of[s]
        rows.append({"second": s, "phase": phase, "step": step_i, "target": rate, "sent": sent[s],
                     "achieved": ok[s], "n2xx": ok[s], "non2xx": bad[s], "conn_errors": conn[s],
                     "p50_ms": round(percentile(l, 50), 3), "p95_ms": round(percentile(l, 95), 3),
                     "p99_ms": round(percentile(l, 99), 3), "gen_cpu_cores": round(gen_cpu[s], 3)})
    steps = []
    for phase, i, off, dur, rate in segs[1:last_step + 1]:
        lo = int(off + a.transient)
        hi = min(int(off + a.transient + a.window), int(off + dur))
        win = range(lo, hi)
        ach = [ok[s] for s in win]
        allat = sorted(x for s in win for x in lat[s])
        tot = sum(ok[s] + bad[s] + conn[s] for s in win)
        steps.append({
            "step": i, "target": rate, "window_seconds": [lo, hi], "window_epoch": [t0 + lo, t0 + hi],
            "achieved_mean": st.fmean(ach) if ach else float("nan"),
            "achieved_std": st.stdev(ach) if len(ach) > 1 else 0.0,
            "sent_mean": st.fmean(sent[s] for s in win) if ach else float("nan"),
            "client_p50_ms": percentile(allat, 50), "client_p95_ms": percentile(allat, 95),
            "client_p99_ms": percentile(allat, 99),
            "non2xx_share": (sum(bad[s] for s in win) / tot) if tot else float("nan"),
            "conn_errors": sum(conn[s] for s in win),
            "gen_cpu_cores": st.fmean(gen_cpu[s] for s in win) if ach else float("nan"),
        })
    return rows, steps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--path", default="/admissions/{id}")
    ap.add_argument("--ids-file", default="results/ids.json")
    ap.add_argument("--warmup", type=float, default=10)
    ap.add_argument("--warmup-rate", type=float, default=200)
    ap.add_argument("--start-rate", type=float, default=200)
    ap.add_argument("--step", type=float, default=200)
    ap.add_argument("--step-duration", type=float, default=20)
    ap.add_argument("--transient", type=float, default=5)
    ap.add_argument("--window", type=float, default=15)
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--gen-limit", type=float, default=0, help="межа генератора G, req/s (0 = не задано)")
    ap.add_argument("--timeout", type=float, default=5)
    ap.add_argument("--procs", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--instances", type=int, default=0, help="метадані")
    ap.add_argument("--out-prefix", required=True)
    a = ap.parse_args()

    ids = json.loads(Path(a.ids_file).read_text()) if "{id}" in a.path else [0]
    u = urlparse(a.base_url)
    segs = make_segments(a)
    total_sec = int(a.warmup + a.max_steps * a.step_duration + 15)
    cfg = dict(host=u.hostname, port=u.port or 80, path=a.path, timeout=a.timeout, procs=a.procs,
               seed=a.seed, segments=segs)
    ctx = mp.get_context("spawn")
    ready, go, q = ctx.Barrier(a.procs + 1), ctx.Event(), ctx.Queue()
    t0v = ctx.Value("d", 0.0)
    stop_after = ctx.Value("i", 10**6)
    ok_arr = ctx.Array("i", total_sec)
    ps = [ctx.Process(target=worker, args=(i, cfg, ids, ready, go, t0v, stop_after, ok_arr, q))
          for i in range(a.procs)]
    for p in ps:
        p.start()
    ready.wait(timeout=120)
    t0v.value = time.time() + 1.0
    t0 = t0v.value
    go.set()
    print(f"T0 {t0:.3f}", flush=True)

    below, stop_reason, last_step = 0, "max-steps", a.max_steps
    for phase, i, off, dur, rate in segs[1:]:
        delay = t0 + off + dur + 0.5 - time.time()
        if delay > 0:
            time.sleep(delay)
        lo, hi = int(off + a.transient), int(min(off + a.transient + a.window, off + dur))
        vals = [ok_arr[s] for s in range(lo, hi)]
        ach = sum(vals) / max(1, len(vals))
        print(f"step {i}: target {rate:.0f} achieved {ach:.1f}", flush=True)
        below = below + 1 if ach < 0.9 * rate else 0
        reason = None
        if below >= 2:
            reason = "двічі поспіль досягнута < 0.9 заданої"
        elif a.gen_limit and rate >= a.gen_limit:
            reason = "задана інтенсивність досягла межі генератора G"
        if reason:
            stop_after.value, stop_reason, last_step = i, reason, i
            break
    results = [q.get(timeout=a.timeout + 60) for _ in ps]
    for p in ps:
        p.join()
    records = [r for _, rec, _ in results for r in rec]
    rows, steps = analyze(records, [c for _, _, c in results], t0, a, segs, last_step)
    best = max(steps, key=lambda s: s["achieved_mean"])
    last = steps[-1]
    meta = {"t0": t0, "instances": a.instances, "args": vars(a), "cpu_count": os.cpu_count(),
            "platform": platform.platform(), "python": platform.python_version(),
            "stop_reason": stop_reason, "last_step": last_step}
    out = {"meta": meta, "steps": steps,
           "X": {"value": best["achieved_mean"], "spread": best["achieved_std"], "step": best["step"],
                 "target": best["target"], "client_p99_ms": best["client_p99_ms"]},
           "last_step_ratio": last["achieved_mean"] / last["target"]}
    pre = Path(a.out_prefix)
    pre.parent.mkdir(parents=True, exist_ok=True)
    Path(str(pre) + "_steps.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(str(pre) + "_seconds.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"X": round(out["X"]["value"], 1), "spread": round(out["X"]["spread"], 1),
                      "step": out["X"]["step"], "stop": stop_reason}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if not sys.flags.utf8_mode:
        import subprocess
        sys.exit(subprocess.run([sys.executable, "-X", "utf8", *sys.argv]).returncode)
    main()
