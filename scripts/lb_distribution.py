#!/usr/bin/env python3
"""Підтвердження розподілу запитів між репліками за логом балансувальника.
Рахує GET /admissions/<id> за адресою вебінстанса (поле up=)."""
import argparse
import collections
import re
import subprocess

from common import RESULTS, ROOT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="напр. 20m (аргумент docker compose logs --since)")
    a = ap.parse_args()
    cmd = ["docker", "compose", "logs", "--no-log-prefix", "lb"]
    if a.since:
        cmd += ["--since", a.since]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    cnt = collections.Counter()
    for line in out.splitlines():
        m = re.search(r'"GET /admissions/\d+[^"]*" \d+ .*up=(\S+)', line)
        if m:
            cnt[m[1]] += 1
    total = sum(cnt.values())
    lines = ["| Вебінстанс (ip:port) | Запитів | Частка |", "|---|---|---|"]
    lines += [f"| {k} | {v} | {100 * v / total:.1f}% |" for k, v in sorted(cnt.items())]
    text = "\n".join(lines) + f"\n\nУсього: {total}\n"
    (RESULTS / "distribution.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
