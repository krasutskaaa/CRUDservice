#!/usr/bin/env python3
"""Етап 9: повний цикл перевірки, журнал у results/verify_log.md.

  python scripts/verify_cycle.py                 # CRUD-цикл
  python scripts/verify_cycle.py --persistence   # + db down/up, down -v, 503 при недоступній базі
"""
import argparse
import json
import sys

from common import RESULTS, call, compose, wait_health

BASE = "http://127.0.0.1:8080"
LOG, FAILS = [], []

VALID = {"patient_code": "P000001", "department": "кардіологія", "room": "12",
         "admission_date": "2025-03-01", "discharge_date": None, "outcome": "на лікуванні"}


def step(title, method, path, body=None, expect=200, check=None):
    st, hd, resp = call(method, BASE + path, body)
    ok = st == expect and (check(resp) if check else True)
    LOG.append(f"### {len(LOG) + 1}. {title}\n\n`{method} {path}`"
               + (f"\n\nТіло запиту: `{json.dumps(body, ensure_ascii=False)}`" if body is not None else "")
               + f"\n\nОчікувано: **{expect}**, отримано: **{st}** {'✅' if ok else '❌'}\n\n"
               + f"```json\n{json.dumps(resp, ensure_ascii=False, indent=2) if resp is not None else '(порожнє тіло)'}\n```\n")
    if not ok:
        FAILS.append(title)
    return resp


def crud_cycle():
    step("Готовність сервісу", "GET", "/healthz", expect=200)
    rec = step("Створення запису", "POST", "/admissions", VALID, 201, lambda r: isinstance(r.get("id"), int))
    rid = rec["id"]
    step("Невалідне створення: виписка раніше надходження", "POST", "/admissions",
         {**VALID, "discharge_date": "2025-02-01"}, 400, lambda r: r["details"][0]["field"] == "discharge_date")
    step("Невалідне створення: бракує поля", "POST", "/admissions",
         {k: v for k, v in VALID.items() if k != "room"}, 400, lambda r: r["details"][0]["field"] == "room")
    step("Невалідне створення: клієнт передає id", "POST", "/admissions", {**VALID, "id": 5}, 400)
    step("Невалідне створення: невідомий результат", "POST", "/admissions", {**VALID, "outcome": "?"}, 400)
    step("Перелік із фільтром за відділенням", "GET", "/admissions?department=кардіологія", expect=200,
         check=lambda r: any(x["id"] == rid for x in r["items"]) and "total" in r)
    step("Перелік без фільтра (пагінація за замовчуванням)", "GET", "/admissions", expect=200,
         check=lambda r: r["limit"] == 20 and isinstance(r["items"], list))
    step("Перелік: limit поза межами", "GET", "/admissions?limit=1000", expect=400)
    step("Читання за ідентифікатором", "GET", f"/admissions/{rid}", expect=200, check=lambda r: r["id"] == rid)
    step("Читання неіснуючого", "GET", "/admissions/999999999", expect=404)
    step("Заміна запису", "PUT", f"/admissions/{rid}",
         {**VALID, "discharge_date": "2025-03-10", "outcome": "одужання", "room": "14"}, 200,
         lambda r: r["outcome"] == "одужання" and r["room"] == "14")
    step("Невалідна заміна", "PUT", f"/admissions/{rid}", {**VALID, "discharge_date": "2024-01-01"}, 400)
    step("Заміна неіснуючого", "PUT", "/admissions/999999999", VALID, 404)
    step("Видалення", "DELETE", f"/admissions/{rid}", expect=204)
    step("Повторне видалення", "DELETE", f"/admissions/{rid}", expect=404)
    step("Читання видаленого", "GET", f"/admissions/{rid}", expect=404)


def persistence():
    rec = step("[persist] Створення запису перед down", "POST", "/admissions", VALID, 201)
    rid = rec["id"]
    compose("down")
    compose("up", "-d")
    wait_health(BASE)
    step("[persist] Запис на місці після down і up", "GET", f"/admissions/{rid}", expect=200)
    compose("stop", "db")
    step("[503] /healthz при зупиненій базі", "GET", "/healthz", expect=503)
    compose("start", "db")
    wait_health(BASE)
    step("[503] /healthz після повернення бази", "GET", "/healthz", expect=200)
    compose("down", "-v")
    compose("up", "-d")
    wait_health(BASE)
    step("[persist] Після down -v база порожня", "GET", "/admissions", expect=200, check=lambda r: r["total"] == 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persistence", action="store_true")
    a = ap.parse_args()
    wait_health(BASE)
    crud_cycle()
    if a.persistence:
        persistence()
    (RESULTS / "verify_log.md").write_text("# Журнал перевірки\n\n" + "\n".join(LOG))
    print(f"кроків: {len(LOG)}, збоїв: {len(FAILS)}", *FAILS, sep="\n")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
