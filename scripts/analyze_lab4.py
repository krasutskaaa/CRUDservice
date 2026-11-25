#!/usr/bin/env python3
"""Аналіз лабораторної 4: results/lab4/*.json → results/lab4/summary_lab4.md, analysis.json, figs/*.png.
  python scripts/analyze_lab4.py"""
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import RESULTS  # noqa: E402
import usl  # noqa: E402

OUT = RESULTS / "lab4"
FIGS = OUT / "figs"
R = 3
T_POLL = 0.050
F = 0.15


def jl(name):
    p = OUT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def num(x, d=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.{d}f}".replace(",", " ").replace(".", ",")


def ms(x, d=1):
    return num(None if x is None else x * 1000, d)


def pct(x, d=1):
    return num(None if x is None else 100 * x, d) + (" %" if x is not None else "")


def step6_points():
    pts = {}
    for w in (1, 2, R):
        runs = [jl(f"step6_w{w}_r{k}.json") for k in range(1, 4)]
        runs = [r for r in runs if r]
        if runs:
            pts[w] = runs
    return pts


def plot_usl(xs, spread, fit, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ws = sorted(xs)
    ax.errorbar(ws, [xs[w] for w in ws], yerr=[spread[w] for w in ws], fmt="o", capsize=4, color="tab:blue",
                label="виміряно: X(W) ± ст. відхилення посекундної кількості")
    if fit:
        top = max(6, int(fit["N_star"] or 0) + 2) if fit.get("N_star") and fit["N_star"] < 30 else 8
        grid = [1 + i * (top - 1) / 200 for i in range(201)]
        ax.plot(grid, [usl.model(w, fit["lambda"], fit["sigma"], fit["kappa"]) for w in grid], color="tab:orange",
                label=f"закон: σ = {fit['sigma']:.3f}, κ = {fit['kappa']:.4f}")
    ax.set_xlabel("W, воркерів"); ax.set_ylabel("X(W), задач/с"); ax.grid(alpha=.3); ax.legend(fontsize=8)
    ax.set_title("Пропускна здатність пулу воркерів")
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def plot_step9(s9, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t0 = s9["t0"]
    fig, ax = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True)
    for name, color in (("tasks_queued", "tab:blue"), ("tasks_running", "tab:orange")):
        series = s9["prom"].get(name)
        if isinstance(series, list) and series:
            pts = [(float(t) - t0, float(v)) for t, v in series[0]["values"]]
            ax[0].step([a for a, _ in pts], [b for _, b in pts], where="post", color=color, label=name)
    ax[0].set_ylabel("задач"); ax[0].legend(); ax[0].grid(alpha=.3)
    ax[0].set_title(f"Крок 9: FAIL_RATE = {s9['F']}, λ = {num(s9['rate'], 2)} задач/с")
    fin = s9["per_second_finished"]
    ax[1].bar(range(len(fin)), fin, width=1.0, color="tab:green", label="завершено задач за секунду")
    ax[1].axhline(s9["X_R"], ls="--", color="gray", label=f"X(R) без падінь = {num(s9['X_R'], 1)}")
    ax[1].axhline(s9["rate"], ls=":", color="black", label="λ")
    ax[1].set_ylabel("задач/с"); ax[1].set_xlabel("секунда прогону"); ax[1].legend(fontsize=8); ax[1].grid(alpha=.3)
    labels = {"f5_no_heartbeat": f"Ф5: L = {num(s9['L_f5'] * 1000, 0)} мс без heartbeat", "f5_heartbeat": "Ф5: з heartbeat"}
    for k, t in s9["marks"].items():
        for a in ax:
            a.axvline(t - t0, color="red", ls="--")
        ax[0].text(t - t0 + 1, ax[0].get_ylim()[1] * 0.9, labels.get(k, k), color="red", fontsize=8)
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def main():
    FIGS.mkdir(parents=True, exist_ok=True)
    md, A = ["# Лабораторна 4: автоматичний підсумок (варіант 12)", ""], {}

    s1 = jl("step1.json")
    if s1:
        md += ["## Крок 1: ендпоінти", "",
               "| запит | код |", "|---|---|",
               f"| POST /admissions/{{id}}/tasks (запис є) | {s1['post_existing']['status']} |",
               f"| POST /admissions/999999999/tasks | {s1['post_missing']['status']} |",
               f"| GET /tasks/{{id}} | {s1['get_task']['status']} |",
               f"| GET /tasks/999999999 | {s1['get_missing']['status']} |",
               f"| GET /tasks?status=queued&limit=2&offset=0 | {s1['list_queued']['status']} |",
               f"| GET /tasks?status=unknown | {s1['list_bad_status']['status']} |", "",
               "/metrics сервісу web: " + ", ".join(f"`{x}`" for x in s1["metrics"]), ""]

    s2 = jl("step2.json")
    if s2:
        m = s2["mean_s"]
        md += ["## Крок 2: 20 задач на одному воркері", "",
               "| claim | fetch | process (t_cpu) | store | t_io = fetch + store | 1 / (t_io + t_cpu + T_poll/2) |",
               "|---|---|---|---|---|---|",
               f"| {ms(m['claim'], 2)} мс | {ms(m['fetch'], 2)} мс | {ms(m['process'], 2)} мс | {ms(m['store'], 2)} мс | "
               f"{ms(s2['t_io'], 2)} мс | {num(s2['X1_formula'], 2)} задач/с |", "",
               f"Результат воркера збігається з локальним обчисленням за тим самим seed: "
               f"{all(c['equal'] for c in s2['determinism'])} ({len(s2['determinism'])} задачі).", ""]
        A["step2"] = {k: s2[k] for k in ("t_io", "t_cpu", "X1_formula")}

    s3 = jl("step3.json")
    if s3:
        md += ["## Крок 3: наївне захоплення (R воркерів, 300 задач, 100 задач/с)", "",
               "| T_poll | n | задач з >1 результатом | D за task_results | задач з >1 done у журналі | D за журналом | "
               "рядків task_results | захоплень task / empty |", "|---|---|---|---|---|---|---|---|"]
        for k, lab in (("tpoll_50", "50 мс"), ("tpoll_1", "1 мс")):
            x = s3.get(k)
            if x:
                md.append(f"| {lab} | {x['n']} | {x['dup_by_results']} | {pct(x['D_results'], 2)} | {x['dup_by_logs']} | "
                          f"{pct(x['D_logs'], 2)} | {x['results_rows']} | {int(x['claims']['task'])} / {int(x['claims']['empty'])} |")
        md.append("")

    s4 = jl("step4.json")
    if s4:
        md += ["## Крок 4: блокування рядка (T_poll = 1 мс)", "",
               "| захоплення | D | p50 / p99 захоплення | захоплень task / empty | макс. Lock у pg_stat_activity (раз на 1 с) | "
               "макс. granted = false (раз на 1 с) | макс. Lock за зрізами через 20 мс | транзакцій бази/с |",
               "|---|---|---|---|---|---|---|---|"]
        for k, lab in (("skip_locked", "FOR UPDATE SKIP LOCKED"), ("for_update", "FOR UPDATE")):
            x = s4.get(k)
            if x:
                md.append(f"| {lab} | {pct(x['D_results'], 2)} | {ms(x['claim_p50_s'], 2)} / {ms(x['claim_p99_s'], 2)} мс | "
                          f"{int(x['claims']['task'])} / {int(x['claims']['empty'])} | {x['max_lock_waits']} | "
                          f"{x['max_not_granted']} | {x.get('max_lock_waits_20ms', '—')} | {num(x['db_xact_per_s'], 0)} |")
        md.append("")

    s5 = jl("step5.json")
    if s5:
        b, a = s5["task_before"], s5["task_after"]
        md += ["## Крок 5: lease, heartbeat, fencing token", "",
               f"Зупинено `{s5['victim']}` на {num(s5['stopped_s'], 1)} с. Сигнал: "
               + "; ".join(f"`{t['how']}` → `{t['state_after']}`" for t in s5["stop_tries"]), "",
               "| задача | attempts до / після | token до / після | статус | завершив | рядків task_results |",
               "|---|---|---|---|---|---|",
               f"| {b['id']} | {b['attempts']} / {a['attempts']} | {b['token']} / {a['token']} | {a['status']} | "
               f"`{', '.join(s5['finished_by'])}` | {s5['results_rows']} |", "",
               "Події задачі (task_events):", "", "| подія | воркер | token | info |", "|---|---|---|---|"]
        md += [f"| {e['event']} | `{e['worker_id']}` | {e['token']} | `{json.dumps(e['info'], ensure_ascii=False)}` |"
               for e in s5["events"]]
        md += ["", "Рядки журналу зупиненого воркера:", "", "```"] + s5["victim_log_lines"][-4:] + ["```", ""]

    pts = step6_points()
    fit = None
    if pts:
        xs = {w: st.fmean(r["X"] for r in runs) for w, runs in pts.items()}
        spread = {w: st.fmean(r["X_std"] for r in runs) for w, runs in pts.items()}
        md += ["## Крок 6: X(W) з непорожньою чергою", "",
               "| W | задач у черзі | вікно, с | X(W), задач/с | розкид (ст. відх. за секунду) | C(W) | p99 захоплення | "
               "транзакцій бази/с | CPU бази, ядер |", "|---|---|---|---|---|---|---|---|---|"]
        for w, runs in sorted(pts.items()):
            r = runs[-1]
            md.append(f"| {w} | {r['n']} | {num(r['window_s'], 0)} | {num(xs[w], 2)} | {num(spread[w], 2)} | "
                      f"{num(xs[w] / xs[1], 3) if 1 in xs else '—'} | {ms(r.get('claim_p99_s'), 2)} мс | "
                      f"{num(r['db_xact_per_s'], 0)} | {num(r['db_cpu_cores'], 3)} |")
        md.append("")
        if 1 in pts and s2:
            r1 = pts[1][-1]
            m1 = r1.get("means_s") or {}
            md += [f"X(1) = {num(xs[1], 2)} задач/с; 1 / (t_io + t_cpu + T_poll/2) за кроком 2 = "
                   f"{num(s2['X1_formula'], 2)} задач/с (t_cpu = {ms(s2['t_cpu'], 1)} мс). "
                   f"Середні тривалості кроків у прогоні W = 1: claim {ms(m1.get('claim'), 2)}, fetch {ms(m1.get('fetch'), 2)}, "
                   f"process {ms(m1.get('process'), 2)}, store {ms(m1.get('store'), 2)} мс; "
                   f"1 / (сума) = {num(1 / sum(v for v in m1.values() if v), 2) if m1 else '—'} задач/с.", ""]
        if 1 in xs and len(xs) >= 3:
            fit = usl.fit(xs[1], [(w, x) for w, x in xs.items() if w > 1])
            md += [f"Закон масштабованості (N → W): λ = X(1) = {num(fit['lambda'], 2)}, σ = {fit['sigma']:.4f}, "
                   f"κ = {fit['kappa']:.5f}, W* = {num(fit['N_star'], 1) if fit['N_star'] else '—'} ({fit['note']}).", ""]
            A["usl"] = {k: fit[k] for k in ("lambda", "sigma", "kappa", "N_star", "note")}
        A["X"] = xs
        A["X_spread"] = spread
        plot_usl(xs, spread, fit, FIGS / "usl_workers.png")
        md += ["![X(W)](figs/usl_workers.png)", ""]

    s7 = jl("step7.json")
    if s7:
        lo, hi = s7.get("low"), s7.get("high")
        md += ["## Крок 7: закон Літтла", ""]
        if lo:
            md += [f"λ задана = {num(lo['rate'], 2)} задач/с (0,7·X(R)), вікно — останні 120 с із 180.", "",
                   "| λ, задач/с | W_q, мс | λ·W_q | L_q з tasks_queued (Prometheus) | L_q за таблицею tasks | "
                   "розбіжність з tasks_queued | розбіжність з таблицею |", "|---|---|---|---|---|---|---|",
                   f"| {num(lo['lambda'], 3)} | {ms(lo['W_q'], 1)} | {num(lo['lambda_Wq'], 4)} | {num(lo.get('L_q_prom'), 4)} | "
                   f"{num(lo['L_q_db'], 4)} | {num(lo.get('diff_pct_prom'), 1)} % | {num(lo.get('diff_pct_db'), 1)} % |", "",
                   f"W_q: p50 {ms(lo['W_q_p50'], 1)} мс, p99 {ms(lo['W_q_p99'], 1)} мс.", ""]
        if hi:
            md += [f"λ = {num(hi['rate'], 2)} задач/с (1,3·X(R)) протягом 120 с: довжина черги на кінець "
                   f"{hi['queue_at_end_db']} (за таблицею), {num(hi.get('queue_at_end_prom'), 0)} (tasks_queued); "
                   f"очікуваний приріст (λ − X(R))·120 = {num(hi['expected_growth'], 0)}. W_q перших 100 задач "
                   f"{ms(hi['W_q_first100'], 1)} мс, останніх 100 — {num(hi['W_q_last100'], 2)} с.", ""]

    s8 = jl("step8.json")
    if s8:
        codes = {}
        for t in s8["terminated"]:
            codes[str(t["exit_code"])] = codes.get(str(t["exit_code"]), 0) + 1
        md += ["## Крок 8: коректна зупинка", "",
               "| коди завершення контейнерів | повернуто в чергу воркерами | failed | задач з >1 результатом | attempts > 1 | "
               "rollout, с |", "|---|---|---|---|---|---|",
               f"| {', '.join(f'{k}: {v}' for k, v in codes.items()) or '—'} | {s8['returned']} | {s8['failed']} | "
               f"{s8['multi_result']} | {s8['attempts_gt1']} | {num(s8['rollout_s'], 1)} |", ""]

    s9 = jl("step9.json")
    if s9:
        ph = s9["phases"]
        md += ["## Крок 9: FAIL_RATE = 0,15 і сценарій Ф5", "",
               f"λ = {num(s9['rate'], 2)} задач/с, {num(s9['duration'], 0)} с; Ф5: L = t_cpu/2 = {num(s9['L_f5'] * 1000, 1)} мс.", "",
               "Частки — серед задач фази, що на кінець прогону в стані done або failed.", "",
               "| фаза | задач створено | у кінцевому стані | attempts > 1 | частка | failed | частка failed | рядків «0 оновлених рядків» | "
               "повторних захоплень | повернуто за lease | завершено/с |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        names = {"base": "L = 15 с, heartbeat", "f5_no_heartbeat": "Ф5 без heartbeat", "f5_heartbeat": "Ф5 з heartbeat"}
        for k in ("base", "f5_no_heartbeat", "f5_heartbeat"):
            p = ph[k]
            md.append(f"| {names[k]} | {p['created']} | {p['n']} | {p['att_gt1']} | {pct(p['share_att_gt1'], 1)} | {p['failed']} | "
                      f"{pct(p['failed'] / p['n'] if p['n'] else None, 2)} | {p['stale']} | {p['reclaims']} | {p['reaped']} | "
                      f"{num(p['X'], 2)} |")
        rs = sum(s9["restarts_end"].values()) - sum(v for k, v in s9["restarts_before"].items() if k in s9["restarts_end"])
        md += ["", f"Очікувано: частка attempts > 1 ≈ f = {F}, частка failed ≈ f³ = {F ** 3:.5f}, "
                   f"захоплень на задачу 1/(1 − f) = {1 / (1 - F):.3f}.",
               f"Перезапусків контейнерів worker за прогін: {rs} (RESTARTS на кінець: {s9['restarts_end']}). "
               f"Задач з >1 результатом: {s9['multi_result']}. X за прогін {num(s9['X_run'], 2)} задач/с, X(R) без падінь "
               f"{num(s9['X_R'], 2)} задач/с. Стан черги на кінець прогону: {s9['status_end']}." +
               (f" Після FAIL_RATE = 0 черга дообробилась за {num(s9['after_drain']['drain_s'], 0)} с, задач з >1 "
                f"результатом: {s9['after_drain']['multi_result']}." if s9.get('after_drain') else ""), "",
               "Розподіл задач у кінцевому стані за attempts (на кінець прогону):", "", "| attempts | статус | задач |", "|---|---|---|"]
        md += [f"| {r['attempts']} | {r['status']} | {r['c']} |" for r in s9["attempts_distribution"]]
        md.append("")
        if s9.get("f5_runs"):
            md += ["Ф5 окремими прогонами без падінь (FAIL_RATE = 0, тож очікувана від FAIL_RATE частка attempts > 1 "
                   "дорівнює 0), λ = 0,7·X(R):", "",
                   "| прогін | L, мс | задач | attempts > 1 | частка | failed | рядків «0 оновлених рядків» (heartbeat / store) | "
                   "повернуто за lease | задач з >1 результатом |", "|---|---|---|---|---|---|---|---|---|"]
            for k, lab in (("no_heartbeat", "без heartbeat"), ("heartbeat", "з heartbeat")):
                r = s9["f5_runs"].get(k)
                if r:
                    md.append(f"| {lab} | {num(r['lease_s'] * 1000, 1)} | {r['n']} | {r['att_gt1']} | {pct(r['share_att_gt1'], 1)} | "
                              f"{r['failed']} | {r['stale']} ({r['stale_hb']} / {r['stale_store']}) | {r['reaped']} | "
                              f"{r['multi_result']} |")
            md.append("")
        plot_step9(s9, FIGS / "step9_timeline.png")
        md += ["![крок 9](figs/step9_timeline.png)", ""]

    d4 = jl("d4.json")
    if d4:
        md += ["## Крок 10: Д4, захоплення пакетом", "",
               "| b | X(R), задач/с | розкид | p50 / p99 захоплення | захоплень task / empty | повернуто після SIGTERM | "
               "коди завершення | attempts > 1 |", "|---|---|---|---|---|---|---|---|"]
        for b in ("b1", "b10"):
            if b not in d4:
                continue
            x, s = d4[b]["X"], d4[b]["sigterm"]
            codes = {}
            for t in s.get("terminated", []):
                codes[str(t["exit_code"])] = codes.get(str(t["exit_code"]), 0) + 1
            md.append(f"| {b[1:]} | {num(x['X'], 2)} | {num(x['X_std'], 2)} | {ms(x.get('claim_p50_s'), 2)} / "
                      f"{ms(x.get('claim_p99_s'), 2)} мс | {int(x['claims']['task'])} / {int(x['claims']['empty'])} | "
                      f"{s.get('returned')} | {', '.join(f'{k}: {v}' for k, v in codes.items())} | {s.get('attempts_gt1')} |")
        md.append("")

    (OUT / "summary_lab4.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / "analysis.json").write_text(json.dumps(A, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
