#!/usr/bin/env python3
"""Час від `docker compose up` до першої відповіді 200 на /healthz.
Образи вже зібрані (виконайте `docker compose build` до цього), томи порожні (down -v).
Три повтори, у звіт іде медіана; а також розмір образу."""
import json
import statistics as st
import subprocess
import time

from common import RESULTS, ROOT, call, compose, wait_health

BASE = "http://127.0.0.1:8080"


def main():
    times = []
    for i in range(3):
        compose("down", "-v")
        t = time.perf_counter()
        compose("up", "-d")
        wait_health(BASE)
        times.append(time.perf_counter() - t)
        print(f"повтор {i + 1}: {times[-1]:.2f} с")
    size = int(subprocess.run(["docker", "image", "inspect", "admissions-web:lab1", "--format", "{{.Size}}"],
                              capture_output=True, text=True, check=True).stdout)
    res = {"startup_s": times, "median_s": st.median(times), "image_size_mb": round(size / 1e6, 1),
           "image_size_mib": round(size / 2**20, 1)}
    (RESULTS / "startup.json").write_text(json.dumps(res, indent=2))
    print(res)


if __name__ == "__main__":
    main()
