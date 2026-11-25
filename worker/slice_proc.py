"""Синтетичний зріз і крок обробки лабораторної 4 (варіант 12: q_lo = 0,01, q_hi = 0,99).
Лише цикли Python, без NumPy: так вимагає умова, і саме це дає t_cpu."""
S = 256
LEVELS = 4096
Q_LO = 0.01
Q_HI = 0.99


def make_slice(seed, size=S):
    """x(0) = seed, x(i+1) = (1103515245·x(i) + 12345) mod 2^31, p(i) = x(i+1) >> 19."""
    x = seed
    out = [0] * (size * size)
    for i in range(size * size):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        out[i] = x >> 19
    return out


def quantile_level(hist, n, q):
    """Найменший рівень v, для якого частка пікселів зі значенням <= v не менша за q."""
    need = q * n
    acc = 0
    for v in range(LEVELS):
        acc += hist[v]
        if acc >= need:
            return v
    return LEVELS - 1


def process(pixels, q_lo=Q_LO, q_hi=Q_HI):
    """Вікно яскравості за квантилями гістограми: поза [p_lo, p_hi] → 0, всередині розтяг на 0..4095.
    Повертає p_lo, p_hi, кількість вилучених пікселів, середнє і стандартне відхилення p'
    за пікселями, що лишились."""
    hist = [0] * LEVELS
    for p in pixels:
        hist[p] += 1
    n = len(pixels)
    p_lo = quantile_level(hist, n, q_lo)
    p_hi = quantile_level(hist, n, q_hi)
    d = p_hi - p_lo
    removed = 0
    kept = 0
    s1 = 0
    s2 = 0
    for p in pixels:
        if p < p_lo or p > p_hi:
            removed += 1
            continue
        # ⌊4095·(p − p_lo)/(p_hi − p_lo)⌉ у цілих числах: округлення до найближчого
        v = (2 * 4095 * (p - p_lo) + d) // (2 * d) if d else 0
        kept += 1
        s1 += v
        s2 += v * v
    mean = s1 / kept if kept else 0.0
    var = s2 / kept - mean * mean if kept else 0.0
    return {"p_lo": p_lo, "p_hi": p_hi, "removed": removed,
            "mean": round(mean, 6), "std": round(max(var, 0.0) ** 0.5, 6)}


if __name__ == "__main__":
    import sys
    import time
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 12345
    t = time.perf_counter()
    px = make_slice(seed)
    r = process(px)
    print(r, f"{(time.perf_counter() - t) * 1000:.1f} мс")
