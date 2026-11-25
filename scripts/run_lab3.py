#!/usr/bin/env python3
"""Оркестратор прогонів лабораторної 3 (k3d, варіант 12: R = 3).

  python scripts/run_lab3.py db-persist          # крок 3: дані бази після kubectl delete pod
  python scripts/run_lab3.py crud                # крок 4: CRUD через localhost:8080, X-Pod, пошук пароля
  python scripts/run_lab3.py monitoring          # крок 5: ряди в Prometheus і kubectl top pods
  python scripts/run_lab3.py steps --replicas N [--run K]   # крок 7: ступінчастий прогін на N репліках
  python scripts/run_lab3.py profile --mode fixed           # крок 10: профіль на фіксованих R репліках
  python scripts/run_lab3.py profile --mode hpa             # крок 11: той самий профіль з HPA
  python scripts/run_lab3.py d2                             # крок 13: Д2 (нестача зʼєднань з базою)
  python scripts/run_lab3.py restore                        # max_connections = 100, DB_POOL_SIZE = 5

Результати: results/lab3/. Аналіз: python scripts/analyze_lab3.py
"""
import argparse
import csv
import json
import os
import re
import statistics as st
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prom  # noqa: E402
import prom3  # noqa: E402
from common import RESULTS, ROOT, call, wait_health  # noqa: E402
from k8s_common import (IMAGE, OUT, hpa_delete, kjson, kubectl, ready_web, scale,  # noqa: E402
                        web_pods)

R = 3
POOL = 5
CONNS = 8 * R            # модель зʼєднань генератора (крок 6)
REQS_PER_CONN = 100
BASE = "http://127.0.0.1:8080"
PROM = f"http://127.0.0.1:{os.environ.get('PROM_PORT', '9090')}"
STEPLOAD = str(ROOT / "loadgen" / "stepload.py")
IDS = OUT / "ids.json"
STEP_ARGS = ["--step-duration", "30", "--transient", "10", "--window", "20"]
WINDOW = 20


def save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def gen_limit():
    g = RESULTS / "lab2" / "G.json"
    return json.loads(g.read_text(encoding="utf-8"))["G"] if g.exists() else 0


def seed():
    subprocess.run([sys.executable, str(ROOT / "loadgen" / "seed.py"), "--base-url", BASE,
                    "--ids-file", str(IDS)], check=True)


def prepare(n):
    """Без HPA рівно n готових подів web, n цілей web у Prometheus."""
    hpa_delete()
    waited = scale(n)
    prom.wait_targets(PROM, n)
    wait_health(BASE)
    return waited


def stepload(prefix, *extra):
    cmd = [sys.executable, STEPLOAD, "--base-url", BASE, "--ids-file", str(IDS),
           "--conns", str(CONNS), "--reqs-per-conn", str(REQS_PER_CONN), "--out-prefix", str(prefix), *extra]
    subprocess.run(cmd, check=True)
    return json.loads((OUT / (prefix.name + "_steps.json")).read_text(encoding="utf-8"))


def db_log_since(t):
    since = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = kubectl("logs", "db-0", f"--since-time={since}", capture=True, check=False)
    return r.stdout


def pg_clients():
    """Зʼєднання клієнтів з базою за адресами (pg_stat_activity) → {pod або адреса: кількість}."""
    q = ("SELECT client_addr, count(*) FROM pg_stat_activity "
         "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() GROUP BY 1")
    r = kubectl("exec", "db-0", "--", "sh", "-c", f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F, -c "{q}"',
                capture=True, check=False)
    ip2pod = {p["status"].get("podIP"): p["metadata"]["name"] for p in kjson("get", "pods")["items"]}
    out = {}
    if r.returncode != 0:  # коли всі слоти зайняті, psql теж не підключиться
        err = [x for x in (r.stderr or "").strip().splitlines() if x.strip()]
        fatal = [x for x in err if "FATAL" in x]
        return {"psql_error": (fatal or err or [f"код {r.returncode}"])[-1].strip()}
    for line in r.stdout.splitlines():
        if "," in line:
            addr, n = line.rsplit(",", 1)
            out[ip2pod.get(addr, addr or "local")] = int(n)
    return out


