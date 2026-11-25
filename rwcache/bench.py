#!/usr/bin/env python3
"""Стенд лабораторної 5 (варіант 12): хеш-таблиця кешу результатів за record_id, n потоків.

Потоки з непарними номерами виконують лише put(k, v), з парними лише get(k). Ключі 0..K-1 рівномірно,
перед прогоном таблиця заповнена половиною простору ключів. Значення — «результат» (record_id, потік, номер
запису потоку), як task_results у лабораторній 4: за ним після прогону перевіряється, що get повернув
справді записане значення.

  python rwcache/bench.py --lock global                  прогін на n потоках
  python rwcache/bench.py --lock rw --policy fair        те саме з RWLock
  python rwcache/bench.py --mode x1 --lock global        X_1: один потік, запис і читання 5 : 4
  python rwcache/bench.py --mode tcs                     t_cs,w і t_cs,r одним потоком (глобальний замок)
  python rwcache/bench.py --mode readonly --lock rw      лише читачі: таблиця не змінюється
"""
import argparse
import json
import math
import os
import platform
import random
import sys
import threading
import time
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from locks import make_lock  # noqa: E402
from table import HashTable  # noqa: E402

ns = time.perf_counter_ns

N_THREADS = 9          # варіант 12
KEYS = 10_000          # Variant_parametr_3: ключі 0..9999
SAMPLE_EVERY = 64      # кожне 64-те непорожнє значення get зберігається для перевірки після прогону


def pctl(sorted_vals, q):
    """p_q = τ_(⌈q·N⌉), нумерація з 1."""
    n = len(sorted_vals)
    if n == 0:
        return None
    k = max(1, math.ceil(q * n))
    return sorted_vals[k - 1]


def role_of(i):
    return "w" if i % 2 == 1 else "r"


