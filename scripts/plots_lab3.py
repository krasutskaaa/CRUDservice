"""Діаграми лабораторної 3 (matplotlib)."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import plots_lab2 as pl2  # noqa: E402
import usl  # noqa: E402


def timeline(label, steps_json, seconds_csv, out):
    sec = pl2.load_seconds(seconds_csv)
    x = [r["second"] for r in sec]
    fig, ax = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
    ax[0].plot(x, [r["target"] for r in sec], "--", label="задана")
    ax[0].plot(x, [r["achieved"] for r in sec], label="досягнута (2xx/с)")
    ax[0].set_ylabel("req/s"); ax[0].legend()
    ax[1].plot(x, [r["p99_ms"] for r in sec], label="p99 клієнта")
    ax[1].set_yscale("log"); ax[1].set_ylabel("мс"); ax[1].legend()
    ax[2].plot(x, [r["non2xx"] + r["conn_errors"] for r in sec], color="tab:red")
    ax[2].set_ylabel("не 2xx + помилки зʼєднання, /с"); ax[2].set_xlabel("секунда прогону")
    fig.suptitle(f"Прогін {label}")
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def pods_rps(steps, prom, out, title):
    t0 = steps["meta"]["t0"]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    tot = None
    for s in prom.get("rps_by_pod", {}).get("result", []):
        pts = [(float(t) - t0, float(v)) for t, v in s["values"] if v != "NaN"]
        ax.plot([a for a, _ in pts], [b for _, b in pts], label=s["metric"].get("pod"))
    tg = [(s["window_seconds"][0] - 10, s["target"]) for s in steps["steps"]]
    ax.step([a for a, _ in tg], [b / 3 for _, b in tg], where="post", ls="--", color="gray", label="задана / 3")
    ax.set_xlabel("секунда прогону"); ax.set_ylabel("req/s пода (rate за 15 с)")
    ax.set_title(f"Запитів за секунду на кожен под, {title}"); ax.legend(fontsize=7); ax.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)
    return tot


def usl_compare(k8s, fit_k8s, cmp, fit_cmp, out):
    fig, ax = plt.subplots(figsize=(7, 4.5))
    x1 = k8s[1][0]
    ns = sorted(k8s)
    ax.errorbar(ns, [k8s[n][0] / x1 for n in ns], yerr=[k8s[n][1] / x1 for n in ns], fmt="o", capsize=4,
                color="tab:blue", label="Kubernetes (k3d), виміряно")
    top = 6
    grid = [1 + i * (top - 1) / 100 for i in range(101)]
    ax.plot(grid, [usl.model(n, 1, fit_k8s["sigma"], fit_k8s["kappa"]) for n in grid], color="tab:blue",
            label=f"закон, k3d: σ={fit_k8s['sigma']:.3f}, κ={fit_k8s['kappa']:.4f}")
    if cmp:
        c1 = cmp[1][0]
        nc = sorted(cmp)
        ax.errorbar(nc, [cmp[n][0] / c1 for n in nc], yerr=[cmp[n][1] / c1 for n in nc], fmt="s", capsize=4,
                    color="tab:orange", label="Compose (лаб. 2), виміряно")
        if fit_cmp:
            ax.plot(grid, [usl.model(n, 1, fit_cmp["sigma"], fit_cmp["kappa"]) for n in grid], color="tab:orange",
                    ls="--", label=f"закон, Compose: σ={fit_cmp['sigma']:.3f}, κ={fit_cmp['kappa']:.4f}")
    ax.set_xlabel("N"); ax.set_ylabel("C(N) = X(N) / X(1)"); ax.grid(alpha=.3); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def elastic(E, p99_0, figs):
    """Для обох прогонів однакові шкали осей."""
    ymax_rps = max(max(max(r["z"], r["achieved"]) for r in e["rows"]) for e in E.values()) * 1.1
    p99s = [r["p99"] for e in E.values() for r in e["rows"] if r["p99"] == r["p99"] and r["p99"] > 0]
    ylo, yhi = min(p99s + [p99_0]) * 0.8, max(p99s + [2 * p99_0]) * 1.3
    cpus = [v for e in E.values() for pts in e["cpu"].values() for _, v in pts]
    ycpu = max(cpus + [100]) * 1.1
    T = max(e["T"] for e in E.values())
    for mode, e in E.items():
        rows = e["rows"]
        t = [r["t"] for r in rows]
        fig, ax = plt.subplots(4, 1, figsize=(9, 11), sharex=True)
        ax[0].plot(t, [r["z"] for r in rows], "--", label="задана z(t)")
        ax[0].plot(t, [r["achieved"] for r in rows], label="досягнута (2xx/с)")
        ax[0].set_ylim(0, ymax_rps); ax[0].set_ylabel("req/s"); ax[0].legend(fontsize=8)
        ax[1].plot(t, [r["p99"] for r in rows], label="p99 клієнта (посекундно)")
        ax[1].axhline(2 * p99_0, color="tab:red", ls=":", label="поріг 2·p99₀")
        ax[1].set_yscale("log"); ax[1].set_ylim(ylo, yhi); ax[1].set_ylabel("мс"); ax[1].legend(fontsize=8)
        ax[2].step(t, [r["d"] for r in rows], where="post", label="потрібна d(t)")
        ax[2].step(t, [r["s"] for r in rows], where="post", label="готові s(t)")
        if any(r["desired"] is not None for r in rows):
            ax[2].step(t, [r["desired"] if r["desired"] is not None else float("nan") for r in rows], where="post",
                       ls=":", label="бажана (HPA)")
        ax[2].set_ylim(0, 3.5); ax[2].set_yticks([0, 1, 2, 3]); ax[2].set_ylabel("реплік"); ax[2].legend(fontsize=8)
        for pod, pts in sorted(e["cpu"].items()):
            pts = [(a, b) for a, b in pts if 0 <= a <= T]
            ax[3].plot([a for a, _ in pts], [b for _, b in pts], marker=".", lw=1, label=pod[-5:])
        ax[3].axhline(70, color="gray", ls="--", label="ціль HPA 70 %")
        ax[3].set_ylim(0, ycpu); ax[3].set_ylabel("CPU / requests.cpu, %"); ax[3].legend(fontsize=7, ncol=3)
        ax[3].set_xlim(0, T); ax[3].set_xlabel("секунда прогону")
        for a in ax:
            for s0, _ in e["segs"][1:]:
                a.axvline(s0, color="k", alpha=.15)
        fig.suptitle({"fixed": "Без автомасштабування, R = 3", "hpa": "HPA (ціль 70 %, вікно зменшення 30 с)"}[mode])
        fig.tight_layout(); fig.savefig(Path(figs) / f"elastic_{mode}.png", dpi=130); plt.close(fig)
