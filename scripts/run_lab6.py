#!/usr/bin/env python3
"""Прогони лабораторної 6: кожен вимір окремим запуском torchrun (або одного процесу), JSON у results/lab6/.

  python scripts/run_lab6.py            усі кроки, 3 повтори вимірів швидкості (~15 хв)
  python scripts/run_lab6.py --quick    перевірка стенду: 1 повтор (results/lab6/quick/)

Порядок: стенд (таблиця 1) і дані; крок 1 і 2 — якість в одному процесі, перевірка еквівалентності, якість з DDP;
виміри швидкості (кроки 1-4) повторами, у кожному повторі всі конфігурації по черзі, щоб дрейф машини однаково
впливав на всі; крок 4 — якість за різних глобальних батчів; крок 5 — навчання з відмовою процесу і перезапуском.
"""
import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN = ROOT / "ddp" / "train.py"
sys.path.insert(0, str(ROOT / "ddp"))
import common as cm  # noqa: E402

V = cm.VARIANT
P_MAX, B = V["P_max"], V["B"]
b0 = B // P_MAX                      # батч процесу у weak scaling


def physical_cores():
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return n
    except ImportError:
        pass
    if platform.system() == "Windows":
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum"],
                           capture_output=True, text=True)
        if r.stdout.strip().isdigit():
            return int(r.stdout.strip())
    try:
        out = subprocess.run(["lscpu", "-p=CORE,SOCKET"], capture_output=True, text=True).stdout
        cores = {ln for ln in out.splitlines() if ln and not ln.startswith("#")}
        if cores:
            return len(cores)
    except FileNotFoundError:
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def cpu_name():
    if platform.system() == "Windows":
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                           capture_output=True, text=True)
        if r.stdout.strip():
            return r.stdout.strip()
    try:
        for ln in open("/proc/cpuinfo", encoding="utf-8"):
            if ln.startswith("model name"):
                return ln.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def mem_gb():
    try:
        import psutil
        return psutil.virtual_memory().total / 2 ** 30
    except ImportError:
        pass
    if platform.system() == "Windows":
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 2 ** 30
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30