def pg_exporter_total():
    """Кількість клієнтських зʼєднань за postgres-exporter (без його власного), останнє значення."""
    try:
        r = prom3.instant(PROM, "sum(pg_connections_count)", time.time())
        return int(float(r[0]["value"][1])) if r else None
    except Exception:  # noqa: BLE001
        return None


# ---------- крок 3 ----------
def cmd_db_persist(_):
    wait_health(BASE)
    body = {"patient_code": "P-PERSIST", "department": "терапія", "room": "3",
            "admission_date": "2025-05-01", "discharge_date": None, "outcome": "на лікуванні"}
    st_c, _, rec = call("POST", BASE + "/admissions", body)
    old_uid = kjson("get", "pod", "db-0")["metadata"]["uid"]
    t = time.time()
    kubectl("delete", "pod", "db-0", "--wait=true")
    kubectl("rollout", "status", "statefulset/db", "--timeout=300s")
    t_db = time.time() - t
    wait_health(BASE, timeout=180)
    t_ok = time.time() - t
    new_uid = kjson("get", "pod", "db-0")["metadata"]["uid"]
    st_r, _, got = call("GET", f"{BASE}/admissions/{rec['id']}")
    pvc = kjson("get", "pvc", "data-db-0")
    res = {"create_status": st_c, "record": rec, "old_pod_uid": old_uid, "new_pod_uid": new_uid,
           "pod_recreated": old_uid != new_uid, "db_ready_after_s": round(t_db, 1),
           "web_healthz_ok_after_s": round(t_ok, 1), "read_status": st_r, "read_back": got,
           "same_record": got == rec, "pvc": pvc["metadata"]["name"], "pv": pvc["spec"].get("volumeName")}
    save("db_persist.json", res)
    call("DELETE", f"{BASE}/admissions/{rec['id']}")
    print(json.dumps(res, indent=2, ensure_ascii=False))


# ---------- крок 4 ----------
def password_checks():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    pw = env["POSTGRES_PASSWORD"]
    cms = kubectl("get", "configmap", "-o", "yaml", capture=True).stdout
    img = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, text=True).stdout
    hist = subprocess.run(["docker", "history", "--no-trunc", IMAGE], capture_output=True, text=True).stdout
    tracked = subprocess.run(["git", "grep", "-l", "-F", pw], cwd=ROOT, capture_output=True, text=True).stdout
    dep = kubectl("get", "deployment", "web", "-o", "yaml", capture=True).stdout
    return {"in_configmaps": pw in cms, "in_image_inspect": pw in img, "in_image_history": pw in hist,
            "in_git_tracked_files": bool(tracked.strip()), "in_deployment_spec": pw in dep,
            "deployment_uses_secretKeyRef": "secretKeyRef" in dep}


def cmd_crud(a):
    prepare(a.replicas)
    v = {"patient_code": "P000312", "department": "кардіологія", "room": "12",
         "admission_date": "2025-03-01", "discharge_date": None, "outcome": "на лікуванні"}
    log = []

    def do(title, method, path, body=None):
        s, h, r = call(method, BASE + path, body)
        hl = {k.lower(): v for k, v in h.items()}
        log.append({"title": title, "method": method, "path": path, "status": s, "x_pod": hl.get("x-pod")})
        return s, r

    s, rec = do("Створення", "POST", "/admissions", v)
    rid = rec["id"]
    do("Читання", "GET", f"/admissions/{rid}")
    do("Заміна", "PUT", f"/admissions/{rid}", {**v, "discharge_date": "2025-03-09", "outcome": "одужання"})
    do("Читання після заміни", "GET", f"/admissions/{rid}")
    do("Видалення", "DELETE", f"/admissions/{rid}")
    do("Читання видаленого", "GET", f"/admissions/{rid}")
    pods = web_pods()
    res = {"replicas": a.replicas, "pods": pods, "log": log, "password": password_checks()}
    save("crud.json", res)
    md = ["| крок | запит | код | X-Pod |", "|---|---|---|---|"]
    md += [f"| {x['title']} | `{x['method']} {x['path']}` | {x['status']} | `{x['x_pod']}` |" for x in log]
    print("\n".join(md))
    print(json.dumps(res["password"], indent=2))


