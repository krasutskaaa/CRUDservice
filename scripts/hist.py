"""Оцінка квантиля за гістограмою Prometheus (лінійна інтерполяція всередині бакета)."""


def quantile(q, bounds, cum, total=None):
    """bounds: скінченні верхні межі b(1..k); cum: накопичені c(1..k); total: n (бакет +Inf)."""
    n = cum[-1] if total is None else total
    if n <= 0:
        return None
    r = q * n
    pb, pc = 0.0, 0.0
    for b, c in zip(bounds, cum):
        if pc < r <= c:
            return {"value": pb + (b - pb) * (r - pc) / (c - pc), "lo": pb, "hi": b,
                    "max_error": b - pb, "beyond": False}
        pb, pc = b, c
    return {"value": bounds[-1], "lo": bounds[-1], "hi": None, "max_error": None, "beyond": True}


def fmt_ms(res):
    if res is None:
        return "н/д"
    if res["beyond"]:
        return f"> {res['lo'] * 1000:g} мс"
    return (f"{res['value'] * 1000:.1f} мс (бакет {res['lo'] * 1000:g}-{res['hi'] * 1000:g} мс, "
            f"похибка до {res['max_error'] * 1000:g} мс)")
