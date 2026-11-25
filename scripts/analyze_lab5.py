#!/usr/bin/env python3
"""Аналіз лабораторної 5: results/lab5/*.json → results/lab5/summary_lab5.md, analysis.json, figs/*.png.

  python scripts/analyze_lab5.py           основні прогони
  python scripts/analyze_lab5.py --quick   перевірочні (results/lab5/quick)

У таблиці 2-4, 6, 7 іде медіанний прогін: з трьох прогонів той, у якого сумарне X медіанне; усі показники
рядка беруться з нього, тож X_w + X_r = X і операції потоків у таблиці 4 сходяться з таблицями 2-3.
X_1 і t_cs — медіана трьох окремих вимірів.
"""
import argparse
import json
import math
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = "rw-readers"                       # основна політика RWLock (таблиця 3)
POLICIES = ["rw-readers", "rw-writers", "rw-fair"]
NAMES = {"global": "глобальний замок", "rw-readers": "RWLock, перевага читачам",
         "rw-writers": "RWLock, перевага писцям", "rw-fair": "RWLock, чергування пакетів"}


def num(x, d=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    s = f"{x:,.{d}f}".replace(",", " ").replace(".", ",")
    return s


def load(out, pattern):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob(pattern))]


def med(vals):
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def mrole(runs, role, key):
    return med([r[role][key] for r in runs if r.get(role)])


def ratio(rw, g, d=2):
    if rw is None or g is None:
        return "—"
    if g == 0:
        return "RW − G = " + num(rw - g, d)
    return num(rw / g, d)