# ---------- крок 5 ----------
def cmd_monitoring(a):
    prepare(a.replicas)
    seed()
    # невелике навантаження, щоб зʼявились ряди запитів і зріс лічильник CFS
    subprocess.run([sys.executable, STEPLOAD, "--base-url", BASE, "--ids-file", str(IDS), "--warmup", "0",
                    "--start-rate", "100", "--step", "0", "--step-duration", "30", "--max-steps", "1",
                    "--conns", str(CONNS), "--out-prefix", str(OUT / "monitoring_load")], check=True)
    time.sleep(20)
    q = {
        "http_requests_total by pod": f'sum by (pod) (http_requests_total{{job="web",{prom.SCENARIO}}})',
        "container_cpu_cfs_periods_total web": f'sum by (pod) (container_cpu_cfs_periods_total{{{prom3.WEB_C}}})',
        "container_cpu_cfs_throttled_periods_total web":
            f'sum by (pod) (container_cpu_cfs_throttled_periods_total{{{prom3.WEB_C}}})',
        "container_cpu_usage_seconds_total web": f'sum by (pod) (container_cpu_usage_seconds_total{{{prom3.WEB_C}}})',
        "kube_deployment_status_replicas_ready": prom3.RANGE["ready"],
        "pg_connections_count": "sum by (state) (pg_connections_count)",
    }
    res = {"pods": web_pods(), "queries": {}}
    for name, expr in q.items():
        r = prom3.instant(PROM, expr, time.time())
        res["queries"][name] = {"query": expr, "result": [(x["metric"], x["value"][1]) for x in r]}
    t_local = time.time()
    t_prom = float(prom._get(PROM, "/api/v1/query", {"query": "time()"})["data"]["result"][0])
    res["clock_skew_s"] = round(t_prom - t_local, 2)  # ряди Prometheus зіставляються з часом генератора
    top = kubectl("top", "pods", check=False, capture=True)
    res["kubectl_top_pods"] = top.stdout + top.stderr
    res["targets"] = [(t["labels"].get("job"), t["labels"].get("instance"), t["health"])
                      for t in prom._get(PROM, "/api/v1/targets")["data"]["activeTargets"]]
    save("monitoring_check.json", res)
    for name, v in res["queries"].items():
        print(name, "→", v["result"])
    print(res["kubectl_top_pods"])


# ---------- крок 7 ----------
def cmd_steps(a):
    n, k = a.replicas, a.run
    label = f"n{n}_r{k}"
    waited = prepare(n)
    seed()
    pods = web_pods()
    t_start = time.time()
    res = stepload(OUT / label, "--instances", str(n), "--gen-limit", str(gen_limit()), *STEP_ARGS)
    time.sleep(12)  # cAdvisor оновлює лічильники раз на 10-15 с
    per_step = {s["step"]: prom3.collect_window(PROM, s["window_epoch"][1], WINDOW) for s in res["steps"]}
    save(f"prom_steps_{label}.json", per_step)
    save(f"prom_{label}.json", prom3.collect_range(PROM, t_start - 5, time.time(), step=5))
    save(f"raw_{label}.json", prom3.collect_raw(PROM, t_start - 5, time.time()))
    save(f"pods_{label}.json", {"pods": pods, "scale_wait_s": waited})
    print("готово:", OUT / f"{label}_steps.json")


# ---------- кроки 10-11 ----------
def x_values():
    xs = {}
    for n in range(1, R + 1):
        vals = []
        for k in range(1, 4):
            p = OUT / f"n{n}_r{k}_steps.json"
            if p.exists():
                vals.append(json.loads(p.read_text(encoding="utf-8"))["X"]["value"])
        if vals:
            xs[n] = st.fmean(vals)
    return xs


def wait_hpa_one(hold=60, timeout=900):
    """Чекає, поки HPA показує одну репліку (поточну і бажану) hold секунд поспіль."""
    t, since = time.time(), None
    while time.time() - t < timeout:
        try:
            s = kjson("get", "hpa", "web").get("status", {})
            ok = s.get("currentReplicas") == 1 and s.get("desiredReplicas") == 1 and ready_web() == (1, 1)
        except Exception:  # noqa: BLE001
            ok = False
        since = (since or time.time()) if ok else None
        if since and time.time() - since >= hold:
            return time.time() - t
        time.sleep(2)
    raise TimeoutError("HPA не стабілізувався на одній репліці")


