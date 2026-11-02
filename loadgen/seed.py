#!/usr/bin/env python3
"""Заносить у базу 100 записів (якщо їх менше) і зберігає їхні id у results/ids.json."""
import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from common import RESULTS, call  # noqa: E402

DEPARTMENTS = ["кардіологія", "хірургія", "неврологія", "терапія", "педіатрія"]
OUTCOMES = ["одужання", "покращення", "переведення", "летальний наслідок"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--count", type=int, default=100)
    a = ap.parse_args()
    rnd = random.Random(12)
    _, _, lst = call("GET", f"{a.base_url}/admissions?limit=1")
    for k in range(max(0, a.count - lst["total"])):
        adm = date(2025, 1, 1) + timedelta(days=rnd.randint(0, 300))
        discharged = rnd.random() < 0.8
        body = {
            "patient_code": f"P{rnd.randint(100000, 999999)}",
            "department": rnd.choice(DEPARTMENTS),
            "room": str(rnd.randint(1, 60)),
            "admission_date": adm.isoformat(),
            "discharge_date": (adm + timedelta(days=rnd.randint(0, 30))).isoformat() if discharged else None,
            "outcome": rnd.choice(OUTCOMES) if discharged else "на лікуванні",
        }
        st, _, resp = call("POST", f"{a.base_url}/admissions", body)
        assert st == 201, (st, resp)
    _, _, page = call("GET", f"{a.base_url}/admissions?limit=100")
    ids = [x["id"] for x in page["items"]]
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "ids.json").write_text(json.dumps(ids))
    print(f"seed: {len(ids)} id збережено у results/ids.json")


if __name__ == "__main__":
    main()