def clean_dir(d):
    """Порожня тека d. Саму теку не видаляємо: у теці OneDrive rmdir може дати Access is denied."""
    d.mkdir(parents=True, exist_ok=True)
    for x in d.iterdir():
        shutil.rmtree(x) if x.is_dir() else x.unlink()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--quick", action="store_true")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--only", default=None, help="speed | quality | fault (для повторного прогону частини)")
    a = p.parse_args()
    out = ROOT / "results" / "lab6" / ("quick" if a.quick else "")
    out.mkdir(parents=True, exist_ok=True)
    reps = 1 if a.quick else a.repeats
    log = open(out / "log_run.txt", "a", encoding="utf-8")

    import torch
    c_phys = physical_cores()
    n_th = max(1, c_phys // P_MAX)
    env = dict(os.environ, OMP_NUM_THREADS=str(n_th), PYTHONIOENCODING="utf-8")
    if platform.system() == "Windows":
        env["USE_LIBUV"] = "0"        # збірка PyTorch для Windows без libuv, інакше TCPStore torchrun не стартує
    model = cm.make_model()
    n_params = sum(q.numel() for q in model.parameters())
    stand = dict(cpu=cpu_name(), c_phys=c_phys, c_logical=os.cpu_count(), mem_gb=round(mem_gb(), 1),
                 os=platform.platform(), python=platform.python_version(), torch=torch.__version__, n_th=n_th,
                 dataset=f"{V['dataset']} ({cm.NAME})", K=cm.K, C=cm.C, widths=[cm.C1, cm.C2, cm.H],
                 n_params=n_params, n_params_formula=cm.n_params_formula(cm.C, cm.C1, cm.C2, cm.H, cm.K),
                 M_MiB=4 * n_params / 2 ** 20, N=cm.N_TRAIN, B=B, P_max=P_MAX, variant=V, host=platform.node())
    (out / "stand.json").write_text(json.dumps(stand, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stand, ensure_ascii=False), flush=True)
    cm.ensure_data()

    def run(name, P, *args, torchrun_args=None, check=True):
        o = out / f"{name}.json"
        if P == 0:              # один процес без torch.distributed
            cmd = [sys.executable, str(TRAIN)]
        else:
            cmd = [sys.executable, *(["-m", "torch.distributed.run"] if platform.system() != "Windows"
                                     else [str(ROOT / "ddp" / "torchrun_win.py")]),
                   *(torchrun_args or ["--standalone"]), f"--nproc_per_node={P}", str(TRAIN)]
        cmd += [*args, "--threads", str(n_th), "--out", str(o)]
        line = f"[{time.strftime('%H:%M:%S')}] {name}: {' '.join(cmd[1:])}"
        print(line, flush=True)
        t = time.time()
        # Windows: torchrun зупиняє процеси через CTRL_C_EVENT, який отримує вся консоль (і цей скрипт);
        # окрема прихована консоль для кожного запуску ізолює його
        flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                           cwd=ROOT, creationflags=flags)
        log.write(line + "\n" + r.stdout + r.stderr + f"код {r.returncode}, {time.time() - t:.1f} с\n\n")
        log.flush()
        brief = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        print("  " + (brief[-1] if brief else "") + f"  (код {r.returncode}, {time.time() - t:.1f} с)", flush=True)
        if r.returncode != 0 and check:
            print(r.stderr[-3000:])
            print(f"  !!! {name}: код {r.returncode}, див. log_run.txt")
        return r.returncode

    t_all = time.time()
    if a.only in (None, "quality"):
        # кроки 1-2: якість одного процесу без torch.distributed, перевірка ḡ = g, якість з DDP на P_max
        run("q_single_B", 0, "--task", "quality", "--sync", "none", "--batch", str(B))
        run("equiv", P_MAX, "--task", "equiv", "--sync", "manual", "--batch", str(B))
        run("q_ddp_B_lr0", P_MAX, "--task", "quality", "--sync", "ddp", "--batch", str(B))
        # крок 4: P = 1 з батчем b і η₀; P_max з η_P = η₀ · P_max (B₀ = b)
        run("q_ddp1_b_lr0", 1, "--task", "quality", "--sync", "ddp", "--batch", str(b0))
        run("q_ddp_B_lrP", P_MAX, "--task", "quality", "--sync", "ddp", "--batch", str(B),
            "--lr", str(cm.LR0 * B / b0))

    if a.only in (None, "speed"):
        configs = [("sp_single_B", 0, ["--sync", "none", "--batch", str(B)]),
                   (f"sp_manual_P{P_MAX}", P_MAX, ["--sync", "manual", "--batch", str(B)])]
        for P in range(1, P_MAX + 1):                     # крок 3: strong, B = const
            configs.append((f"sp_strong_ddp_P{P}", P, ["--sync", "ddp", "--batch", str(B)]))
            configs.append((f"sp_strong_nosync_P{P}", P, ["--sync", "ddp_nosync", "--batch", str(B)]))
        for P in range(1, P_MAX):                         # крок 4: weak, b = const (P_max з таблиці 3)
            configs.append((f"sp_weak_ddp_P{P}", P, ["--sync", "ddp", "--batch", str(P * b0)]))
        for k in range(1, reps + 1):
            for name, P, args in configs:
                run(f"{name}_r{k}", P, "--task", "speed", *args)

    if a.only in (None, "fault"):
        # крок 5: контрольні точки кожні K_c кроків, відмова процесу P_max - 1 на початку кроку s_f
        wd = out / "fault"
        clean_dir(wd)
        static = ["--nnodes=1", "--max-restarts=1", "--rdzv-backend=static", "--master-addr=127.0.0.1",
                  "--master-port=29500", "--node-rank=0"]
        run("fault/result", P_MAX, "--task", "quality", "--sync", "ddp", "--batch", str(B),
            "--ckpt-every", str(V["K_c"]), "--fail-step", str(V["s_f"]), "--workdir", str(wd), torchrun_args=static)
        # контроль: те саме з ручною синхронізацією, і її прогін без відмови (чи залежить збіг втрати від DDP)
        wm = out / "fault_manual"
        clean_dir(wm)
        run("fault_manual/result", P_MAX, "--task", "quality", "--sync", "manual", "--batch", str(B),
            "--ckpt-every", str(V["K_c"]), "--fail-step", str(V["s_f"]), "--workdir", str(wm), torchrun_args=static)
        run("q_manual_B_lr0", P_MAX, "--task", "quality", "--sync", "manual", "--batch", str(B))
    print(f"готово за {(time.time() - t_all) / 60:.1f} хв; далі: python scripts/analyze_lab6.py"
          + (" --quick" if a.quick else ""))


if __name__ == "__main__":
    main()