def cmd_profile(a):
    xs = x_values()
    if 1 not in xs or R not in xs:
        sys.exit("спершу виконайте крок 7 (потрібні X(1) і X(R))")
    low = round(0.3 * xs[1])
    jump = round(min(2.5 * xs[1], 0.9 * xs[R]))
    profile = f"30:{low},60:{jump},90:{low}"
    print(f"X(1) = {xs[1]:.0f}, X({R}) = {xs[R]:.0f}; профіль {profile}")
    if a.mode == "fixed":
        prepare(R)
        waited = None
    else:
        hpa_delete()
        kubectl("apply", "-f", ROOT / "k8s" / "hpa.yaml")
        waited = wait_hpa_one()
        prom.wait_targets(PROM, 1)
    seed()
    t_start = time.time()
    res = stepload(OUT / f"profile_{a.mode}", "--profile", profile, "--instances", str(R))
    time.sleep(12)
    t0 = res["meta"]["t0"]
    save(f"prom_profile_{a.mode}.json", prom3.collect_range(PROM, t0 - 10, time.time(), step=1))
    save(f"raw_profile_{a.mode}.json", prom3.collect_raw(PROM, t0 - 40, time.time()))
    meta = {"mode": a.mode, "profile": profile, "low": low, "jump": jump, "X": xs, "t0": t0,
            "t_start": t_start, "hpa_wait_s": waited, "pods_after": web_pods()}
    if a.mode == "hpa":
        meta["hpa_describe"] = kubectl("describe", "hpa", "web", capture=True, check=False).stdout
    save(f"profile_{a.mode}_meta.json", meta)
    print("готово:", OUT / f"profile_{a.mode}_steps.json")


# ---------- крок 13: Д2 ----------
def set_max_connections(n):
    kubectl("patch", "configmap", "db-config", "--type", "merge", "-p", json.dumps({"data": {"MAX_CONNECTIONS": str(n)}}))
    kubectl("delete", "pod", "db-0", "--wait=true")  # перезапуск бази: StatefulSet створює под заново
    kubectl("rollout", "status", "statefulset/db", "--timeout=300s")
    r = kubectl("exec", "db-0", "--", "sh", "-c", 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SHOW max_connections"',
                capture=True)
    return int(r.stdout.strip())


def set_pool(n, timeout=120):
    """Новий DB_POOL_SIZE через перезапуск подів. Повертає, чи завершився rolling update сам.
    Якщо всі слоти бази зайняті старими подами, новий под не отримує зʼєднання, не стає готовим
    і rollout зависає; тоді поди перезапускаються через scale 0 → попередня кількість."""
    kubectl("patch", "configmap", "web-config", "--type", "merge", "-p", json.dumps({"data": {"DB_POOL_SIZE": str(n)}}))
    replicas = kjson("get", "deployment", "web")["spec"].get("replicas") or 1
    kubectl("rollout", "restart", "deployment/web")
    r = kubectl("rollout", "status", "deployment/web", f"--timeout={timeout}s", check=False, capture=True)
    if r.returncode == 0:
        return {"rollout_ok": True}
    kubectl("scale", "deployment/web", "--replicas=0")
    t = time.time()
    while ready_web()[1] and time.time() - t < 120:
        time.sleep(1)
    scale(replicas)
    return {"rollout_ok": False, "rollout_status": (r.stdout + r.stderr).strip().splitlines()[-1:],
            "fallback": "scale 0 → " + str(replicas)}


def count_db_errors(text):
    return len(re.findall(r"too many clients|remaining connection slots are reserved", text))


