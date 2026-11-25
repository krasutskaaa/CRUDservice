#!/usr/bin/env python3
"""Аналіз результатів лабораторної 3 → results/lab3/summary_lab3.md, results/lab3/analysis.json і figs/."""
import csv
import json
import math
import os
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hist  # noqa: E402
import usl  # noqa: E402
from common import RESULTS  # noqa: E402

OUT = RESULTS / "lab3"
LAB2 = RESULTS / "lab2"
FIGS = OUT / "figs"
R = 3
REQ_CPU = 0.25            # requests.cpu, ядер
BOUNDS = [0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128, 0.256, 0.512, 1.024]
NA = "не досягнуто"


def jl(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def num(x, d=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "н/д"
    return f"{x:.{d}f}".replace(".", ",")


def seconds(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for r in rows:
        for k in r:
            if k != "phase":
                r[k] = float(r[k])
    return rows


# ---------- допоміжне: Prometheus ----------
def by_pod(res, key="pod"):
    return {x["metric"].get(key, "?"): float(x["value"][1]) for x in res.get("result", [])}


def server_p99(bucket_result, pod=None):
    """p99 сервера з приростів бакетів за вікно (сума по подах або один под)."""
    acc = {}
    for x in bucket_result.get("result", []):
        if pod and x["metric"].get("pod") != pod:
            continue
        le = x["metric"]["le"]
        acc[le] = acc.get(le, 0.0) + float(x["value"][1])
    if not acc:
        return None
    cum = [acc.get(f"{b:g}", acc.get(str(b), 0.0)) for b in BOUNDS]
    return hist.quantile(0.99, BOUNDS, cum, acc.get("+Inf", cum[-1]))


def fmt_q(q):
    if q is None:
        return "н/д"
    if q["beyond"]:
        return "> 1024 мс"
    return f"{num(q['value'] * 1000)} мс"


def raw_delta(pts, lo, hi):
    """Прирости лічильника між першим і останнім зразком у [lo, hi]; вікно розширюється,
    якщо в ньому менше двох зразків (cAdvisor оновлює лічильники раз на 10-15 с)."""
    for pad in (0, 5, 10, 15):
        sel = [(t, v) for t, v in pts if lo - pad <= t <= hi + pad]
        if len(sel) >= 2 and sel[-1][0] > sel[0][0]:
            return sel[-1][1] - sel[0][1], sel[-1][0] - sel[0][0]
    return None, None


def throttling(raw, lo, hi):
    out = {}
    for pod, per in raw.get("cfs_periods", {}).items():
        dp, _ = raw_delta(per, lo, hi)
        dt, _ = raw_delta(raw.get("cfs_throttled", {}).get(pod, []), lo, hi)
        if dp:
            out[pod] = dt / dp
    return out


def cpu_series(raw):
    """{pod: [(t_mid, ядер)]} з послідовних зразків container_cpu_usage_seconds_total."""
    out = {}
    for pod, pts in raw.get("cpu_usage", {}).items():
        out[pod] = [((a + b) / 2, (vb - va) / (b - a)) for (a, va), (b, vb) in zip(pts, pts[1:]) if b > a]
    return out


def cpu_at(pts, t, max_age=20):
    """Останнє значення CPU пода до моменту t, якщо воно не старше max_age с (под ще існує)."""
    v = None
    for tm, x in pts:
        if tm <= t:
            v = (tm, x)
        else:
            break
    return v[1] if v and t - v[0] < max_age else None


def range_series(prom, name, key="pod"):
    out = {}
    for s in prom.get(name, {}).get("result", []):
        k = s["metric"].get(key) or "all"
        out[k] = [(float(t), float(v)) for t, v in s["values"] if v != "NaN"]
    return out


def held(pts, t, default=None):
    """Значення ряду в момент t: останнє відоме до t (між точками збору сталe)."""
    v = default
    for tt, vv in pts:
        if tt <= t:
            v = vv
        else:
            break
    return v


# ---------- кроки 7-8 ----------
def load_runs():
    runs = {}
    for n in range(1, R + 1):
        for k in range(1, 4):
            lab = f"n{n}_r{k}"
            if (OUT / f"{lab}_steps.json").exists():
                runs.setdefault(n, []).append({
                    "label": lab, "res": jl(OUT / f"{lab}_steps.json"),
                    "pw": jl(OUT / f"prom_steps_{lab}.json") if (OUT / f"prom_steps_{lab}.json").exists() else {},
                    "raw": jl(OUT / f"raw_{lab}.json") if (OUT / f"raw_{lab}.json").exists() else {},
                    "pods": jl(OUT / f"pods_{lab}.json") if (OUT / f"pods_{lab}.json").exists() else {}})
    return runs


def run_point(run):
    res = run["res"]
    x = res["X"]
    s = next(s for s in res["steps"] if s["step"] == x["step"])
    pw = run["pw"].get(str(x["step"]), {})
    rps = by_pod(pw.get("rps_by_pod", {}))
    thr = throttling(run["raw"], *s["window_epoch"])
    q_all = server_p99(pw.get("bucket_by_pod", {})) if pw else None
    imb = None
    hi_pod = lo_pod = None
    if len(rps) > 1:
        avg = st.fmean(rps.values())
        imb = max(rps.values()) / avg if avg else None
        hi_pod = max(rps, key=rps.get)
        lo_pod = min(rps, key=rps.get)
    pods = {}
    for p in rps:
        pods[p] = {"rps": rps[p], "server_p99": server_p99(pw.get("bucket_by_pod", {}), p),
                   "throttling": thr.get(p)}
    nodes = {x_["pod"]: x_["node"] for x_ in run["pods"].get("pods", [])}
    return {"label": run["label"], "X": x["value"], "spread": x["spread"], "step": x["step"], "target": x["target"],
            "reached": res["last_step_ratio"] < 0.9, "client_p99": s["client_p99_ms"], "non2xx": s["non2xx_share"],
            "server_p99": q_all, "thr_mean": st.fmean(thr.values()) if thr else None,
            "thr_max": max(thr.values()) if thr else None, "imbalance": imb, "hi_pod": hi_pod, "lo_pod": lo_pod,
            "pods": pods, "nodes": nodes, "window": s["window_epoch"]}


# ---------- крок 12 ----------
def need(z, xs, fitres):
    for n in range(1, R + 1):
        xn = xs.get(n)
        if xn is None and fitres:
            xn = usl.model(n, fitres["lambda"], fitres["sigma"], fitres["kappa"])
        if xn is not None and xn >= z:
            return n
    return R


def elastic(mode, xs, fitres):
    sec = seconds(OUT / f"profile_{mode}_seconds.csv")
    res = jl(OUT / f"profile_{mode}_steps.json")
    meta = jl(OUT / f"profile_{mode}_meta.json")
    prom = jl(OUT / f"prom_profile_{mode}.json")
    raw = jl(OUT / f"raw_profile_{mode}.json") if (OUT / f"raw_profile_{mode}.json").exists() else {}
    t0 = res["meta"]["t0"]
    segs = [(s["window_seconds"][0], s["window_seconds"][1]) for s in res["steps"]]
    T = int(segs[-1][1])
    sec = [r for r in sec if r["second"] < T]
    ready = range_series(prom, "ready").get("all", [])
    desired = range_series(prom, "desired").get("all", [])
    cpu = cpu_series(raw)
    rows = []
    for r in sec:
        t = int(r["second"])
        tt = t0 + t + 0.5
        s_t = held(ready, tt, 0)
        d_t = need(r["target"], xs, fitres)
        cores = [v for v in (cpu_at(pts, tt) for pts in cpu.values()) if v is not None]
        rows.append({"t": t, "z": r["target"], "achieved": r["achieved"], "p99": r["p99_ms"],
                     "bad": r["non2xx"] + r["conn_errors"], "non2xx": r["non2xx"], "conn": r["conn_errors"],
                     "s": s_t, "d": d_t, "desired": held(desired, tt),
                     "cpu_rel": (st.fmean(cores) / REQ_CPU * 100) if cores else None})
    return {"mode": mode, "rows": rows, "segs": segs, "T": T, "steps": res["steps"], "meta": meta,
            "cpu": {p: [(t - t0, v / REQ_CPU * 100) for t, v in pts] for p, pts in cpu.items()}}


def metrics(e, p99_0):
    rows, T = e["rows"], e["T"]
    return {"theta_U": sum(r["s"] < r["d"] for r in rows) / T, "theta_O": sum(r["s"] > r["d"] for r in rows) / T,
            "a_U": sum(max(r["d"] - r["s"], 0) for r in rows) / T, "a_O": sum(max(r["s"] - r["d"], 0) for r in rows) / T,
            "W": sum(r["s"] for r in rows), "T": T,
            "viol_s": sum(1 for r in rows if not math.isnan(r["p99"]) and r["p99"] > 2 * p99_0),
            "non2xx": int(sum(r["non2xx"] for r in rows)), "conn_errors": int(sum(r["conn"] for r in rows))}


def hpa_events(e, p99_ref):
    rows = e["rows"]
    (a0, a1), (b0, b1), (c0, c1) = e["segs"][:3]
    by_t = {r["t"]: r for r in rows}
    d0 = by_t[b0 - 1]["desired"]  # бажана кількість перед стрибком
    ev = {}
    ev["desired_change"] = next((r["t"] - b0 for r in rows if b0 <= r["t"] < b1 and r["desired"] != d0), None)
    mx = max(r["s"] for r in rows)
    ev["max_ready"] = mx
    ev["max_ready_at"] = next((r["t"] - b0 for r in rows if b0 <= r["t"] < b1 and r["s"] >= mx), None)
    ev["p99_recover"] = None
    for r in rows:
        t = r["t"]
        if b0 <= t and t + 9 < b1 and all(by_t[t + i]["p99"] < 1.2 * p99_ref for i in range(10)):
            ev["p99_recover"] = t - b0
            break
    s_end = held([(r["t"], r["s"]) for r in rows], b1 - 1)
    ev["scale_down"] = next((r["t"] - b1 for r in rows if c0 <= r["t"] < c1 and r["s"] < s_end), None)
    return ev


def main():
    import plots_lab3 as pl  # matplotlib лише тут
    FIGS.mkdir(parents=True, exist_ok=True)
    md = ["# Підсумок лабораторної 3 (автоматично згенеровано)\n"]
    A = {}

    if (OUT / "db_persist.json").exists():
        d = jl(OUT / "db_persist.json")
        md.append("## Крок 3. Збереження даних бази\n")
        md.append(f"Под db-0 видалено і створено заново (uid змінився: {'так' if d['pod_recreated'] else 'ні'}), "
                  f"PVC `{d['pvc']}` → PV `{d['pv']}`. Запис id={d['record']['id']} після перезапуску: код {d['read_status']}, "
                  f"збігається з початковим: **{'так' if d['same_record'] else 'ні'}**. /healthz знову 200 через {num(d['web_healthz_ok_after_s'])} с.\n")
    if (OUT / "crud.json").exists():
        d = jl(OUT / "crud.json")
        md.append(f"## Крок 4. CRUD через localhost:8080 ({d['replicas']} репліки)\n")
        md.append("| крок | запит | код | X-Pod | вузол |\n|---|---|---|---|---|")
        nodes = {p["pod"]: p["node"] for p in d["pods"]}
        for x in d["log"]:
            md.append(f"| {x['title']} | `{x['method']} {x['path']}` | {x['status']} | `{x['x_pod']}` | {nodes.get(x['x_pod'], '?')} |")
        pw = {k: "так" if v else "ні" for k, v in d["password"].items()}
        md.append(f"\nПароль знайдено в ConfigMap: {pw['in_configmaps']}; в образі (inspect/history): {pw['in_image_inspect']}/"
                  f"{pw['in_image_history']}; у файлах репозиторію: {pw['in_git_tracked_files']}; у Deployment: "
                  f"{pw['in_deployment_spec']}. Deployment бере пароль через secretKeyRef: {pw['deployment_uses_secretKeyRef']}.\n")
    if (OUT / "monitoring_check.json").exists():
        d = jl(OUT / "monitoring_check.json")
        md.append("## Крок 5. Моніторинг\n")
        md.append("| ряд | кількість рядів | значення |\n|---|---|---|")
        for name, v in d["queries"].items():
            vals = ", ".join(f"{m.get('pod') or m.get('state') or m.get('deployment')}={num(float(x), 0)}" for m, x in v["result"])
            md.append(f"| `{name}` | {len(v['result'])} | {vals} |")
        if "clock_skew_s" in d:
            md.append(f"\nРізниця годинників Prometheus і генератора: {num(d['clock_skew_s'], 2)} с.")
        md.append("\n```\n" + d["kubectl_top_pods"].strip() + "\n```\n")

    # ---- крок 7 ----
    runs = load_runs()
    pts = {n: [run_point(r) for r in rs] for n, rs in runs.items()}
    xs, spread, reached = {}, {}, {}
    md.append("## Крок 7. X(N) без автомасштабування\n")
    md.append("| N | прогін | X(N), req/s | розкид у вікні | сходинка (задана) | точка досягнута | p99 клієнта, мс | p99 сервера | дроселювання: середнє / найбільше | не 2xx |\n|---|---|---|---|---|---|---|---|---|---|")
    for n, ps in sorted(pts.items()):
        for p in ps:
            md.append(f"| {n} | {p['label']} | {num(p['X'])} | {num(p['spread'])} | {p['step']} ({num(p['target'], 0)}) | "
                      f"{'так' if p['reached'] else 'ні'} | {num(p['client_p99'])} | {fmt_q(p['server_p99'])} | "
                      f"{num(p['thr_mean'] * 100 if p['thr_mean'] is not None else None)} % / "
                      f"{num(p['thr_max'] * 100 if p['thr_max'] is not None else None)} % | {num(p['non2xx'] * 100, 2)} % |")
        ok = [p for p in ps if p["reached"]]
        if ok:
            xs[n] = st.fmean(p["X"] for p in ok)
            spread[n] = st.stdev([p["X"] for p in ok]) if len(ok) > 1 else ok[0]["spread"]
            reached[n] = True
    md.append("\n| N | X(N), req/s | розкид, req/s | прогонів |\n|---|---|---|---|")
    for n in sorted(xs):
        k = len([p for p in pts[n] if p["reached"]])
        md.append(f"| {n} | {num(xs[n])} | {num(spread[n])} ({'між прогонами' if k > 1 else 'у вікні сходинки'}) | {k} |")
    A["X"] = xs
    A["spread"] = spread

    # ---- крок 8 ----
    md.append("\n## Крок 8. Коефіцієнт дисбалансу I = X_max / X_avg\n")
    md.append("| N | прогін | req/s за подами | I | найзавантаженіший: p99 сервера / дроселювання | найменш завантажений: p99 сервера / дроселювання |\n|---|---|---|---|---|---|")
    for n, ps in sorted(pts.items()):
        if n < 2:
            continue
        for p in ps:
            if p["imbalance"] is None:
                continue
            per = ", ".join(f"{k[-5:]}@{p['nodes'].get(k, '?').replace('k3d-lab3-', '')}={num(v['rps'], 0)}" for k, v in p["pods"].items())
            hi, lo = p["pods"][p["hi_pod"]], p["pods"][p["lo_pod"]]
            md.append(f"| {n} | {p['label']} | {per} | {num(p['imbalance'], 3)} | {fmt_q(hi['server_p99'])} / "
                      f"{num(hi['throttling'] * 100 if hi['throttling'] is not None else None)} % | {fmt_q(lo['server_p99'])} / "
                      f"{num(lo['throttling'] * 100 if lo['throttling'] is not None else None)} % |")
    if R in runs:
        lab = runs[R][0]["label"]
        pl.pods_rps(jl(OUT / f"{lab}_steps.json"), jl(OUT / f"prom_{lab}.json"), FIGS / "pods_rps_R.png", f"N = {R} ({lab})")
    for n, rs in runs.items():
        for r in rs:
            pl.timeline(r["label"], OUT / f"{r['label']}_steps.json", OUT / f"{r['label']}_seconds.csv",
                        FIGS / f"timeline_{r['label']}.png")

    # ---- крок 9 ----
    md.append("\n## Крок 9. Універсальний закон масштабованості\n")
    fit_k8s = fit_cmp = None
    if 1 in xs and len([n for n in xs if n > 1]) >= 2:
        fit_k8s = usl.fit(xs[1], [(n, x) for n, x in xs.items() if n > 1])
        md.append(f"Kubernetes: λ = {num(fit_k8s['lambda'])}; σ = {fit_k8s['sigma']:.4f}; κ = {fit_k8s['kappa']:.5f}; "
                  f"N* = {num(fit_k8s['N_star'], 2) if fit_k8s['N_star'] else 'н/д'}. {fit_k8s['note']}.\n")
        md.append("| N | X(N) | C(N) | y = N/C − 1 | X за законом |\n|---|---|---|---|---|")
        for r in fit_k8s["rows"]:
            md.append(f"| {r['N']} | {num(r['X'])} | {r['C']:.3f} | {r['y']:.4f} | "
                      f"{num(usl.model(r['N'], fit_k8s['lambda'], fit_k8s['sigma'], fit_k8s['kappa']))} |")
    cmp = {}
    for n in range(1, 5):
        p = LAB2 / f"n{n}_steps.json"
        if p.exists():
            x = jl(p)["X"]
            cmp[n] = (x["value"], x["spread"])
    if 1 in cmp and len(cmp) >= 3:
        fit_cmp = usl.fit(cmp[1][0], [(n, v[0]) for n, v in cmp.items() if n > 1])
        md.append(f"\nCompose (лабораторна 2): λ = {num(fit_cmp['lambda'])}; σ = {fit_cmp['sigma']:.4f}; "
                  f"κ = {fit_cmp['kappa']:.5f}; N* = {num(fit_cmp['N_star'], 2) if fit_cmp['N_star'] else 'н/д'}.\n")
    if fit_k8s:
        pl.usl_compare({n: (xs[n], spread[n]) for n in xs}, fit_k8s, cmp, fit_cmp, FIGS / "usl_compare.png")
    A["usl_k8s"] = fit_k8s and {k: fit_k8s[k] for k in ("lambda", "sigma", "kappa", "N_star", "note")}
    A["usl_compose"] = fit_cmp and {k: fit_cmp[k] for k in ("lambda", "sigma", "kappa", "N_star", "note")}

    # ---- кроки 10-12 ----
    E = {}
    for mode in ("fixed", "hpa"):
        if (OUT / f"profile_{mode}_steps.json").exists():
            E[mode] = elastic(mode, xs, fit_k8s)
    if "fixed" in E:
        fx = E["fixed"]
        p99_0 = fx["steps"][0]["client_p99_ms"]
        p99_ref = fx["steps"][1]["client_p99_ms"]
        md.append("\n## Кроки 10-12. Змінне навантаження\n")
        md.append(f"Профіль: `{fx['meta']['profile']}` (тривалість:інтенсивність), рівень стрибка = min(2,5·X(1), 0,9·X({R})) = "
                  f"{fx['meta']['jump']} req/s. p99₀ (перший відрізок без HPA) = {num(p99_0)} мс; поріг 2·p99₀ = {num(2 * p99_0)} мс; "
                  f"p99 без HPA на стрибку = {num(p99_ref)} мс.\n")
        md.append("| прогін | відрізок | задана | досягнута | p99 клієнта, мс | не 2xx |\n|---|---|---|---|---|---|")
        for mode, e in E.items():
            for s in e["steps"]:
                md.append(f"| {mode} | {s['step']} | {num(s['target'], 0)} | {num(s['achieved_mean'])} | {num(s['client_p99_ms'])} | "
                          f"{num(s['non2xx_share'] * 100, 2)} % |")
        M = {mode: metrics(e, p99_0) for mode, e in E.items()}
        md.append("\n| прогін | θ_U | θ_O | a_U | a_O | W, репліко-с | T, с | с порушення порогу | не 2xx | помилки зʼєднання |\n|---|---|---|---|---|---|---|---|---|---|")
        for mode, m in M.items():
            md.append(f"| {mode} | {num(m['theta_U'], 3)} | {num(m['theta_O'], 3)} | {num(m['a_U'], 3)} | {num(m['a_O'], 3)} | "
                      f"{m['W']:.0f} | {m['T']} | {m['viol_s']} | {m['non2xx']} | {m['conn_errors']} |")
        A["elastic"] = M
        if "hpa" in E:
            ev = hpa_events(E["hpa"], p99_ref)
            A["hpa_events"] = ev
            f = lambda v: NA if v is None else f"{v} с"  # noqa: E731
            md.append("\n| подія (HPA) | час |\n|---|---|")
            md.append(f"| від початку стрибка до першої зміни бажаної кількості реплік | {f(ev['desired_change'])} |")
            md.append(f"| від початку стрибка до готовності найбільшої кількості реплік ({ev['max_ready']:.0f}) | {f(ev['max_ready_at'])} |")
            md.append(f"| від початку стрибка до p99 < 1,2·{num(p99_ref)} мс 10 с поспіль | {f(ev['p99_recover'])} |")
            md.append(f"| від кінця стрибка до першого зменшення готових реплік | {f(ev['scale_down'])} |")
        pl.elastic(E, p99_0, FIGS)
        A["profile"] = fx["meta"]["profile"]

    # ---- Д2 ----
    if (OUT / "d2.json").exists():
        d = jl(OUT / "d2.json")
        md.append(f"\n## Д2. Нестача зʼєднань: max_connections = {d['max_connections']} (показує база: {d.get('max_connections_shown')})\n")
        md.append("| N | задана | досягнута | не 2xx | помилки зʼєднання | p99 клієнта, мс | зʼєднань з базою (pg_stat_activity, за подами) | за postgres-exporter | помилок у журналі бази |\n|---|---|---|---|---|---|---|---|---|")
        for s in d["scan"]:
            cl = s["db_clients"].get("psql_error") or ", ".join(f"{k[-5:]}={v}" for k, v in s["db_clients"].items())
            md.append(f"| {s['N']} | {s['rate']} | {num(s['achieved'])} | {s['non2xx_total']} | {s['conn_errors']} | "
                      f"{num(s['client_p99_ms'])} | {cl} (усього {s['db_clients_total']}) | {s.get('pg_exporter_connections', 'н/д')} | {s['db_log_errors']} |")
        md.append("\n| R = 3 | DB_POOL_SIZE | X(R), req/s | зʼєднань з базою (pg_stat_activity / exporter) | не 2xx до кінця сходинки X / за прогін | помилки зʼєднання до X / за прогін | помилок у журналі бази |\n|---|---|---|---|---|---|---|")
        for lab in ("before", "after"):
            if lab in d:
                x = d[lab]
                md.append(f"| {'до' if lab == 'before' else 'після'} виправлення | {x['pool']} | {num(x['X']['value'])} | "
                          f"{x['db_clients'].get('psql_error') or x['db_clients_total']} / {x.get('pg_exporter_connections', 'н/д')} | "
                          f"{x.get('non2xx_to_X', 'н/д')} / {x['non2xx_total']} | {x.get('conn_errors_to_X', 'н/д')} / {x['conn_errors_total']} | "
                          f"{x['db_log_errors']} |")
        if "pool_change" in d:
            pc = d["pool_change"]
            md.append(f"\nЗміна DB_POOL_SIZE: rolling update завершився сам: {'так' if pc['rollout_ok'] else 'ні'}"
                      + ("" if pc["rollout_ok"] else f" ({' '.join(pc.get('rollout_status', []))}); поди перезапущено: {pc['fallback']}") + ".")
        samples = [s for s in d["scan"] for s in s["db_log_sample"]][:3]
        if samples:
            md.append("\nЖурнал бази:\n\n```\n" + "\n".join(samples) + "\n```")
        A["d2"] = d

    (OUT / "summary_lab3.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (OUT / "analysis.json").write_text(json.dumps(A, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("записано", OUT / "summary_lab3.md")


if __name__ == "__main__":
    main()
