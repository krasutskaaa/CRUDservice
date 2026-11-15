"""Універсальний закон масштабованості: X(N) = λN / (1 + σ(N−1) + κN(N−1)).
МНК без вільного члена за N/C(N) − 1 = σ(N−1) + κN(N−1), C(N) = X(N)/X(1)."""
import math


def fit(x1, points):
    pts = [(n, x) for n, x in points if n > 1]
    if len(pts) < 2:
        raise ValueError("для підбору потрібно щонайменше дві точки з N > 1")
    rows = []
    for n, x in pts:
        c = x / x1
        rows.append({"N": n, "X": x, "C": c, "y": n / c - 1, "u": n - 1, "v": n * (n - 1)})
    suu = sum(r["u"] ** 2 for r in rows)
    svv = sum(r["v"] ** 2 for r in rows)
    suv = sum(r["u"] * r["v"] for r in rows)
    suy = sum(r["u"] * r["y"] for r in rows)
    svy = sum(r["v"] * r["y"] for r in rows)
    d = suu * svv - suv ** 2
    sigma = (svv * suy - suv * svy) / d
    kappa = (suu * svy - suv * suy) / d
    note = "обидві оцінки невідʼємні"
    if sigma < 0 or kappa < 0:
        if sigma < 0:
            sigma, kappa, note = 0.0, svy / svv, "σ < 0: прирівняно до 0, κ = Σvy / Σv²"
            if kappa < 0:
                kappa, note = 0.0, "σ < 0 і κ < 0: обидві прирівняно до 0"
        else:
            kappa, sigma, note = 0.0, suy / suu, "κ < 0: прирівняно до 0, σ = Σuy / Σu²"
            if sigma < 0:
                sigma, note = 0.0, "κ < 0 і σ < 0: обидві прирівняно до 0"
    n_star = math.sqrt((1 - sigma) / kappa) if kappa > 0 and sigma < 1 else None
    return {"lambda": x1, "sigma": sigma, "kappa": kappa, "N_star": n_star, "note": note,
            "sums": {"suu": suu, "svv": svv, "suv": suv, "suy": suy, "svy": svy, "det": d}, "rows": rows}


def model(n, lam, sigma, kappa):
    return lam * n / (1 + sigma * (n - 1) + kappa * n * (n - 1))
