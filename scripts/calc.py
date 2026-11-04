#!/usr/bin/env python3
"""Таблиця шести конфігурацій і три розрахунки за сирими результатами.

  python scripts/calc.py            # друк + results/summary.md
"""
import glob
import json
import re
import statistics as st

from common import RAW, RESULTS


def load(pattern):
    out = {}
    for f in sorted(glob.glob(str(RAW / pattern))):
        m = re.search(r"_n(\d+)_r(\d+)_run(\d+)", f)
        d = json.load(open(f))
        out.setdefault((int(m[1]), int(m[2])), []).append(d["summary"])
    return out


def med(runs, key):
    return st.median(r[key] for r in runs)


def main():
    op, cl = load("open_n*_r*_run*.json"), load("closed_n*_r*_run*.json")
    lines = ["## Таблиця вимірювань (медіана з прогонів)", "",
             "| Інстансів | Задана, req/s | Досягнута, req/s | p50, мс | p95, мс | p99, мс | Не-2xx, % | Прогонів |",
             "|---|---|---|---|---|---|---|---|"]
    for (n, r), runs in sorted(op.items()):
        lines.append(f"| {n} | {r} | {med(runs, 'achieved_rps'):.1f} | {med(runs, 'p50_ms'):.1f} | "
                     f"{med(runs, 'p95_ms'):.1f} | {med(runs, 'p99_ms'):.1f} | "
                     f"{100 * med(runs, 'non2xx_share'):.2f} | {len(runs)} |")

    # --- 1. Прискорення і Карп-Флатт ---
    lines += ["", "## Розрахунок 1. Прискорення і метрика Карпа-Флатта", "",
              "S = X(2) / X(1); e = (1/S − 1/N) / (1 − 1/N), N = 2 → e = (1/S − 0.5) / 0.5", "",
              "| Рівень, req/s | X(1) | X(2) | S | e |", "|---|---|---|---|---|"]
    for r in sorted({k[1] for k in op}):
        if (1, r) in op and (2, r) in op:
            x1, x2 = med(op[(1, r)], "achieved_2xx_rps"), med(op[(2, r)], "achieved_2xx_rps")
            s = x2 / x1
            e = (1 / s - 0.5) / 0.5
            lines.append(f"| {r} | {x1:.1f} | {x2:.1f} | {s:.3f} | {e:.3f} |")

    # --- рівень, де 1 інстанс не тримає навантаження ---
    sat = next((r for (n, r) in sorted(op) if n == 1 and med(op[(n, r)], "achieved_rps") < 0.95 * r), None)
    lines += ["", f"Перший рівень, де один інстанс не тримає задану інтенсивність (досягнута < 95% заданої): "
                  f"**{sat if sat else 'не виявлено'}**"]


    # --- 2. Літтл ---
    lines += ["", "## Розрахунок 2. Закон Літтла (1 інстанс, 100 req/s)", "",
              "L = λ·W; розбіжність = |L_вим − λ·W| / L_вим · 100%", "",
              "| Прогін | λ, req/s | W, с | λ·W | L виміряне | Розбіжність, % |", "|---|---|---|---|---|---|"]
    for i, s in enumerate(op.get((1, 100), []), 1):
        lt = s["little"]
        div = abs(lt["L_measured"] - lt["lambda_times_W"]) / lt["L_measured"] * 100
        lines.append(f"| {i} | {lt['lambda_rps']:.2f} | {lt['W_s']:.5f} | {lt['lambda_times_W']:.4f} | "
                     f"{lt['L_measured']:.4f} | {div:.2f} |")

    # --- 3. Закритий контур ---
    lines += ["", "## Розрахунок 3. Відкритий і закритий контур", ""]
    if sat:
        conc = round(st.median(s["little"]["L_measured"] for s in op[(1, sat)]))
        lines.append(f"Рівень {sat} req/s; середня кількість запитів у системі у відкритому контурі "
                     f"L ≈ {conc} → concurrency для закритого контуру = {max(conc, 1)}. "
                     f"Запуск: `python scripts/run_closed.py --instances 1 --level {sat}`")
        lines += ["", "| Контур | Досягнута, req/s | p95, мс |", "|---|---|---|",
                  f"| Відкритий ({sat} req/s) | {med(op[(1, sat)], 'achieved_rps'):.1f} | {med(op[(1, sat)], 'p95_ms'):.1f} |"]
        if (1, sat) in cl:
            lines.append(f"| Закритий (concurrency {max(conc, 1)}) | {med(cl[(1, sat)], 'achieved_rps'):.1f} | "
                         f"{med(cl[(1, sat)], 'p95_ms'):.1f} |")
        else:
            lines.append("| Закритий | ⟨ще не знято⟩ | ⟨ще не знято⟩ |")
    else:
        lines.append("Рівня насичення на одному інстансі не виявлено: порівняння робиться на найвищому рівні.")

    text = "\n".join(lines) + "\n"
    (RESULTS / "summary.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()