#!/usr/bin/env python3
"""Серія відкритого контуру для заданої кількості вебінстансів.

  python scripts/run_series.py --instances 1
  python scripts/run_series.py --instances 2

Піднімає стенд із WEB_REPLICAS=N, перезапускає балансувальник (щоб nginx побачив
усі репліки), заносить 100 записів і для кожного рівня 10/100/500 робить
3 прогони (прогрів 10 с + 30 с). Сирі результати: results/raw/open_n{N}_r{R}_run{K}.json
"""
import argparse
import os
import platform
import subprocess
import sys
import time

from common import RAW, RESULTS, ROOT, compose, wait_health

LG = str(ROOT / "loadgen" / "loadgen.py")


def run_lg(*extra):
    subprocess.run([sys.executable, LG, *extra], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", type=int, required=True)
    ap.add_argument("--rates", type=int, nargs="+", default=[10, 100, 500])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--selfcheck", action="store_true",
                    help="лише перевірити генератор проти /noop (без бази)")
    ap.add_argument("--no-up", action="store_true", help="не чіпати стенд")
    a = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    if not a.no_up:
        env = {**os.environ, "WEB_REPLICAS": str(a.instances)}
        compose("up", "-d", "--build", env=env)
        compose("restart", "lb", env=env)
        wait_health(a.base_url)
        n = len(subprocess.run(["docker", "compose", "ps", "-q", "web"], cwd=ROOT,
                               capture_output=True, text=True).stdout.split())
        assert n == a.instances, f"запущено {n} вебінстансів, очікувалось {a.instances}"
    (RESULTS / "machine.txt").write_text(
        f"{platform.platform()}\ncpu_count={os.cpu_count()}\npython={platform.python_version()}\n"
        "Генератор і стенд працюють на одній машині.\n")

    if a.selfcheck:
        for r in a.rates:
            run_lg("--base-url", a.base_url, "--path", "/noop", "--rate", str(r),
                   "--duration", str(a.duration), "--warmup", str(a.warmup),
                   "--out", str(RAW / f"selfcheck_noop_r{r}.json"))
        return

    subprocess.run([sys.executable, str(ROOT / "loadgen" / "seed.py"), "--base-url", a.base_url], check=True)
    for r in a.rates:
        for k in range(1, a.repeats + 1):
            run_lg("--base-url", a.base_url, "--rate", str(r), "--duration", str(a.duration),
                   "--warmup", str(a.warmup), "--instances", str(a.instances),
                   "--out", str(RAW / f"open_n{a.instances}_r{r}_run{k}.json"))
            time.sleep(5)  # пауза, щоб черги встигли розсмоктатись


if __name__ == "__main__":
    main()
