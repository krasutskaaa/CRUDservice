#!/usr/bin/env python3
"""Перевірка коректного завершення: 2 інстанси, 300 req/s, docker stop одного під навантаженням.

Критерії: код виходу зупиненого контейнера 0; у вікні ±10 с навколо зупинки жодної відповіді не 2xx
і жодної помилки зʼєднання. Результат: results/lab2/graceful.json
"""
import json
import os
import subprocess
import sys
import time

from common import ROOT, RESULTS, compose, wait_health
import prom

OUT = RESULTS / "lab2"


def main():
    base, pu = "http://127.0.0.1:8080", f"http://127.0.0.1:{os.environ.get('PROM_PORT', '9090')}"
    OUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WEB_REPLICAS": "2"}
    compose("up", "-d", "--build", env=env)
    compose("restart", "lb", env=env)
    wait_health(base)
    prom.wait_targets(pu, 2)
    subprocess.run([sys.executable, str(ROOT / "loadgen" / "seed.py"), "--base-url", base], check=True)
    ids = subprocess.run(["docker", "compose", "ps", "-q", "web"], cwd=ROOT, capture_output=True,
                         text=True).stdout.split()
    victim = ids[0]
    prefix = OUT / "graceful"
    gen = subprocess.Popen([sys.executable, str(ROOT / "loadgen" / "stepload.py"), "--base-url", base,
                            "--warmup", "5", "--warmup-rate", "300", "--start-rate", "300", "--step", "0",
                            "--step-duration", "60", "--max-steps", "1", "--transient", "0", "--window", "50",
                            "--out-prefix", str(prefix)], stdout=subprocess.PIPE, text=True, encoding="utf-8")
    t0 = None
    for line in gen.stdout:
        if line.startswith("T0 "):
            t0 = float(line.split()[1])
            break
    time.sleep(max(0, t0 + 25 - time.time()))
    ts = time.time()
    subprocess.run(["docker", "stop", victim], check=True, capture_output=True)
    te = time.time()
    code = int(subprocess.run(["docker", "inspect", "-f", "{{.State.ExitCode}}", victim],
                              capture_output=True, text=True).stdout.strip())
    gen.communicate()
    import csv
    rows = list(csv.DictReader(open(str(prefix) + "_seconds.csv", encoding="utf-8")))
    s0 = int(ts - t0)
    win = [r for r in rows if s0 - 10 <= int(r["second"]) <= s0 + 10]
    bad = sum(int(r["non2xx"]) for r in win)
    conn = sum(int(r["conn_errors"]) for r in win)
    ok = sum(int(r["n2xx"]) for r in win)
    res = {"exit_code": code, "stop_duration_s": te - ts, "stop_at_second": s0, "window_ok": ok,
           "window_non2xx": bad, "window_conn_errors": conn,
           "passed": code == 0 and bad == 0 and conn == 0}
    (OUT / "graceful.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))
    compose("up", "-d", env=env)  # повернути другий інстанс


if __name__ == "__main__":
    if not sys.flags.utf8_mode:
        sys.exit(subprocess.run([sys.executable, "-X", "utf8", *sys.argv]).returncode)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