def prefill(table, keys, rnd):
    """Половина простору ключів, значення з «потоку 0». Повертає журнал ключів потоку 0."""
    log = array("i", rnd.sample(range(keys), keys // 2))
    for s, k in enumerate(log):
        table.put(k, (k, 0, s))
    return log


class Worker(threading.Thread):
    def __init__(self, idx, st, lock, table, keys, seed):
        super().__init__(name=f"t{idx}", daemon=True)
        self.idx, self.st, self.lock, self.table, self.keys = idx, st, lock, table, keys
        self.rnd = random.Random(seed * 1000 + idx)
        self.count = 0
        self.tau = array("q")      # тривалість операції, нс
        self.wait_sum = 0          # Σ t_wait у межах [t0, t_end), нс
        self.wait_max = 0          # найбільший t_wait у межах [t0, t_end), нс
        self.log = array("i")      # писець: ключ кожного свого запису (номер запису = індекс)
        self.bad = 0               # читач: get повернув значення іншого ключа
        self.samples = []          # читач: (k, v) для перевірки після прогону
        self.misses = 0            # читач: get повернув None

    def run(self):
        st = self.st
        st["barrier"].wait()
        t0, t_end = st["t0"], st["t_end"]
        rnd_k = self.rnd.randrange
        keys = self.keys
        tau_a = self.tau.append
        table = self.table
        cnt = 0
        wsum = wmax = 0
        if role_of(self.idx) == "w":
            acq, rel, put = self.lock.acquire_write, self.lock.release_write, table.put
            log_a = self.log.append
            idx = self.idx
            seq = 0
            while True:
                k = rnd_k(keys)
                v = (k, idx, seq)
                t_call = ns()
                acq()
                t_acq = ns()
                put(k, v)
                rel()
                t_ret = ns()
                log_a(k)
                seq += 1
                # очікування рахується в межах вікна виміру: і для операції, що почалась у прогріві
                # чи закінчилась після t_end (інакше голодуючий потік не дав би жодного t_wait)
                if t_acq > t0 and t_call < t_end:
                    wt = (t_acq if t_acq < t_end else t_end) - (t_call if t_call > t0 else t0)
                    wsum += wt
                    if wt > wmax:
                        wmax = wt
                if t_call >= t_end:
                    break
                if t_call >= t0:
                    cnt += 1
                    tau_a(t_ret - t_call)
        else:
            acq, rel, get = self.lock.acquire_read, self.lock.release_read, table.get
            bad = misses = 0
            samples_a = self.samples.append
            j = 0
            while True:
                k = rnd_k(keys)
                t_call = ns()
                acq()
                t_acq = ns()
                v = get(k)
                rel()
                t_ret = ns()
                if v is None:
                    misses += 1
                else:
                    if v[0] != k:
                        bad += 1
                    j += 1
                    if j % SAMPLE_EVERY == 0:
                        samples_a((k, v))
                # очікування рахується в межах вікна виміру: і для операції, що почалась у прогріві
                # чи закінчилась після t_end (інакше голодуючий потік не дав би жодного t_wait)
                if t_acq > t0 and t_call < t_end:
                    wt = (t_acq if t_acq < t_end else t_end) - (t_call if t_call > t0 else t0)
                    wsum += wt
                    if wt > wmax:
                        wmax = wt
                if t_call >= t_end:
                    break
                if t_call >= t0:
                    cnt += 1
                    tau_a(t_ret - t_call)
            self.bad, self.misses = bad, misses
        self.count = cnt
        self.wait_sum, self.wait_max = wsum, wmax


def written_ok(logs, k, v):
    kk, w, s = v
    return kk == k and w in logs and 0 <= s < len(logs[w]) and logs[w][s] == k


def check_invariants(table, logs, workers):
    out = {}
    ok, msg = table.check_structure()
    out["structure"] = msg
    # 1) get повертає одне з записаних для ключа значень
    wrong_table = sum(1 for k, v in table.items() if not written_ok(logs, k, v))
    readers = [w for w in workers if role_of(w.idx) == "r"]
    wrong_key = sum(w.bad for w in readers)
    sampled = sum(len(w.samples) for w in readers)
    wrong_sampled = sum(1 for w in readers for k, v in w.samples if not written_ok(logs, k, v))
    out.update(values_in_table_not_written=wrong_table, get_wrong_key=wrong_key,
               get_sampled=sampled, get_sampled_not_written=wrong_sampled)
    # 2) кількість ключів = кількість різних ключів, записаних хоча б раз
    distinct = set()
    for lg in logs.values():
        distinct.update(lg)
    out["size"] = table.size()
    out["distinct_written"] = len(distinct)
    out["resizes"] = table.resizes
    out["ok"] = bool(ok and wrong_table == 0 and wrong_key == 0 and wrong_sampled == 0
                     and table.size() == len(distinct))
    return out


def role_stats(ws, D):
    if not ws:
        return None
    taus = sorted(t for w in ws for t in w.tau)
    N = len(taus)
    waits_sum = sum(w.wait_sum for w in ws)
    wait_max = max(w.wait_max for w in ws)
    counts = [w.count for w in ws]
    mu = sum(counts) / len(counts)
    sigma = math.sqrt(sum((c - mu) ** 2 for c in counts) / len(counts))
    # 101 точка розподілу τ для діаграм
    qs = [pctl(taus, q / 100) for q in range(1, 100)] + [pctl(taus, 0.999), taus[-1] if taus else None]

    def us(v):  # якщо роль не виконала жодної операції (голодування), перцентилів немає
        return None if v is None else v / 1000
    return {
        "threads": len(ws), "N": N, "X": N / D, "x": N / D / len(ws),
        "p50_us": us(pctl(taus, 0.5)), "p99_us": us(pctl(taus, 0.99)),
        "p999_us": us(pctl(taus, 0.999)), "max_us": us(taus[-1] if taus else None),
        "wait_sum_s": waits_sum / 1e9, "w": waits_sum / 1e9 / (len(ws) * D),
        "wait_max_ms": wait_max / 1e6, "mu": mu, "sigma": sigma, "cv": sigma / mu if mu else None,
        "tau_q_us": [None if q is None else q / 1000 for q in qs],
    }


def env_info():
    gil = getattr(sys, "_is_gil_enabled", None)
    return {
        "python": sys.version.split()[0], "implementation": platform.python_implementation(),
        "gil_enabled": True if gil is None else gil(), "switchinterval_s": sys.getswitchinterval(),
        "logical_cpus": os.cpu_count(), "platform": platform.platform(), "machine": platform.machine(),
        "processor": platform.processor(),
    }


def run_threads(a):
    lock = make_lock(a.lock, a.policy)
    table = HashTable()
    rnd = random.Random(a.seed)
    logs = {0: prefill(table, a.keys, rnd)}
    st = {"barrier": threading.Barrier(a.threads + 1)}
    workers = [Worker(i, st, lock, table, a.keys, a.seed) for i in range(1, a.threads + 1)]
    for w in workers:
        w.start()
    now = ns()
    st["t0"] = now + int(a.warmup * 1e9)
    st["t_end"] = st["t0"] + int(a.duration * 1e9)
    st["barrier"].wait()
    for w in workers:
        w.join()
    for w in workers:
        if role_of(w.idx) == "w":
            logs[w.idx] = w.log
    wr = [w for w in workers if role_of(w.idx) == "w"]
    rd = [w for w in workers if role_of(w.idx) == "r"]
    D = a.duration
    sw, sr, sa = role_stats(wr, D), role_stats(rd, D), role_stats(workers, D)
    X = (sw["N"] if sw else 0) / D + (sr["N"] if sr else 0) / D
    res = {
        "mode": "run", "lock": lock.name, "n": a.threads, "n_w": len(wr), "n_r": len(rd), "D": D,
        "warmup": a.warmup, "keys": a.keys, "seed": a.seed,
        "threads": [{"idx": w.idx, "role": role_of(w.idx), "count": w.count} for w in workers],
        "write": sw, "read": sr, "all": sa, "X": X,
        "phi_w": sw["X"] / X if sw and X else None, "phi_r": sr["X"] / X if sr and X else None,
        "read_misses": sum(w.misses for w in rd),
        "invariants": check_invariants(table, logs, workers),
    }
    return res


def run_x1(a):
    """Один потік по черзі виконує записи і читання в пропорції n_w : n_r під тим самим замком."""
    # той самий порядок ролей, що й номери потоків 1..n: W R W R ... W (для n = 9 це 5 : 4)
    pattern = [role_of(i) for i in range(1, a.threads + 1)]
    lock = make_lock(a.lock, a.policy)
    table = HashTable()
    rnd = random.Random(a.seed)
    logs = {0: prefill(table, a.keys, rnd)}
    log = array("i")
    logs[1] = log
    rk = random.Random(a.seed * 1000 + 1).randrange
    keys = a.keys
    t0 = ns() + int(a.warmup * 1e9)
    t_end = t0 + int(a.duration * 1e9)
    nw = nr = 0
    seq = 0
    stop = False
    while not stop:
        for p in pattern:
            k = rk(keys)
            if p == "w":
                v = (k, 1, seq)
                t_call = ns()
                lock.acquire_write()
                table.put(k, v)
                lock.release_write()
                log.append(k)
                seq += 1
                if t_call >= t_end:
                    stop = True
                    break
                if t_call >= t0:
                    nw += 1
            else:
                t_call = ns()
                lock.acquire_read()
                v = table.get(k)
                lock.release_read()
                if v is not None and v[0] != k:
                    raise AssertionError("get повернув значення іншого ключа")
                if t_call >= t_end:
                    stop = True
                    break
                if t_call >= t0:
                    nr += 1
    D = a.duration
    ok, msg = table.check_structure()
    distinct = set(logs[0]) | set(log)
    return {"mode": "x1", "lock": lock.name, "pattern": "".join(pattern), "D": D, "N_w": nw, "N_r": nr,
            "X_1": (nw + nr) / D, "X_1w": nw / D, "X_1r": nr / D,
            "invariants": {"structure": msg, "size": table.size(), "distinct_written": len(distinct),
                           "ok": ok and table.size() == len(distinct)}}


def run_tcs(a):
    """Час утримання замка без конкуренції: від повернення acquire до виклику release (глобальний замок)."""
    lock = make_lock("global")
    table = HashTable()
    rnd = random.Random(a.seed)
    prefill(table, a.keys, rnd)
    rk = random.Random(a.seed * 1000 + 1).randrange
    keys = a.keys
    res = {"mode": "tcs", "lock": lock.name, "D_each": a.duration}
    # запис
    acc = n = 0
    seq = 0
    t0 = ns() + int(a.warmup * 1e9)
    t_end = t0 + int(a.duration * 1e9)
    while True:
        k = rk(keys)
        v = (k, 1, seq)
        t_call = ns()
        lock.acquire_write()
        t_acq = ns()
        table.put(k, v)
        t_rel = ns()
        lock.release_write()
        seq += 1
        if t_call >= t_end:
            break
        if t_call >= t0:
            acc += t_rel - t_acq
            n += 1
    res["t_cs_w_us"], res["N_w"] = acc / n / 1000, n
    # читання
    acc = n = 0
    t0 = ns() + int(a.warmup * 1e9)
    t_end = t0 + int(a.duration * 1e9)
    while True:
        k = rk(keys)
        t_call = ns()
        lock.acquire_read()
        t_acq = ns()
        table.get(k)
        t_rel = ns()
        lock.release_read()
        if t_call >= t_end:
            break
        if t_call >= t0:
            acc += t_rel - t_acq
            n += 1
    res["t_cs_r_us"], res["N_r"] = acc / n / 1000, n
    # поправка: два виклики perf_counter_ns підряд (накладні самого виміру)
    m = []
    for _ in range(200_000):
        a1 = ns()
        a2 = ns()
        m.append(a2 - a1)
    m.sort()
    res["timer_overhead_us"] = m[len(m) // 2] / 1000
    return res


def run_readonly(a):
    """Лише потоки читання (парні номери) на тій самій таблиці: знімок полів до і після має збігтися."""
    lock = make_lock(a.lock, a.policy)
    table = HashTable()
    rnd = random.Random(a.seed)
    logs = {0: prefill(table, a.keys, rnd)}
    before = table.fingerprint()
    st = {"barrier": threading.Barrier(a.threads // 2 + 1)}
    workers = [Worker(i, st, lock, table, a.keys, a.seed) for i in range(2, a.threads + 1, 2)]
    for w in workers:
        w.start()
    st["t0"] = ns() + int(a.warmup * 1e9)
    st["t_end"] = st["t0"] + int(a.duration * 1e9)
    st["barrier"].wait()
    for w in workers:
        w.join()
    after = table.fingerprint()
    reads = sum(w.count for w in workers)
    wrong = sum(w.bad for w in workers) + sum(1 for w in workers for k, v in w.samples if not written_ok(logs, k, v))
    return {"mode": "readonly", "lock": lock.name, "readers": len(workers), "reads": reads, "D": a.duration,
            "unchanged": before == after, "fields_compared": len(before), "wrong_values": wrong}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["run", "x1", "tcs", "readonly"], default="run")
    p.add_argument("--lock", choices=["global", "rw"], default="global")
    p.add_argument("--policy", choices=["fair", "writers", "readers"], default="fair")
    p.add_argument("--threads", type=int, default=N_THREADS)
    p.add_argument("--keys", type=int, default=KEYS)
    p.add_argument("--duration", type=float, default=10.0)
    p.add_argument("--warmup", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=12)
    p.add_argument("--out")
    a = p.parse_args()
    fn = {"run": run_threads, "x1": run_x1, "tcs": run_tcs, "readonly": run_readonly}[a.mode]
    t = time.time()
    res = fn(a)
    res["env"] = env_info()
    res["started"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    short = {k: v for k, v in res.items() if k not in ("threads", "env")}
    for r in ("write", "read", "all"):
        if isinstance(short.get(r), dict):
            short[r] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in short[r].items() if k != "tau_q_us"}
    print(json.dumps(short, ensure_ascii=False, indent=1))
    if a.mode == "run":
        print("операцій потоків:", " ".join(f"{t['idx']}{t['role']}={t['count']}" for t in res["threads"]))
    inv = res.get("invariants")
    if inv is not None and not inv["ok"]:
        sys.exit(2)
    if a.mode == "readonly" and not res["unchanged"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
