#!/usr/bin/env python3
"""Оркестратор прогонів лабораторної 2.

  python scripts/run_lab2.py --noop                 # межа генератора G (запит /noop → 204)
  python scripts/run_lab2.py --instances 1          # X(1); аналогічно 2, 3, 4
Параметри ступінчастого профілю — за завданням (прогрів 10 с @200; сходинки 20 с від 200 з кроком 200).
Результати: results/lab2/ (G.json, n{N}_seconds.csv, n{N}_steps.json, prom_n{N}.json,
docker_stats_n{N}.jsonl, docker_info_n{N}.json).
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

from common import ROOT, RESULTS, compose, wait_health
import prom

OUT = RESULTS / "lab2"
STEPLOAD = str(ROOT / "loadgen" / "stepload.py")


def sample_stats(stop, path):
    """Раз на ~2 с знімає docker stats для всіх контейнерів проєкту."""
    with open(path, "w", encoding="utf-8") as f:
        while not stop.is_set():
            t = time.time()
            r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            for line in r.stdout.splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "admissions-lab" in d.get("Name", ""):
                    f.write(json.dumps({"t": t, **d}, ensure_ascii=False) + "\n")
            f.flush()
            stop.wait(1.0)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--noop", action="store_true")
    g.add_argument("--instances", type=int)
    ap.add_argument("--gen-limit", type=float, default=0,
                    help="G, req/s; якщо 0 — береться з results/lab2/G.json")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--prom-url", default=f"http://127.0.0.1:{os.environ.get('PROM_PORT', '9090')}")
    ap.add_argument("--procs", type=int, default=0)
    ap.add_argument("--no-up", action="store_true")
    ap.add_argument("--stats-interval", type=float, default=5.0,
                    help="пауза між вимірами docker stats, с (docker stats сам навантажує машину)")
    ap.add_argument("--no-stats", action="store_true", help="не знімати docker stats під час прогону")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    procs = ["--procs", str(a.procs)] if a.procs else []

    if a.noop:
        if not a.no_up:
            compose("up", "-d", "--build", env={**os.environ, "WEB_REPLICAS": "1"})
            wait_health(a.base_url)
        subprocess.run([sys.executable, STEPLOAD, "--base-url", a.base_url, "--path", "/noop",
                        "--out-prefix", str(OUT / "noop"), *procs], check=True)
        res = json.loads((OUT / "noop_steps.json").read_text(encoding="utf-8"))
        g = res["X"]["value"]
        (OUT / "G.json").write_text(json.dumps({"G": g, "spread": res["X"]["spread"],
                                                "step": res["X"]["step"], "stop": res["meta"]["stop_reason"],
                                                "gen_cpu_cores": max(s["gen_cpu_cores"] for s in res["steps"])},
                                               indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"G = {g:.0f} req/s")
        return

    n = a.instances
    label = f"n{n}"
    gen_limit = a.gen_limit
    if not gen_limit and (OUT / "G.json").exists():
        gen_limit = json.loads((OUT / "G.json").read_text(encoding="utf-8"))["G"]
    if not gen_limit:
        print("УВАГА: G невідома — спершу запустіть з --noop", file=sys.stderr)
    if not a.no_up:
        env = {**os.environ, "WEB_REPLICAS": str(n)}
        compose("up", "-d", "--build", env=env)
        compose("restart", "lb", env=env)
        wait_health(a.base_url)
    instances = prom.wait_targets(a.prom_url, n)
    print("цілі Prometheus UP:", instances)
    subprocess.run([sys.executable, str(ROOT / "loadgen" / "seed.py"), "--base-url", a.base_url], check=True)

    ps = subprocess.run(["docker", "compose", "ps", "-q", "web"], cwd=ROOT, capture_output=True, text=True)
    ids = ps.stdout.split()
    info = subprocess.run(["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout
    try:
        di = json.loads(info)
        docker_info = {k: di.get(k) for k in ("NCPU", "MemTotal", "OperatingSystem", "ServerVersion")}
    except ValueError:
        docker_info = {}
    (OUT / f"docker_info_{label}.json").write_text(
        json.dumps({"web_containers": len(ids), "targets": instances, **docker_info}, indent=2), encoding="utf-8")

    stop = threading.Event()
    th = threading.Thread(target=sample_stats, args=(stop, OUT / f"docker_stats_{label}.jsonl",
                                                     a.stats_interval), daemon=True)
    if not a.no_stats:
        th.start()
    t_start = time.time()
    try:
        subprocess.run([sys.executable, STEPLOAD, "--base-url", a.base_url, "--instances", str(n),
                        "--gen-limit", str(gen_limit), "--out-prefix", str(OUT / label), *procs], check=True)
    finally:
        stop.set()
        if th.is_alive():
            th.join(timeout=30)
    time.sleep(6)  # дати Prometheus дозняти останній скрейп
    series = prom.collect(a.prom_url, t_start - 5, time.time())
    (OUT / f"prom_{label}.json").write_text(json.dumps(series, ensure_ascii=False), encoding="utf-8")
    print("готово:", OUT / f"{label}_steps.json")


if __name__ == "__main__":
    if not sys.flags.utf8_mode:
        sys.exit(subprocess.run([sys.executable, "-X", "utf8", *sys.argv]).returncode)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
