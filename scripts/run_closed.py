#!/usr/bin/env python3
"""Закритий контур на рівні, де сервіс не тримає інтенсивність.
concurrency = округлене середнє L, виміряне у відкритому контурі на цьому рівні.

  python scripts/run_closed.py --instances 1 --level 500
Результат: results/raw/closed_n{N}_r{R}_run{K}.json
"""
import argparse
import glob
import json
import re
import statistics as st
import subprocess
import sys
import time

from common import RAW, ROOT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", type=int, default=1)
    ap.add_argument("--level", type=int, required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    a = ap.parse_args()
    ls = [json.load(open(f))["summary"]["little"]["L_measured"]
          for f in glob.glob(str(RAW / f"open_n{a.instances}_r{a.level}_run*.json"))]
    assert ls, "немає відкритих прогонів для цього рівня"
    conc = max(1, round(st.median(ls)))
    print(f"concurrency = {conc}")
    for k in range(1, a.repeats + 1):
        subprocess.run([sys.executable, str(ROOT / "loadgen" / "loadgen.py"), "--base-url", a.base_url,
                        "--mode", "closed", "--concurrency", str(conc), "--duration", "30", "--warmup", "10",
                        "--instances", str(a.instances),
                        "--out", str(RAW / f"closed_n{a.instances}_r{a.level}_run{k}.json")], check=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
