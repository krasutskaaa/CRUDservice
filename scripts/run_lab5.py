#!/usr/bin/env python3
"""Прогони лабораторної 5: кожен вимір в окремому процесі Python, результати в results/lab5/*.json.

  python scripts/run_lab5.py            усі кроки (~8 хв)
  python scripts/run_lab5.py --quick    перевірка стенду: D = 2 с, 1 повтор (у results/lab5/quick/)

Порядок: t_cs (крок 2), X_1 для обох замків (крок 1), далі 3 повтори; у кожному повторі по черзі
глобальний замок і RWLock (основна політика readers, додатково writers і fair), щоб дрейф машини
однаково впливав на обидва способи. Наприкінці перевірка «читання не змінює полів» (крок 3).
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "rwcache" / "bench.py"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--quick", action="store_true")
    p.add_argument("--repeats", type=int, default=3)
    a = p.parse_args()
    out = ROOT / "results" / "lab5" / ("quick" if a.quick else "")
    out.mkdir(parents=True, exist_ok=True)
    D = "2" if a.quick else "10"
    reps = 1 if a.quick else a.repeats
    log = open(out / "log_run.txt", "a", encoding="utf-8")

    def bench(name, *args):
        cmd = [sys.executable, str(BENCH), "--duration", D, "--out", str(out / f"{name}.json"), *args]
        t = time.time()
        line = f"[{time.strftime('%H:%M:%S')}] {name}: {' '.join(args)}"
        print(line, flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        log.write(line + "\n" + r.stdout + r.stderr + f"код {r.returncode}, {time.time() - t:.1f} с\n\n")
        log.flush()
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-2000:])
            print(f"  !!! {name}: код {r.returncode} (інваріанти або помилка), див. log_run.txt")
        else:
            j = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
            if j["mode"] == "run":
                print(f"  X_w = {j['write']['X']:.0f}, X_r = {j['read']['X']:.0f}, X = {j['X']:.0f} оп/с, "
                      f"інваріанти: {'ok' if j['invariants']['ok'] else 'ПОРУШЕНО'}")
            elif j["mode"] == "x1":
                print(f"  X_1 = {j['X_1']:.0f} оп/с")
            elif j["mode"] == "tcs":
                print(f"  t_cs,w = {j['t_cs_w_us']:.3f} мкс, t_cs,r = {j['t_cs_r_us']:.3f} мкс")
            elif j["mode"] == "readonly":
                print(f"  читань {j['reads']}, поля таблиці не змінились: {j['unchanged']}")

    t_all = time.time()
    for k in range(1, reps + 1):
        bench(f"tcs_r{k}", "--mode", "tcs")
    for k in range(1, reps + 1):
        bench(f"x1_global_r{k}", "--mode", "x1", "--lock", "global")
        bench(f"x1_rw_r{k}", "--mode", "x1", "--lock", "rw", "--policy", "readers")
    for k in range(1, reps + 1):
        bench(f"run_global_r{k}", "--lock", "global")
        bench(f"run_rw-readers_r{k}", "--lock", "rw", "--policy", "readers")
        bench(f"run_rw-writers_r{k}", "--lock", "rw", "--policy", "writers")
        bench(f"run_rw-fair_r{k}", "--lock", "rw", "--policy", "fair")
    bench("readonly_rw-readers", "--mode", "readonly", "--lock", "rw", "--policy", "readers")
    bench("readonly_global", "--mode", "readonly", "--lock", "global")
    print(f"готово за {(time.time() - t_all) / 60:.1f} хв; далі: python scripts/analyze_lab5.py"
          + (" --quick" if a.quick else ""))


if __name__ == "__main__":
    main()
