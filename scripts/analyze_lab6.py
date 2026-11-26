#!/usr/bin/env python3
"""Аналіз лабораторної 6: results/lab6/*.json → results/lab6/summary_lab6.md, analysis.json, figs/*.png.

  python scripts/analyze_lab6.py           основні прогони
  python scripts/analyze_lab6.py --quick   перевірочні (results/lab6/quick)

Для вимірів швидкості з кількох повторів береться медіанний прогін: той, у якого X медіанне,
і t_step з нього ж. t_step,ns — медіанний за X прогін no_sync тієї самої конфігурації.
"""
import argparse
import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ddp"))
import common as cm  # noqa: E402

V = cm.VARIANT
P_MAX, B = V["P_max"], V["B"]
b0 = B // P_MAX


def num(x, d=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.{d}f}".replace(",", " ").replace(".", ",")


def sci(x):
    if x is None:
        return "—"
    if x == 0:
        return "0"
    m, e = f"{x:.2e}".split("e")
    return f"{m.replace('.', ',')}·10^{int(e)}"


def load(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def median_run(out, prefix):
    """Прогони prefix_r*.json → (медіанний за X прогін, усі прогони)."""
    runs = [load(p) for p in sorted(out.glob(f"{prefix}_r*.json"))]
    runs = [r for r in runs if r]
    if not runs:
        return None, []
    srt = sorted(runs, key=lambda r: r["X"])
    return srt[(len(srt) - 1) // 2], runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    out = ROOT / "results" / "lab6" / ("quick" if a.quick else "")
    figs = out / "figs"
    figs.mkdir(parents=True, exist_ok=True)
    stand = load(out / "stand.json")
    md, an = [], {"stand": stand}

    # ---- таблиця 1
    md += ["# Підсумок лабораторної 6 (варіант 12)", "",
           "Таблиця 1. Стенд", "",
           "| Процесор | Фізичних ядер C_phys | Логічних ядер | Памʼять, ГБ | ОС | Python | PyTorch | n_th |",
           "|---|---|---|---|---|---|---|---|",
           f"| {stand['cpu']} | {stand['c_phys']} | {stand['c_logical']} | {num(stand['mem_gb'])} | {stand['os']} | "
           f"{stand['python']} | {stand['torch']} | {stand['n_th']} |", "",
           "| Датасет | K | C | c1 / c2 / h | n_params | M, МіБ | N | B | P_max |",
           "|---|---|---|---|---|---|---|---|---|",
           f"| {stand['dataset']} | {stand['K']} | {stand['C']} | {' / '.join(map(str, stand['widths']))} | "
           f"{num(stand['n_params'], 0)} | {num(stand['M_MiB'], 3)} | {stand['N']} | {stand['B']} | {stand['P_max']} |",
           ""]

    # ---- таблиця 2
    single, single_all = median_run(out, "sp_single_B")
    manual, manual_all = median_run(out, f"sp_manual_P{P_MAX}")
    ddp, _ = median_run(out, f"sp_strong_ddp_P{P_MAX}")
    ns, _ = median_run(out, f"sp_strong_nosync_P{P_MAX}")
    eq = load(out / "equiv.json")
    q1 = load(out / "q_single_B.json")
    qd = load(out / "q_ddp_B_lr0.json")
    rows2 = []
    if single:
        rows2.append(("1 процес, батч B", single["t_step_ms"], 0.0, 0.0, single["X"]))
    if manual:
        rows2.append((f"{P_MAX} процеси, ручна синхронізація", manual["t_step_ms"], manual["t_sync_ms"],
                      manual["t_sync_ms"] / manual["t_step_ms"], manual["X"]))
    if ddp and ns:
        tce = ddp["t_step_ms"] - ns["t_step_ms"]
        rows2.append((f"{P_MAX} процеси, DDP", ddp["t_step_ms"], tce, tce / ddp["t_step_ms"], ddp["X"]))
        rows2.append((f"{P_MAX} процеси, DDP, no_sync", ns["t_step_ms"], 0.0, 0.0, ns["X"]))
    md += ["Таблиця 2. Синхронізація градієнтів", "",
           "| Режим | t_step, мс | Час обміну на крок, мс | Частка кроку в обміні | X, прикл./с |",
           "|---|---|---|---|---|"]
    md += [f"| {r[0]} | {num(r[1], 2)} | {num(r[2], 2)} | {num(r[3], 3)} | {num(r[4], 0)} |" for r in rows2]
    md += ["", "| max abs(g − ḡ) / max abs(g) | Різниця сум ваг між процесами після 50 кроків | ACC, 1 процес | "
           "ACC, DDP на P_max | AUC, 1 процес | AUC, DDP на P_max |", "|---|---|---|---|---|---|",
           f"| {sci(eq and eq['max_ratio'])} | {sci(eq and eq['weight_sum_spread'])} | {num(q1 and q1['acc'], 4)} | "
           f"{num(qd and qd['acc'], 4)} | {num(q1 and q1['auc'], 4)} | {num(qd and qd['auc'], 4)} |", ""]
    an["table2"] = rows2
    if eq:
        md += ["Перевірка градієнта за тензорами (крок 2):", "",
               "| тензор | форма | max abs(g) | max abs(g − ḡ) | відношення |", "|---|---|---|---|---|"]
        md += [f"| {g['name']} | {'×'.join(map(str, g['shape']))} | {sci(g['max_abs_g'])} | "
               f"{sci(g['max_abs_diff'])} | {sci(g['ratio'])} |" for g in eq["grad_check"]]
        md += ["", f"Суми ваг процесів після {eq['equiv_steps']} кроків: "
               + ", ".join(f"{s:.12f}" for s in eq["weight_sums"]), ""]
    if q1 and qd:
        n = min(len(q1["losses"]), len(qd["losses"]))
        dl = [abs(x["loss"] - y["loss"]) for x, y in zip(q1["losses"][:n], qd["losses"][:n])]
        md += [f"Втрата на кроці: 1 процес (B = {B}) проти DDP на {P_MAX} процесах, {n} кроків: найбільша різниця "
               f"{sci(max(dl))}, на останньому кроці {sci(dl[-1])}.", ""]
        an["loss_diff_single_vs_ddp"] = dict(max=max(dl), last=dl[-1])
    if manual:
        md += [f"Ручна синхронізація: усі {len(manual_all)} прогони t_step = "
               + ", ".join(num(r['t_step_ms'], 2) for r in manual_all) + " мс, t_sync = "
               + ", ".join(num(r['t_sync_ms'], 2) for r in manual_all) + " мс.", ""]
    if ddp and ddp.get("ddp_buckets"):
        md += [f"Кошики DDP після першого кроку (байт): {ddp['ddp_buckets']['bucket_sizes_bytes']}, "
               f"M = {stand['n_params'] * 4} байт.", ""]

    # ---- таблиця 3
    md += [f"Таблиця 3. Strong scaling, B = {B}", "",
           "| P | b | t_step, мс | t_step,ns, мс | X, прикл./с | S | E | e | c |", "|---|---|---|---|---|---|---|---|---|"]
    strong = {}
    for P in range(1, P_MAX + 1):
        r, allr = median_run(out, f"sp_strong_ddp_P{P}")
        rn, _ = median_run(out, f"sp_strong_nosync_P{P}")
        if r:
            strong[P] = dict(b=B // P, t=r["t_step_ms"], tns=rn and rn["t_step_ms"], X=r["X"],
                             X_all=[x["X"] for x in allr])
    X1 = strong.get(1, {}).get("X")
    for P, s in strong.items():
        S = s["X"] / X1 if X1 else None
        E = S / P if S else None
        e = (1 / S - 1 / P) / (1 - 1 / P) if S and P > 1 else None
        c = (s["t"] - s["tns"]) / s["t"] if s["tns"] else None
        s.update(S=S, E=E, e=e, c=c)
        md.append(f"| {P} | {s['b']} | {num(s['t'], 2)} | {num(s['tns'], 2)} | {num(s['X'], 0)} | {num(S, 2)} | "
                  f"{num(E, 3)} | {'−' if P == 1 else num(e, 3)} | {num(c, 3)} |")
    md += ["", "X усіх повторів: " + "; ".join(f"P = {P}: " + ", ".join(num(x, 0) for x in s["X_all"])
                                                for P, s in strong.items()), ""]
    an["strong"] = strong

    # ---- таблиця 4
    weak = {}
    for P in range(1, P_MAX):
        r, allr = median_run(out, f"sp_weak_ddp_P{P}")
        if r:
            weak[P] = dict(BP=P * b0, t=r["t_step_ms"], X=r["X"], X_all=[x["X"] for x in allr])
    if P_MAX in strong:
        weak[P_MAX] = dict(BP=B, t=strong[P_MAX]["t"], X=strong[P_MAX]["X"], X_all=strong[P_MAX]["X_all"])
    Xw1 = weak.get(1, {}).get("X")
    md += [f"Таблиця 4. Weak scaling, b = {b0}", "", "| P | B_P | t_step, мс | X, прикл./с | S_w | E_w |",
           "|---|---|---|---|---|---|"]
    for P, w in sorted(weak.items()):
        Sw = w["X"] / Xw1 if Xw1 else None
        w.update(Sw=Sw, Ew=Sw / P if Sw else None)
        md.append(f"| {P} | {w['BP']} | {num(w['t'], 2)} | {num(w['X'], 0)} | {num(Sw, 2)} | {num(w['Ew'], 3)} |")
    md += ["", f"Рядок P = {P_MAX} узято з таблиці 3.", ""]
    an["weak"] = weak

    # ---- таблиця 5
    qb = load(out / "q_ddp1_b_lr0.json")
    qp = load(out / "q_ddp_B_lrP.json")
    md += ["Таблиця 5. Якість за різних глобальних батчів", "",
           "| Режим | B_P | η | Кроків оптимізатора за n_ep епох | ACC | AUC | Найбільша втрата за останню епоху |",
           "|---|---|---|---|---|---|---|"]
    for title, q in (("P = 1", qb), (f"P = {P_MAX}, з кроку 2", qd), (f"P = {P_MAX}", qp)):
        if q:
            md.append(f"| {title} | {q['B']} | {num(q['lr'], 2)} | {q['steps']} | {num(q['acc'], 4)} | "
                      f"{num(q['auc'], 4)} | {num(q['max_loss_last_epoch'], 4)} |")
    md.append("")
    if qp:
        first = [x["loss"] for x in qp["losses"][:10]]
        md += [f"Втрата перших 10 кроків з η_P = {num(qp['lr'], 2)}: " + ", ".join(num(x, 3) for x in first), ""]
    an["table5"] = {k: q and {kk: q[kk] for kk in ("B", "lr", "steps", "acc", "auc", "max_loss_last_epoch")}
                    for k, q in (("P1_b", qb), ("Pmax_lr0", qd), ("Pmax_lrP", qp))}

    # ---- таблиця 6
    wd = out / "fault"
    fr = load(wd / "result.json")
    if fr and (wd / "events.jsonl").exists():
        ev = [json.loads(x) for x in (wd / "events.jsonl").read_text(encoding="utf-8").splitlines() if x]
        mk = json.loads((wd / "failed.marker").read_text(encoding="utf-8"))
        t_f, s_f = mk["t_f"], mk["step"]
        ck = [e for e in ev if e["ev"] == "ckpt" and e["attempt"] == 0]
        t_c, s_c = ck[-1]["t"], ck[-1]["step"]
        st1 = next(e for e in ev if e["ev"] == "start" and e["attempt"] == 1)
        res1 = next(e for e in ev if e["ev"] == "resumed" and e["attempt"] == 1)
        fst = next(e for e in ev if e["ev"] == "first_step_after_resume")
        t_r = fst["t"] - t_f
        t_lost = (t_f - t_c) + t_r
        redo = (s_f - 1) - s_c
        md += ["Таблиця 6. Відмова і відновлення", "",
               "| K_c | s_f | Крок останньої контрольної точки | Кроків виконано повторно | t_lost, с |",
               "|---|---|---|---|---|",
               f"| {V['K_c']} | {s_f} | {s_c} | {redo} | {num(t_lost, 2)} |", "",
               f"t_f − t_c = {num(t_f - t_c, 3)} с; t_r = {num(t_r, 2)} с, з них: від відмови до старту процесів "
               f"нової спроби {num(st1['t_proc'] - t_f, 2)} с, імпорт і створення групи "
               f"{num(st1['t_group'] - st1['t_proc'], 2)} с, дані, модель і контрольна точка "
               f"{num(res1['t'] - st1['t_group'], 2)} с, перший крок {num(fst['t'] - res1['t'], 3)} с.", ""]
        lf = {}
        for x in (wd / "losses.jsonl").read_text(encoding="utf-8").splitlines():
            x = json.loads(x)
            lf[x["step"]] = x["loss"]             # остання спроба перезаписує повторені кроки
        l0 = {x["step"]: x["loss"] for x in qd["losses"]} if qd else {}
        steps = list(range(V["s_f"] - 2, V["s_f"] + 3))
        md += ["| Крок | " + " | ".join(map(str, steps)) + " | ACC після n_ep епох |",
               "|---" * (len(steps) + 2) + "|",
               "| Втрата, прогін з відмовою | " + " | ".join(num(lf.get(s), 6) for s in steps)
               + f" | {num(fr['acc'], 4)} |",
               "| Втрата, прогін без відмови | " + " | ".join(num(l0.get(s), 6) for s in steps)
               + f" | {num(qd and qd['acc'], 4)} |", ""]
        att = {}
        for x in (wd / "losses.jsonl").read_text(encoding="utf-8").splitlines():
            x = json.loads(x)
            att.setdefault(x["step"], {})[x["attempt"]] = x["loss"]
        rep = {s: v for s, v in att.items() if len(v) > 1}
        same_rep = all(v[0] == v[1] for v in rep.values())
        same_all = all(lf.get(s) == l0.get(s) for s in l0) and len(lf) == len(l0)
        md += [f"Кроки {', '.join(map(str, sorted(rep)))} виконано двічі; втрата повтору збігається з першою спробою "
               f"біт у біт: {'так' if same_rep else 'ні'}. Втрата всіх {len(l0)} кроків збігається з прогоном без "
               f"відмови біт у біт: {'так' if same_all else 'ні'}.", ""]
        diff = [s for s in sorted(l0) if lf.get(s) != l0.get(s)]
        if diff:
            md += [f"Перший крок, де втрата прогону з відмовою відрізняється від прогону без відмови: {diff[0]}; "
                   f"найбільша відносна різниця {sci(max(abs(lf[s] - l0[s]) / abs(l0[s]) for s in diff))}.", ""]
        mm = out / "fault_manual" / "losses.jsonl"
        qm = load(out / "q_manual_B_lr0.json")
        if mm.exists() and qm:
            lm = {}
            for x in mm.read_text(encoding="utf-8").splitlines():
                x = json.loads(x)
                lm[x["step"]] = x["loss"]
            l0m = {x["step"]: x["loss"] for x in qm["losses"]}
            dm = [s for s in sorted(l0m) if lm.get(s) != l0m[s]]
            md += [f"Контроль з ручною синхронізацією (та сама відмова): втрата всіх {len(l0m)} кроків збігається з "
                   f"прогоном без відмови біт у біт: {'так' if not dm else 'ні, перший різний крок ' + str(dm[0])}.", ""]
            an["fault_manual_same_all"] = not dm
        an["fault"] = dict(first_diff_step=diff[0] if diff else None, s_f=s_f, s_c=s_c, redo=redo, t_lost=t_lost, t_f_minus_t_c=t_f - t_c, t_r=t_r,
                           acc=fr["acc"], auc=fr["auc"], same_rep=same_rep, same_all=same_all)

    (out / "summary_lab6.md").write_text("\n".join(md), encoding="utf-8")
    (out / "analysis.json").write_text(json.dumps(an, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\n".join(md))
    plots(figs, strong, weak, X1, Xw1, (q1, qd, qb, qp))


def plots(figs, strong, weak, X1, Xw1, qs):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib немає — графіки пропущено")
        return
    for name, data, x1, title in (("strong_X", strong, X1, f"Strong scaling, B = {B}"),
                                  ("weak_X", weak, Xw1, f"Weak scaling, b = {b0}")):
        if not data or not x1:
            continue
        P = sorted(data)
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.plot(P, [p * x1 for p in P], "--", color="#888", label="ідеал P · X(1)")
        ax.plot(P, [data[p]["X"] for p in P], "o-", color="#2a6fdb", label="DDP, виміряно")
        ax.set_xlabel("P, процесів")
        ax.set_ylabel("X, прикладів/с")
        ax.set_xticks(P)
        ax.set_title(title)
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(figs / f"{name}.png", dpi=130)
        plt.close(fig)
    q1, qd, qb, qp = qs
    fig, ax = plt.subplots(figsize=(7, 4))
    for q, lab in ((qb, f"P = 1, B = {b0}, η₀"), (qd, f"P = {P_MAX}, B = {B}, η₀"),
                   (qp, f"P = {P_MAX}, B = {B}, η_P")):
        if q:
            spe = q["steps"] / cm.N_EP
            ax.plot([x["step"] / spe for x in q["losses"]], [x["loss"] for x in q["losses"]], label=lab, lw=1)
    ax.set_xlabel("епоха")
    ax.set_ylabel("втрата на кроці")
    ax.set_title("Втрата за різних глобальних батчів")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(figs / "loss_batches.png", dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    main()
