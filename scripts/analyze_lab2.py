#!/usr/bin/env python3
"""Аналіз результатів лабораторної 2 → results/lab2/summary_lab2.md і figs/."""
import json
import os
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hist  # noqa: E402
import plots_lab2 as pl  # noqa: E402
import usl  # noqa: E402
from common import RESULTS  # noqa: E402

OUT = RESULTS / "lab2"
FIGS = OUT / "figs"
POOL = int(os.environ.get("DB_POOL_SIZE", "5"))


def jl(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def pct(s):
    return float(str(s).strip().rstrip("%") or 0)


def stats_at(n, win):
    """Середнє docker stats (CPU %, памʼять МіБ) за вікно сходинки, по сервісах."""
    p = OUT / f"docker_stats_n{n}.jsonl"
    acc = {}
    if not p.exists():
        return acc
    for line in p.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if not (win[0] <= d["t"] <= win[1]):
            continue
        svc = "web" if "-web-" in d["Name"] else d["Name"].split("-")[-2] if d["Name"].count("-") >= 2 else d["Name"]
        mem = d["MemUsage"].split("/")[0].strip()
        val = float(''.join(c for c in mem if c.isdigit() or c == '.'))
        if mem.endswith("GiB"):
            val *= 1024
        elif mem.endswith("KiB"):
            val /= 1024
        acc.setdefault(d["Name"], []).append((pct(d["CPUPerc"]), val))
    return {k: (st.fmean(c for c, _ in v), st.fmean(m for _, m in v)) for k, v in acc.items()}


def main():
    FIGS.mkdir(parents=True, exist_ok=True)
    md = ["# Підсумок лабораторної 2 (автоматично згенеровано)\n"]
    g = jl(OUT / "G.json") if (OUT / "G.json").exists() else None
    if g:
        md.append(f"Межа генератора G = **{g['G']:.0f} req/s** (±{g['spread']:.0f}), причина зупинки: {g['stop']}.\n")
    runs = {}
    for n in range(1, 5):
        if (OUT / f"n{n}_steps.json").exists():
            runs[n] = jl(OUT / f"n{n}_steps.json")
    rows = []
    md.append("\n## X(N)\n\n| N | X(N), req/s | розкид (σ за вікном) | сходинка | задана | обмежено генератором | p99 клієнта на X, мс | не 2xx на сходинці |\n|---|---|---|---|---|---|---|---|")
    for n, r in sorted(runs.items()):
        x = r["X"]
        best = next(s for s in r["steps"] if s["step"] == x["step"])
        lim = bool(g and (x["value"] >= 0.9 * g["G"] or r["last_step_ratio"] >= 0.9))
        rows.append({"N": n, "X": x["value"], "spread": x["spread"], "limited": lim, "step": x["step"]})
        md.append(f"| {n} | {x['value']:.1f} | {x['spread']:.1f} | {x['step']} | {x['target']:.0f} | "
                  f"{'так' if lim else 'ні'} | {x['client_p99_ms']:.1f} | {best['non2xx_share']*100:.2f}% |")
        pl.timeline(f"N={n}", OUT / f"n{n}_steps.json", OUT / f"n{n}_seconds.csv", OUT / f"prom_n{n}.json",
                    FIGS / f"timeline_n{n}.png")
    if rows:
        pl.instances_plot(rows, FIGS / "instances.png")

    # p99 клієнт vs сервер, N=1
    if 1 in runs and (OUT / "prom_n1.json").exists():
        prom = jl(OUT / "prom_n1.json")
        md.append("\n## p99: клієнт і сервер, N=1\n\n| сходинка | задана | досягнута | p99 клієнта, мс | p99 сервера (гістограма) | бакет / похибка |\n|---|---|---|---|---|---|")
        for s in runs[1]["steps"]:
            q = pl.server_quantile(prom, *s["window_epoch"])
            md.append(f"| {s['step']} | {s['target']:.0f} | {s['achieved_mean']:.0f} | {s['client_p99_ms']:.1f} | "
                      f"{hist.fmt_ms(q).split(' (')[0]} | "
                      f"{'—' if not q or q['beyond'] else f'{q['lo']*1000:g}–{q['hi']*1000:g} мс / ≤{q['max_error']*1000:g} мс'} |")

    # USL
    if 1 in runs:
        pts = [(r["N"], r["X"]) for r in rows if not r["limited"] and r["N"] > 1]
        good1 = not rows[0]["limited"]
        md.append("\n## USL\n")
        if not good1:
            md.append("X(1) обмежено генератором — підбір USL некоректний; зменшіть CPU-ліміт (`cpus: \"0.5\"`) і повторіть.\n")
        elif len(pts) >= 2:
            f = usl.fit(rows[0]["X"], pts)
            nstar = f["N_star"]
            md.append(f"λ = X(1) = {f['lambda']:.1f}; σ = {f['sigma']:.4f}; κ = {f['kappa']:.5f}; "
                      f"N* = {'н/д (κ=0)' if nstar is None else f'{nstar:.2f}'}. {f['note']}.\n")
            md.append("| N | X(N) | C(N)=X/X(1) | y=N/C−1 | u=N−1 | v=N(N−1) | X за моделлю |\n|---|---|---|---|---|---|---|")
            for r in f["rows"]:
                md.append(f"| {r['N']} | {r['X']:.1f} | {r['C']:.3f} | {r['y']:.4f} | {r['u']} | {r['v']} | "
                          f"{usl.model(r['N'], f['lambda'], f['sigma'], f['kappa']):.1f} |")
            pl.usl_plot([(1, rows[0]["X"])] + pts, f, FIGS / "usl.png")
        else:
            md.append("Менше двох точок N>1 без обмеження генератором — підбір неможливий; зменшіть CPU-ліміт і повторіть.\n")

    # docker stats на сходинці X(4)
    for n in (4,):
        if n in runs:
            s = next(s for s in runs[n]["steps"] if s["step"] == runs[n]["X"]["step"])
            md.append(f"\n## docker stats на сходинці X({n}) (сходинка {s['step']}, задана {s['target']:.0f})\n\n| контейнер | CPU, % | памʼять, МіБ |\n|---|---|---|")
            for name, (c, m) in sorted(stats_at(n, s["window_epoch"]).items()):
                md.append(f"| {name} | {c:.0f} | {m:.0f} |")
            md.append(f"| генератор (процеси) | {s['gen_cpu_cores']*100:.0f} | — |")

    # М2
    md.append("\n## М2\n\n| N | макс. зʼєднань (усі стани) | N·пул | макс. active | транзакцій/с на X-сходинці | задана |\n|---|---|---|---|---|---|")
    for n in (1, 4):
        if n in runs and (OUT / f"prom_n{n}.json").exists():
            prom = jl(OUT / f"prom_n{n}.json")
            states = pl.series(prom, "pg_connections_by_state")
            tot = {}
            for pts in states.values():
                for t, v in pts:
                    tot[t] = tot.get(t, 0) + v
            act = max((v for _, v in states.get("active", [])), default=0)
            s = next(s for s in runs[n]["steps"] if s["step"] == runs[n]["X"]["step"])
            tx = [v for t, v in pl.series(prom, "pg_xact_per_s").get("all", [])
                  if s["window_epoch"][0] <= t <= s["window_epoch"][1]]
            md.append(f"| {n} | {max(tot.values(), default=0):.0f} | {n*POOL} | {act:.0f} | "
                      f"{(st.fmean(tx) if tx else float('nan')):.0f} | {s['target']:.0f} |")
            pl.m2_plot(f"N={n}", OUT / f"n{n}_steps.json", OUT / f"prom_n{n}.json", POOL, n, FIGS / f"m2_n{n}.png")

    if (OUT / "graceful.json").exists():
        md.append("\n## Graceful stop\n\n```json\n" + json.dumps(jl(OUT / "graceful.json"), indent=2) + "\n```")
    (OUT / "summary_lab2.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("записано", OUT / "summary_lab2.md")


if __name__ == "__main__":
    main()