def cmd_d2(a):
    max_conn = 2 * POOL + 5
    fixed_pool = (max_conn - 4) // R
    xs = x_values()
    out = {"max_connections": max_conn, "pool_before": POOL, "pool_after": fixed_pool, "scan": []}
    hpa_delete()
    scale(1)
    out["max_connections_shown"] = set_max_connections(max_conn)
    wait_health(BASE, timeout=180)
    seed()
    # 1) з якої кількості реплік зʼявляються помилки: N = 1..R, сталий потік 0,7·X(N), 40 с
    for n in range(1, R + 1):
        t = time.time()
        prepare(n)
        time.sleep(10)  # пули нових подів встигають відкрити зʼєднання
        clients = pg_clients()
        exp_conn = pg_exporter_total()
        rate = round(0.7 * xs.get(n, xs.get(1, 300)))
        res = stepload(OUT / f"d2_scan_n{n}", "--warmup", "0", "--start-rate", str(rate), "--step", "0",
                       "--step-duration", "40", "--max-steps", "1", "--transient", "0", "--window", "40",
                       "--instances", str(n))
        log = db_log_since(t)
        s = res["steps"][0]
        sec = list(csv.DictReader(open(OUT / f"d2_scan_n{n}_seconds.csv", encoding="utf-8")))
        out["scan"].append({"N": n, "rate": rate, "achieved": s["achieved_mean"], "non2xx_share": s["non2xx_share"],
                            "non2xx_total": sum(int(r["non2xx"]) for r in sec),
                            "conn_errors": sum(int(r["conn_errors"]) for r in sec), "client_p99_ms": s["client_p99_ms"],
                            "db_clients": clients, "db_clients_total": sum(v for v in clients.values() if isinstance(v, int)),
                            "pg_exporter_connections": exp_conn,
                            "db_log_errors": count_db_errors(log),
                            "db_log_sample": [x for x in log.splitlines() if "FATAL" in x][:5],
                            "ready_pods": ready_web()[0]})
        print(json.dumps(out["scan"][-1], ensure_ascii=False))
    # 2) R реплік до виправлення: ступінчастий прогін
    for label, pool in (("before", None), ("after", fixed_pool)):
        if pool:
            out["pool_change"] = set_pool(pool)
            save("d2.json", out)
        t = time.time()
        prepare(R)
        time.sleep(10)
        clients = pg_clients()
        exp_conn = pg_exporter_total()
        t_start = time.time()
        res = stepload(OUT / f"d2_{label}", "--instances", str(R), "--gen-limit", str(gen_limit()), *STEP_ARGS)
        time.sleep(12)
        log = db_log_since(t)
        save(f"prom_d2_{label}.json", prom3.collect_range(PROM, t_start - 5, time.time(), step=5))
        sec = list(csv.DictReader(open(OUT / f"d2_{label}_seconds.csv", encoding="utf-8")))
        out[label] = {"pool": pool or POOL, "X": res["X"], "db_clients": clients,
                      "db_clients_total": sum(v for v in clients.values() if isinstance(v, int)),
                      "pg_exporter_connections": exp_conn, "db_log_errors": count_db_errors(log),
                      "db_log_sample": [x for x in log.splitlines() if "FATAL" in x][:5],
                      "non2xx_total": sum(int(r["non2xx"]) for r in sec),
                      "conn_errors_total": sum(int(r["conn_errors"]) for r in sec),
                      # до кінця сходинки X(R) включно (далі відмови вже через перевантаження)
                      "non2xx_to_X": sum(int(r["non2xx"]) for r in sec if int(float(r["step"])) <= res["X"]["step"]),
                      "conn_errors_to_X": sum(int(r["conn_errors"]) for r in sec if int(float(r["step"])) <= res["X"]["step"])}
        save("d2.json", out)
    print(json.dumps({k: v for k, v in out.items() if k != "scan"}, indent=2, ensure_ascii=False))


def cmd_restore(_):
    hpa_delete()
    print("max_connections =", set_max_connections(100))  # спершу база, щоб нові поди отримали зʼєднання
    print(set_pool(POOL))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("db-persist")
    p = sub.add_parser("crud")
    p.add_argument("--replicas", type=int, default=R)
    p = sub.add_parser("monitoring")
    p.add_argument("--replicas", type=int, default=R)
    p = sub.add_parser("steps")
    p.add_argument("--replicas", type=int, required=True)
    p.add_argument("--run", type=int, default=1)
    p = sub.add_parser("profile")
    p.add_argument("--mode", choices=["fixed", "hpa"], required=True)
    sub.add_parser("d2")
    sub.add_parser("restore")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    {"db-persist": cmd_db_persist, "crud": cmd_crud, "monitoring": cmd_monitoring, "steps": cmd_steps,
     "profile": cmd_profile, "d2": cmd_d2, "restore": cmd_restore}[a.cmd](a)


if __name__ == "__main__":
    if not sys.flags.utf8_mode:
        sys.exit(subprocess.run([sys.executable, "-X", "utf8", *sys.argv]).returncode)
    main()
