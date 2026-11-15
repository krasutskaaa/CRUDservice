"""Графіки лабораторної 2 (matplotlib)."""
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import hist  # noqa: E402
import usl  # noqa: E402

BOUNDS = [0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128, 0.256, 0.512, 1.024]


def load_seconds(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for r in rows:
        for k in ("second", "step", "target", "sent", "achieved", "non2xx", "conn_errors"):
            r[k] = float(r[k])
        for k in ("p50_ms", "p95_ms", "p99_ms", "gen_cpu_cores"):
            r[k] = float(r[k])
    return rows


def series(prom, name):
    """{instance_or_state: [(t, v)]} із відповіді query_range."""
    out = {}
    for s in prom.get(name, {}).get("result", []):
        m = s["metric"]
        key = m.get("instance") or m.get("state") or "all"
        out[key] = [(float(t), float(v)) for t, v in s["values"] if v not in ("NaN",)]
    return out


def bucket_cum(prom, t):
    """Накопичені лічильники бакетів (по le) у момент, найближчий до t."""
    by_le = {}
    for s in prom["bucket"]["result"]:
        le = s["metric"]["le"]
        vals = s["values"]
        if not vals:
            continue
        best = min(vals, key=lambda tv: abs(float(tv[0]) - t))
        by_le[le] = float(best[1])
    cum = [by_le.get(f"{b:g}", by_le.get(str(b), 0.0)) for b in BOUNDS]
    total = by_le.get("+Inf", cum[-1] if cum else 0.0)
    return cum, total


def server_quantile(prom, t_lo, t_hi, q=0.99):
    c0, n0 = bucket_cum(prom, t_lo)
    c1, n1 = bucket_cum(prom, t_hi)
    cum = [b - a for a, b in zip(c0, c1)]
    return hist.quantile(q, BOUNDS, cum, n1 - n0)


def timeline(label, steps_json, seconds_csv, prom_json, out):
    sec = load_seconds(seconds_csv)
    prom = json.loads(Path(prom_json).read_text(encoding="utf-8")) if Path(prom_json).exists() else {}
    meta = json.loads(Path(steps_json).read_text(encoding="utf-8"))
    t0 = meta["meta"]["t0"]
    x = [r["second"] for r in sec]
    fig, ax = plt.subplots(4, 1, figsize=(9, 10), sharex=True)
    ax[0].plot(x, [r["target"] for r in sec], "--", label="задана")
    ax[0].plot(x, [r["achieved"] for r in sec], label="досягнута (2xx/с)")
    ax[0].set_ylabel("req/s"); ax[0].legend()
    ax[1].plot(x, [r["p99_ms"] for r in sec], label="p99 клієнта")
    ax[1].set_yscale("log"); ax[1].set_ylabel("мс"); ax[1].legend()
    ax[2].plot(x, [r["non2xx"] + r["conn_errors"] for r in sec], color="tab:red")
    ax[2].set_ylabel("не 2xx + помилки зʼєднання, /с")
    for name, ylab, k in (("cpu_by_instance", "CPU, ядер", 3),):
        for inst, pts in series(prom, name).items():
            ax[3].plot([t - t0 for t, _ in pts], [v for _, v in pts], label=inst)
    ax[3].set_ylabel("CPU інстансу, ядер"); ax[3].set_xlabel("секунда прогону")
    ax[3].legend(fontsize=7)
    ip = series(prom, "inprogress_by_instance")
    if ip:
        ax2 = ax[3].twinx()
        for inst, pts in ip.items():
            ax2.plot([t - t0 for t, _ in pts], [v for _, v in pts], ":", alpha=.6)
        ax2.set_ylabel("запитів у обробці (пунктир)")
    fig.suptitle(f"Прогін {label}")
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def instances_plot(rows, out):
    ns = [r["N"] for r in rows]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.errorbar(ns, [r["X"] for r in rows], yerr=[r["spread"] for r in rows], fmt="o-", capsize=4)
    ax.set_xlabel("N"); ax.set_ylabel("X(N), req/s"); ax.set_xticks(ns); ax.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def usl_plot(points, fitres, out):
    fig, ax = plt.subplots(figsize=(6, 4))
    ns = [n for n, _ in points]
    ax.plot(ns, [x for _, x in points], "o", label="виміряно")
    top = max(ns) + (2 if not fitres["N_star"] else max(2, int(fitres["N_star"] * 1.5)))
    grid = [1 + i * (top - 1) / 100 for i in range(101)]
    ax.plot(grid, [usl.model(n, fitres["lambda"], fitres["sigma"], fitres["kappa"]) for n in grid],
            label=f"USL σ={fitres['sigma']:.3f}, κ={fitres['kappa']:.4f}")
    ax.set_xlabel("N"); ax.set_ylabel("X(N), req/s"); ax.legend(); ax.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def m2_plot(label, steps_json, prom_json, pool, n, out):
    prom = json.loads(Path(prom_json).read_text(encoding="utf-8"))
    meta = json.loads(Path(steps_json).read_text(encoding="utf-8"))
    t0 = meta["meta"]["t0"]
    fig, ax = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    for state, pts in series(prom, "pg_connections_by_state").items():
        ax[0].plot([t - t0 for t, _ in pts], [v for _, v in pts], label=state)
    ax[0].axhline(n * pool, color="k", ls="--", label=f"N·пул = {n * pool}")
    ax[0].set_ylabel("зʼєднань"); ax[0].legend(fontsize=8)
    tx = series(prom, "pg_xact_per_s").get("all", [])
    ax[1].plot([t - t0 for t, _ in tx], [v for _, v in tx], label="транзакцій/с (commit+rollback)")
    tg = [(s["window_epoch"][0] - t0, s["target"]) for s in meta["steps"]]
    ax[1].step([a for a, _ in tg], [b for _, b in tg], where="post", ls="--", label="задана інтенсивність")
    ax[1].set_ylabel("1/с"); ax[1].set_xlabel("секунда прогону"); ax[1].legend(fontsize=8)
    fig.suptitle(f"М2, {label}")
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)