def median_run(runs):
    return sorted(runs, key=lambda r: r["X"])[len(runs) // 2]


def role_summary(runs):
    r = median_run(runs)
    out = {}
    for role in ("write", "read", "all"):
        out[role] = {k: r[role][k] for k in ("N", "X", "x", "p50_us", "p99_us", "wait_max_ms", "w", "cv")}
        p50, p99 = out[role]["p50_us"], out[role]["p99_us"]
        out[role]["h"] = p99 / p50 if p50 else None
    out["phi_w"], out["phi_r"], out["X"] = r["phi_w"], r["phi_r"], r["X"]
    out["run"] = runs.index(r) + 1
    out["min_thread"] = min(t["count"] for t in r["threads"])
    return out


def plots(out, runs, summ, threads_tab, n):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib не встановлено: діаграми пропущено")
        return []
    figs = out / "figs"
    figs.mkdir(exist_ok=True)
    made = []
    # 1. X_w і X_r за способами синхронізації
    kinds = ["global"] + POLICIES
    kinds = [k for k in kinds if k in summ]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    xs = range(len(kinds))
    ax.bar([i - 0.2 for i in xs], [summ[k]["write"]["X"] / 1000 for k in kinds], 0.4, label="запис, X_w",
           color="tab:red")
    ax.bar([i + 0.2 for i in xs], [summ[k]["read"]["X"] / 1000 for k in kinds], 0.4, label="читання, X_r",
           color="tab:blue")
    ax.set_xticks(list(xs), [NAMES[k].replace(", ", ",\n") for k in kinds])
    ax.set_ylabel("тис. операцій / с")
    ax.set_title(f"Швидкість запису і читання, n = {n} (медіанний прогін з 3)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(figs / "fig1_throughput.png", dpi=130)
    plt.close(fig)
    made.append("fig1_throughput.png")
    # 2. операції кожного потоку
    fig, ax = plt.subplots(figsize=(8, 4.5))
    idx = [t["idx"] for t in threads_tab]
    ax.bar([i - 0.2 for i in idx], [t["global"] / 1000 for t in threads_tab], 0.4, label=NAMES["global"],
           color="tab:gray")
    ax.bar([i + 0.2 for i in idx], [t[MAIN] / 1000 for t in threads_tab], 0.4, label=NAMES[MAIN],
           color="tab:green")
    ax.set_xticks(idx, [f"{i}\n{'з' if i % 2 else 'ч'}" for i in idx])
    ax.set_xlabel("потік (з — запис, ч — читання)")
    ax.set_ylabel("тис. операцій за D = 10 с")
    ax.set_title("Операції кожного потоку (медіанний прогін з 3)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(figs / "fig2_threads.png", dpi=130)
    plt.close(fig)
    made.append("fig2_threads.png")
    # 3. розподіл тривалості операцій (прогін з медіанним X)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    qs = [q / 100 for q in range(1, 100)] + [0.999, 1.0]
    styles = {"global": "-", MAIN: "--"}
    for kind in ("global", MAIN):
        r = median_run(runs[kind])
        for role, col in (("write", "tab:red"), ("read", "tab:blue")):
            ys = r[role]["tau_q_us"]
            if ys[0] is None:
                continue
            ax.plot(qs[:-1], ys[:-1], styles[kind], color=col,
                    label=f"{'запис' if role == 'write' else 'читання'}, {NAMES[kind]}")
    ax.set_yscale("log")
    ax.set_xlabel("рівень q")
    ax.set_ylabel("τ, мкс (лог. шкала)")
    ax.set_title("Перцентилі тривалості операції p_q")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(figs / "fig3_latency.png", dpi=130)
    plt.close(fig)
    made.append("fig3_latency.png")
    return made


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--quick", action="store_true")
    a = p.parse_args()
    out = ROOT / "results" / "lab5" / ("quick" if a.quick else "")
    runs = {k: load(out, f"run_{k}_r*.json") for k in ["global"] + POLICIES}
    runs = {k: v for k, v in runs.items() if v}
    x1 = {"global": load(out, "x1_global_r*.json"), "rw": load(out, "x1_rw_r*.json")}
    tcs = load(out, "tcs_r*.json")
    ro = load(out, "readonly_*.json")
    g = runs["global"]
    env = g[0]["env"]
    n, n_w, n_r, D = g[0]["n"], g[0]["n_w"], g[0]["n_r"], g[0]["D"]
    summ = {k: role_summary(v) for k, v in runs.items()}
    X1 = {k: med([r["X_1"] for r in v]) for k, v in x1.items()}
    t_w = med([r["t_cs_w_us"] for r in tcs])
    t_r = med([r["t_cs_r_us"] for r in tcs])
    t_bar = (n_w * t_w + n_r * t_r) / n
    bound = 1e6 / t_bar
    timer = med([r["timer_overhead_us"] for r in tcs])
    inv_runs = [r for v in runs.values() for r in v]
    inv_ok = sum(1 for r in inv_runs if r["invariants"]["ok"])
    L = []
    w = L.append
    w("# Підсумок лабораторної 5 (варіант 12): С2, хеш-таблиця кешу за record_id")
    w("")
    w(f"Ключі 0..{g[0]['keys'] - 1}, перед прогоном заповнено {g[0]['keys'] // 2}; n = {n}; D = {num(D, 0)} с, "
      f"прогрів {num(g[0]['warmup'], 0)} с; повторів: {len(g)}. У таблицях 2-4, 6, 7 — медіанний прогін "
      f"(той, у якого X медіанне; номер у таблиці «Окремі прогони»), X_1 і t_cs — медіана трьох вимірів.")
    w(f"Python {env['python']} ({env['implementation']}), GIL: {'увімкнено' if env['gil_enabled'] else 'вимкнено'}, "
      f"sys.getswitchinterval() = {num(env['switchinterval_s'] * 1000, 0)} мс; {env['platform']}; "
      f"логічних ядер: {env['logical_cpus']}.")
    w("")
    w("## Таблиця 1. Стенд")
    w("")
    w("| n | n_w | n_r | Мова і версія | Логічних ядер | X_1, оп/с | Інваріанти після прогонів кроків 2 і 3 |")
    w("|---|---|---|---|---|---|---|")
    w(f"| {n} | {n_w} | {n_r} | Python {env['python']} | {env['logical_cpus']} | "
      f"{num(X1['global'], 0)} (глобальний замок), {num(X1['rw'], 0)} (RWLock) | "
      f"виконано в {inv_ok} з {len(inv_runs)} прогонів |")
    w("")
    inv_rows = []
    for k, v in runs.items():
        for i, r in enumerate(v, 1):
            iv = r["invariants"]
            inv_rows.append(f"| {NAMES[k]} | {i} | {iv['structure']} | {iv['values_in_table_not_written']} | "
                            f"{iv['get_wrong_key']} | {iv['get_sampled']} / {iv['get_sampled_not_written']} | "
                            f"{iv['size']} / {iv['distinct_written']} | {'так' if iv['ok'] else 'НІ'} |")
    w("Перевірка інваріантів у кожному прогоні:")
    w("")
    w("| спосіб | прогін | структура (вузли = size, кошики, повтори ключів) | значень у таблиці, яких ніхто не записував | "
      "get повернув значення іншого ключа | перевірено значень get / з них не записаних | size() / різних записаних ключів | ok |")
    w("|---|---|---|---|---|---|---|---|")
    L += inv_rows
    w("")
    for r in ro:
        w(f"Лише читання (потоків читання: {r['readers']}, {NAMES.get(r['lock'], r['lock'])}, {num(r['D'], 0)} с): "
          f"{num(r['reads'], 0)} операцій get, порівняно {r['fields_compared']} полів таблиці і вузлів до і після, "
          f"збіглися: {'так' if r['unchanged'] else 'НІ'}; неправильних значень: {r['wrong_values']}.")
    w("")

    def role_table(s, with_tcs):
        head = "| Роль | Операцій за D | X, оп/с | p50, мкс | p99, мкс | Найбільший t_wait, мс | w | cv |"
        sep = "|---|---|---|---|---|---|---|---|"
        if with_tcs:
            head += " t_cs, мкс |"
            sep += "---|"
        w(head)
        w(sep)
        for role, nm, tc in (("write", "Запис", t_w), ("read", "Читання", t_r), ("all", "Разом", t_bar)):
            x = s[role]
            row = (f"| {nm} | {num(x['N'], 0)} | {num(x['X'], 0)} | {num(x['p50_us'], 2)} | {num(x['p99_us'], 2)} | "
                   f"{num(x['wait_max_ms'], 1)} | {num(x['w'], 3)} | {num(x['cv'], 3)} |")
            if with_tcs:
                row += f" {num(tc, 3)}{' (t̄_cs)' if role == 'all' else ''} |"
            w(row)
        w("")

    w("## Таблиця 2. Глобальний замок")
    w("")
    role_table(summ["global"], True)
    w(f"t̄_cs = ({n_w}·{num(t_w, 3)} + {n_r}·{num(t_r, 3)}) / {n} = {num(t_bar, 3)} мкс; "
      f"межа 1 / t̄_cs = {num(bound, 0)} оп/с. Накладні двох викликів perf_counter_ns підряд: {num(timer, 3)} мкс "
      f"(входять у t_cs).")
    w("")
    w(f"## Таблиця 3. RWLock ({NAMES[MAIN]})")
    w("")
    role_table(summ[MAIN], False)
    w("## Таблиця 4. Операції потоків за D секунд")
    w("")
    w(f"| Потік | Роль | Глобальний замок | RWLock ({NAMES[MAIN].split(', ')[1]}) |")
    w("|---|---|---|---|")
    threads_tab = []
    for i in range(1, n + 1):
        row = {"idx": i}
        for k in ("global", MAIN):
            row[k] = next(t["count"] for t in median_run(runs[k])["threads"] if t["idx"] == i)
        threads_tab.append(row)
        w(f"| {i} | {'запис' if i % 2 else 'читання'} | {num(row['global'], 0)} | {num(row[MAIN], 0)} |")
    w("")
    sg, sr = summ["global"], summ[MAIN]
    S_g = sg["X"] / X1["global"]
    S_rw = sr["X"] / X1["rw"]
    m5 = {
        "S_G": S_g, "E_G": S_g / n, "S_RW": S_rw, "E_RW": S_rw / n,
        "S_w": sr["write"]["X"] / sg["write"]["X"], "S_r": sr["read"]["X"] / sg["read"]["X"],
        "S_sum": sr["X"] / sg["X"], "rho": sg["X"] * t_bar / 1e6,
    }
    w("## Таблиця 5. Метрики прискорення")
    w("")
    w("| Метрика | Формула | Значення |")
    w("|---|---|---|")
    w(f"| S(n), глобальний замок | X(G) / X_1 = {num(sg['X'], 0)} / {num(X1['global'], 0)} | {num(m5['S_G'], 3)} |")
    w(f"| E(n), глобальний замок | S(n) / n | {num(m5['E_G'], 3)} |")
    w(f"| S(n), RWLock | X(RW) / X_1 = {num(sr['X'], 0)} / {num(X1['rw'], 0)} | {num(m5['S_RW'], 3)} |")
    w(f"| E(n), RWLock | S(n) / n | {num(m5['E_RW'], 3)} |")
    w(f"| S_w | X_w(RW) / X_w(G) | {num(m5['S_w'], 3)} |")
    w(f"| S_r | X_r(RW) / X_r(G) | {num(m5['S_r'], 3)} |")
    w(f"| S_Σ | X(RW) / X(G) | {num(m5['S_sum'], 3)} |")
    w(f"| ρ, завантаженість глобального замка | X(G) · t̄_cs = {num(sg['X'], 0)} · {num(t_bar, 3)} мкс | "
      f"{num(m5['rho'], 3)} |")
    w("")
    w(f"X(G) = {num(sg['X'], 0)} оп/с — це {num(100 * sg['X'] / bound, 1)} % межі 1 / t̄_cs = {num(bound, 0)} оп/с.")
    w("")
    w("## Таблиця 6. Порівняння доступу до даних")
    w("")
    w("| Показник | Глобальний замок: запис | Глобальний замок: читання | RWLock: запис | RWLock: читання | "
      "RW / G: запис | RW / G: читання |")
    w("|---|---|---|---|---|---|---|")
    rows6 = [("X, оп/с", "X", 0), ("x, на один потік ролі, оп/с", "x", 0), ("p50, мкс", "p50_us", 2),
             ("p99, мкс", "p99_us", 2), ("h = p99 / p50", "h", 2), ("Найбільший t_wait, мс", "wait_max_ms", 1),
             ("w", "w", 3), ("cv", "cv", 3)]
    for nm, key, d in rows6:
        gw, gr, rw_, rr = sg["write"][key], sg["read"][key], sr["write"][key], sr["read"][key]
        w(f"| {nm} | {num(gw, d)} | {num(gr, d)} | {num(rw_, d)} | {num(rr, d)} | {ratio(rw_, gw)} | {ratio(rr, gr)} |")
    w(f"| φ, частка ролі в усіх операціях | {num(sg['phi_w'], 3)} | {num(sg['phi_r'], 3)} | {num(sr['phi_w'], 3)} | "
      f"{num(sr['phi_r'], 3)} | {ratio(sr['phi_w'], sg['phi_w'])} | {ratio(sr['phi_r'], sg['phi_r'])} |")
    w("")
    w("## Таблиця 7. Політики RWLock (додатково)")
    w("")
    w("| Спосіб | X_w, оп/с | X_r, оп/с | X, оп/с | S_w | S_r | S_Σ | найбільший t_wait запису, мс | "
      "найбільший t_wait читання, мс | cv запису | cv читання | найменше операцій одного потоку |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k in ["global"] + POLICIES:
        if k not in summ:
            continue
        s = summ[k]
        mins = s["min_thread"]
        w(f"| {NAMES[k]} | {num(s['write']['X'], 0)} | {num(s['read']['X'], 0)} | {num(s['X'], 0)} | "
          f"{ratio(s['write']['X'], sg['write']['X'], 3)} | {ratio(s['read']['X'], sg['read']['X'], 3)} | "
          f"{num(s['X'] / sg['X'], 3)} | {num(s['write']['wait_max_ms'], 1)} | {num(s['read']['wait_max_ms'], 1)} | "
          f"{num(s['write']['cv'], 3)} | {num(s['read']['cv'], 3)} | {num(mins, 0)} |")
    w("")
    w("## Окремі прогони")
    w("")
    w("| спосіб | прогін | початок | X_w | X_r | X | найбільший t_wait запису, мс |")
    w("|---|---|---|---|---|---|---|")
    for k, v in runs.items():
        for i, r in enumerate(v, 1):
            mark = " (медіанний)" if i == summ[k]["run"] else ""
            w(f"| {NAMES[k]} | {i}{mark} | {r['started']} | {num(r['write']['X'], 0)} | {num(r['read']['X'], 0)} | "
              f"{num(r['X'], 0)} | {num(r['write']['wait_max_ms'], 1)} |")
    for k, v in x1.items():
        w(f"| X_1, {'глобальний замок' if k == 'global' else 'RWLock'} | " + ", ".join(num(r["X_1"], 0) for r in v)
          + " | | | | | |")
    w("| t_cs,w / t_cs,r, мкс | " + ", ".join(f"{num(r['t_cs_w_us'], 3)} / {num(r['t_cs_r_us'], 3)}" for r in tcs)
      + " | | | | | |")
    w("")
    figs = plots(out, runs, summ, threads_tab, n)
    for f in figs:
        w(f"![{f}](figs/{f})")
    (out / "summary_lab5.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / "analysis.json").write_text(json.dumps({
        "env": env, "X_1": X1, "t_cs_w_us": t_w, "t_cs_r_us": t_r, "t_bar_us": t_bar, "bound": bound,
        "summary": {k: {kk: vv for kk, vv in s.items()} for k, s in summ.items()}, "table5": m5,
        "threads": threads_tab}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
