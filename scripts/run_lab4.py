#!/usr/bin/env python3
"""Оркестратор прогонів лабораторної 4 (кластер з лабораторної 3, варіант 12:
q_lo/q_hi = 0,01/0,99, T_poll = 50 мс, L = 15 с, сценарій Ф5, додаткова вимога Д4, R = 3).

  python scripts/run_lab4.py step1        крок 1: ендпоінти задач і вимірювачі tasks_* у /metrics
  python scripts/run_lab4.py step2        крок 2: 20 задач на одному воркері, t_io і t_cpu
  python scripts/run_lab4.py step3        крок 3: наївне захоплення, D при T_poll = 50 і 1 мс
  python scripts/run_lab4.py step4        крок 4: FOR UPDATE SKIP LOCKED і FOR UPDATE без SKIP LOCKED
  python scripts/run_lab4.py step5        крок 5: lease, heartbeat, fencing token (kill -STOP на 2·L)
  python scripts/run_lab4.py step6 [--run K]   крок 6: X(W) для W = 1, 2, R з непорожньою чергою
  python scripts/run_lab4.py step7        крок 7: закон Літтла (0,7·X(R) і 1,3·X(R))
  python scripts/run_lab4.py step8        крок 8: коректна зупинка (rollout restart під навантаженням)
  python scripts/run_lab4.py step9        крок 9: FAIL_RATE = 0,15 і сценарій Ф5 посеред прогону
  python scripts/run_lab4.py d4           крок 10: Д4, захоплення пакетом b = 1 і b = 10
  python scripts/run_lab4.py restore      налаштування воркерів за замовчуванням, FAIL_RATE = 0

Перемикання режимів воркера (claim_mode, store_mode, t_poll_ms, lease_s, heartbeat, batch) іде через
таблицю worker_config без перезапуску подів; FAIL_RATE у кроці 9 задається в Deployment.
Результати: results/lab4/. Аналіз: python scripts/analyze_lab4.py
"""
import argparse
import json
import math
import os
import statistics as st
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker"))
import prom  # noqa: E402
import prom3  # noqa: E402
from common import RESULTS, ROOT, call, wait_health  # noqa: E402
import k8s_common as kc  # noqa: E402
from slice_proc import make_slice, process  # noqa: E402

R = 3
T_POLL_MS = 50
LEASE_S = 15
F = 0.15
BASE = "http://127.0.0.1:8080"
PROM = f"http://127.0.0.1:{os.environ.get('PROM_PORT', '9090')}"
STEPLOAD = str(ROOT / "loadgen" / "stepload.py")
OUT = RESULTS / "lab4"
IDS = OUT / "ids.json"
DEFAULT_CFG = {"claim_mode": "skip_locked", "store_mode": "idempotent", "t_poll_ms": str(T_POLL_MS),
               "lease_s": str(LEASE_S), "heartbeat": "1", "batch": "1"}


def save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("збережено:", OUT / name, flush=True)


def load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------- середовище: кластер k3d з лабораторної 3 ----------
class K8s:
    """Усі дії з кластером: kubectl і, для kill -STOP поза простором PID пода, docker exec у вузол k3d."""

    def psql(self, sql):
        """Виконує SQL у поді бази; повертає stdout (рядки без вирівнювання)."""
        r = kc.kubectl("exec", "-i", "db-0", "--", "sh", "-c",
                       'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -q',
                       input=sql, capture=True)
        return r.stdout

    def worker_pods(self, include_terminating=False):
        pods = kc.kjson("get", "pods", "-l", "app=worker")["items"]
        out = []
        for p in pods:
            if p["metadata"].get("deletionTimestamp") and not include_terminating:
                continue
            cs = (p["status"].get("containerStatuses") or [{}])[0]
            out.append({"pod": p["metadata"]["name"], "node": p["spec"].get("nodeName"),
                        "ready": bool(cs.get("ready")), "restarts": cs.get("restartCount", 0),
                        "container_id": (cs.get("containerID") or "").split("//")[-1],
                        "phase": p["status"].get("phase")})
        return out

    def scale(self, n, timeout=300):
        kc.kubectl("scale", "deployment/worker", f"--replicas={n}")
        t = time.time()
        while time.time() - t < timeout:
            pods = self.worker_pods(include_terminating=True)
            if len(pods) == n and all(p["ready"] for p in pods):
                return time.time() - t
            time.sleep(1)
        raise TimeoutError(f"не дочекалась {n} готових подів worker")

    def set_env(self, **kv):
        kc.kubectl("set", "env", "deployment/worker", *[f"{k}={v}" for k, v in kv.items()])
        kc.kubectl("rollout", "status", "deployment/worker", "--timeout=300s")

    def metrics_text(self, pod):
        r = kc.kubectl("exec", pod, "--", "python", "-c",
                       "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())",
                       capture=True, check=False)
        return r.stdout if r.returncode == 0 else ""

    def logs(self, pod, previous=False):
        args = ["logs", pod] + (["--previous"] if previous else [])
        r = kc.kubectl(*args, capture=True, check=False)
        return r.stdout

    def proc_state(self, pod):
        r = kc.kubectl("exec", pod, "--", "sh", "-c", "grep State /proc/1/status", capture=True, check=False)
        return (r.stdout or r.stderr).strip()

    def signal(self, pod, sig):
        """Спершу kubectl exec ... kill -SIG 1, як в умові. Воркер є PID 1 у своєму просторі PID, і ядро не
        доставляє йому SIGSTOP зсередини цього простору, тому, якщо стан процесу не змінився, сигнал
        надсилається з вузла k3d (docker exec <вузол> kill), де процес має інший PID."""
        tries = []
        r = kc.kubectl("exec", pod, "--", "sh", "-c", f"kill -{sig} 1", capture=True, check=False)
        time.sleep(0.3)
        state = self.proc_state(pod)
        tries.append({"how": f"kubectl exec {pod} -- kill -{sig} 1", "rc": r.returncode, "state_after": state})
        want_stopped = sig == "STOP"
        if ("stopped" in state) == want_stopped:
            return tries
        p = next(x for x in self.worker_pods() if x["pod"] == pod)
        pid = subprocess.run(["docker", "exec", p["node"], "crictl", "inspect", "--output", "go-template",
                              "--template", "{{.info.pid}}", p["container_id"]],
                             capture_output=True, text=True).stdout.strip()
        r2 = subprocess.run(["docker", "exec", p["node"], "kill", f"-{sig}", pid], capture_output=True, text=True)
        time.sleep(0.3)
        tries.append({"how": f"docker exec {p['node']} kill -{sig} {pid}", "rc": r2.returncode,
                      "stderr": r2.stderr.strip(), "state_after": self.proc_state(pod)})
        return tries

    def rollout_restart(self):
        kc.kubectl("rollout", "restart", "deployment/worker")

    def rollout_wait(self):
        kc.kubectl("rollout", "status", "deployment/worker", "--timeout=300s")

    def watch_pods(self):
        """Фоновий kubectl get pods -w: збирає коди завершення контейнерів worker."""
        cmd = ["kubectl"] + (["--context", kc.CONTEXT] if kc.CONTEXT else []) + [
            "-n", kc.NS, "get", "pods", "-l", "app=worker", "-w", "-o", "json", "--output-watch-events"]
        return PodWatch(subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, encoding="utf-8"))

    def db_cpu_seconds(self):
        """Сумарний час CPU контейнера бази з cgroup (v2: cpu.stat usage_usec; v1: cpuacct.usage)."""
        r = kc.kubectl("exec", "db-0", "--", "sh", "-c",
                       "cat /sys/fs/cgroup/cpu.stat 2>/dev/null || cat /sys/fs/cgroup/cpuacct/cpuacct.usage",
                       capture=True, check=False)
        for line in r.stdout.splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
        try:
            return int(r.stdout.strip()) / 1e9
        except ValueError:
            return None

    def lock_sampler(self, seconds):
        """Очікування блокувань у базі: раз на секунду миттєвий зріз pg_stat_activity (wait_event_type = 'Lock')
        і pg_locks (granted = false), як в умові, і додатково найбільше значення за 50 зрізами через 20 мс
        усередині цієї секунди (короткі очікування між секундними зрізами інакше не видно)."""
        cmd = ["kubectl"] + (["--context", kc.CONTEXT] if kc.CONTEXT else []) + [
            "-n", kc.NS, "exec", "db-0", "--", "sh", "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "$0"', lock_sampler_sql(seconds)]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")

    def wait_web(self):
        wait_health(BASE, timeout=180)


def lock_sampler_sql(seconds):
    return f"""DO $$ DECLARE c int; g int; mc int; mg int; ic int; ig int; BEGIN
  FOR i IN 1..{int(seconds)} LOOP
    mc := 0; mg := 0;
    FOR j IN 1..50 LOOP
      PERFORM pg_stat_clear_snapshot();
      SELECT count(*) INTO c FROM pg_stat_activity WHERE wait_event_type = 'Lock';
      SELECT count(*) INTO g FROM pg_locks WHERE NOT granted;
      IF j = 1 THEN ic := c; ig := g; END IF;
      mc := greatest(mc, c); mg := greatest(mg, g);
      PERFORM pg_sleep(0.02);
    END LOOP;
    RAISE NOTICE 'S|%|%|%|%|%', extract(epoch FROM clock_timestamp()), ic, ig, mc, mg;
  END LOOP; END $$;"""


def read_lock_samples(proc, t0, t1):
    proc.terminate()
    out = []
    for line in proc.stdout.read().splitlines():
        if "S|" not in line:
            continue
        parts = line.split("S|", 1)[1].split("|")
        ts = float(parts[0])
        if t0 - 1 <= ts <= t1 + 1:
            out.append({"t": ts, "lock_waits": int(parts[1]), "not_granted": int(parts[2]),
                        "lock_waits_max_20ms": int(parts[3]), "not_granted_max_20ms": int(parts[4])})
    return out


class PodWatch:
    def __init__(self, proc):
        self.proc = proc
        self.terminated = {}
        self.events = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = ""
        for line in self.proc.stdout:
            buf += line
            try:
                ev = json.loads(buf)
            except ValueError:
                continue
            buf = ""
            obj = ev.get("object", ev)
            name = obj["metadata"]["name"]
            for cs in obj.get("status", {}).get("containerStatuses") or []:
                for key in ("state", "lastState"):
                    term = (cs.get(key) or {}).get("terminated")
                    if term:
                        k = (name, term.get("containerID") or term.get("finishedAt"))
                        if k not in self.terminated:
                            self.terminated[k] = {"pod": name, "exit_code": term.get("exitCode"),
                                                  "reason": term.get("reason"), "finished": term.get("finishedAt"),
                                                  "seen": time.time(), "where": key}
            self.events.append({"t": time.time(), "type": ev.get("type"), "pod": name,
                                "deleting": bool(obj["metadata"].get("deletionTimestamp"))})

    def stop(self):
        self.proc.terminate()
        return list(self.terminated.values())


ENV = K8s()


# ---------- база ----------
def q(sql):
    """SELECT → список словників (через json_agg)."""
    out = ENV.psql(f"SELECT coalesce(json_agg(r_), '[]'::json) FROM ({sql}) r_;")
    return json.loads(out.strip())


def q1(sql):
    rows = q(sql)
    return rows[0] if rows else {}


def execute(sql):
    ENV.psql(sql)


def set_cfg(**kv):
    """Налаштування воркерів через таблицю worker_config; воркери перечитують її раз на секунду."""
    vals = ", ".join(f"('{k}', '{v}')" for k, v in kv.items())
    execute(f"INSERT INTO worker_config (key, value) VALUES {vals} "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;")
    time.sleep(2.5)


def reset_cfg(**kv):
    execute("DELETE FROM worker_config;")
    set_cfg(**{**DEFAULT_CFG, **kv})


def clean_tasks():
    execute("TRUNCATE tasks, task_results, task_events RESTART IDENTITY;")


def pk(on):
    if on:
        execute("DROP INDEX IF EXISTS idx_task_results_task; "
                "ALTER TABLE task_results ADD CONSTRAINT task_results_pkey PRIMARY KEY (task_id);")
    else:
        execute("ALTER TABLE task_results DROP CONSTRAINT IF EXISTS task_results_pkey; "
                "CREATE INDEX IF NOT EXISTS idx_task_results_task ON task_results (task_id);")


def has_pk():
    return bool(q("SELECT 1 FROM pg_constraint WHERE conname = 'task_results_pkey'"))


def counts():
    return q1("SELECT count(*) FILTER (WHERE status = 'queued') AS queued, "
              "count(*) FILTER (WHERE status = 'running') AS running, "
              "count(*) FILTER (WHERE status = 'done') AS done, "
              "count(*) FILTER (WHERE status = 'failed') AS failed, count(*) AS total FROM tasks")


def wait_drain(timeout=900, quiet=2.0):
    """Чекає, поки в черзі немає queued і running; повертає час, с."""
    t = time.time()
    while time.time() - t < timeout:
        c = counts()
        if c["queued"] == 0 and c["running"] == 0:
            time.sleep(quiet)
            c = counts()
            if c["queued"] == 0 and c["running"] == 0:
                return time.time() - t
        time.sleep(1)
    raise TimeoutError(f"черга не спорожніла: {counts()}")


def xact_total():
    return q1("SELECT sum(xact_commit + xact_rollback)::bigint AS x, extract(epoch FROM clock_timestamp()) AS t "
              "FROM pg_stat_database WHERE datname = current_database()")


def seed():
    subprocess.run([sys.executable, str(ROOT / "loadgen" / "seed.py"), "--base-url", BASE,
                    "--ids-file", str(IDS)], check=True)


def create_tasks(prefix, rate, count=0, duration=0):
    args = [sys.executable, STEPLOAD, "--base-url", BASE, "--ids-file", str(IDS), "--tasks-rate", str(rate),
            "--out-prefix", str(OUT / prefix)]
    args += ["--tasks-count", str(count)] if count else ["--tasks-duration", str(duration)]
    subprocess.run(args, check=True)
    return load(prefix + "_tasks.json")


def create_tasks_bg(prefix, rate, count=0, duration=0):
    (OUT / (prefix + "_tasks.json")).unlink(missing_ok=True)  # t0 читається з цього файла лише після запуску
    args = [sys.executable, STEPLOAD, "--base-url", BASE, "--ids-file", str(IDS), "--tasks-rate", str(rate),
            "--out-prefix", str(OUT / prefix)]
    args += ["--tasks-count", str(count)] if count else ["--tasks-duration", str(duration)]
    return subprocess.Popen(args)


# ---------- метрики воркерів напряму з /metrics подів (точні різниці за вікно) ----------
def parse_metrics(text):
    out = {}
    for line in text.splitlines():
        if not line.startswith("worker_") or line.startswith("#"):
            continue
        name_labels, val = line.rsplit(" ", 1)
        out[name_labels] = float(val)
    return out


def scrape_workers():
    """{імʼя_ряду_з_мітками: сума за всіма подами} + перелік подів."""
    total, pods = {}, []
    for p in ENV.worker_pods():
        m = parse_metrics(ENV.metrics_text(p["pod"]))
        pods.append(p["pod"])
        for k, v in m.items():
            total[k] = total.get(k, 0.0) + v
    return {"pods": pods, "m": total, "t": time.time()}


def delta(a, b):
    return {k: b["m"].get(k, 0.0) - a["m"].get(k, 0.0) for k in b["m"]}


def hist_quantile(d, step, qq):
    """Квантиль за різницею бакетів гістограми worker_task_duration_seconds (як histogram_quantile)."""
    buckets = []
    for k, v in d.items():
        if k.startswith("worker_task_duration_seconds_bucket{") and f'step="{step}"' in k:
            le = k.split('le="')[1].split('"')[0]
            buckets.append((math.inf if le == "+Inf" else float(le), v))
    buckets.sort()
    if not buckets or buckets[-1][1] <= 0:
        return None
    total = buckets[-1][1]
    rank = qq * total
    prev_le, prev_c = 0.0, 0.0
    for le, c in buckets:
        if c >= rank:
            if le == math.inf:
                return prev_le
            return prev_le + (le - prev_le) * (rank - prev_c) / max(c - prev_c, 1e-12)
        prev_le, prev_c = le, c
    return None


def hist_mean(d, step):
    s = d.get(f'worker_task_duration_seconds_sum{{step="{step}"}}', 0.0)
    c = d.get(f'worker_task_duration_seconds_count{{step="{step}"}}', 0.0)
    return (s / c if c else None), c


def claims(d):
    return {r: d.get(f'worker_claims_total{{result="{r}"}}', 0.0) for r in ("task", "empty")}


def worker_counters(d):
    return {r: d.get(f'worker_tasks_total{{result="{r}"}}', 0.0) for r in ("done", "failed", "lost")}


# ---------- підрахунки за таблицями ----------
def dup_stats():
    n = q1("SELECT count(*) AS n FROM tasks")["n"]
    by_res = q1("SELECT count(*) AS k, coalesce(sum(c), 0) AS rows FROM "
                "(SELECT task_id, count(*) AS c FROM task_results GROUP BY 1 HAVING count(*) > 1) t")
    by_log = q1("SELECT count(*) AS k FROM (SELECT task_id FROM task_events WHERE event = 'done' "
                "GROUP BY 1 HAVING count(*) > 1) t")
    by_claim = q1("SELECT count(*) AS k FROM (SELECT task_id FROM task_events WHERE event = 'claim' "
                  "GROUP BY 1 HAVING count(DISTINCT worker_id) > 1) t")
    workers = q("SELECT worker_id, count(*) AS done FROM task_events WHERE event = 'done' GROUP BY 1 ORDER BY 1")
    return {"n": n, "dup_by_results": by_res["k"], "extra_result_rows": by_res["rows"],
            "D_results": by_res["k"] / n if n else None, "dup_by_logs": by_log["k"],
            "D_logs": by_log["k"] / n if n else None, "claimed_by_several_workers": by_claim["k"],
            "results_rows": q1("SELECT count(*) AS c FROM task_results")["c"], "done_by_worker": workers}


def per_second_finished(t_from, t_to):
    rows = q(f"SELECT floor(extract(epoch FROM finished_at))::bigint AS s, count(*) AS c FROM tasks "
             f"WHERE status = 'done' AND finished_at >= to_timestamp({t_from}) AND finished_at < to_timestamp({t_to}) "
             "GROUP BY 1 ORDER BY 1")
    by = {r["s"]: r["c"] for r in rows}
    return [by.get(s, 0) for s in range(int(math.floor(t_from)), int(math.floor(t_to)))]


def prepare(w, **cfg):
    ENV.wait_web()
    reset_cfg(**cfg)
    ENV.scale(w)
    if not IDS.exists():
        seed()


def x1_estimate():
    s2 = load("step2.json")
    return 1.0 / (s2["t_io"] + s2["t_cpu"] + T_POLL_MS / 2000)


def x_of(w):
    """X(W) з кроку 6 (середнє за прогонами)."""
    vals = []
    for k in range(1, 4):
        p = OUT / f"step6_w{w}_r{k}.json"
        if p.exists():
            vals.append(json.loads(p.read_text(encoding="utf-8"))["X"])
    if not vals:
        sys.exit("спершу виконайте крок 6")
    return st.fmean(vals)


# ---------- крок 1 ----------
def cmd_step1(_):
    ENV.wait_web()
    seed()
    rid = json.loads(IDS.read_text())[0]
    res = {}
    s, h, body = call("POST", f"{BASE}/admissions/{rid}/tasks")
    res["post_existing"] = {"status": s, "location": {k.lower(): v for k, v in h.items()}.get("location"), "body": body}
    s, _, b404 = call("POST", f"{BASE}/admissions/999999999/tasks")
    res["post_missing"] = {"status": s, "body": b404}
    s, _, got = call("GET", f"{BASE}/tasks/{body['id']}")
    res["get_task"] = {"status": s, "body": got}
    res["get_missing"] = {"status": call("GET", f"{BASE}/tasks/999999999")[0]}
    s, _, lst = call("GET", f"{BASE}/tasks?status=queued&limit=2&offset=0")
    res["list_queued"] = {"status": s, "total": lst.get("total"), "items": len(lst.get("items", []))}
    res["list_bad_status"] = {"status": call("GET", f"{BASE}/tasks?status=unknown")[0]}
    time.sleep(6)
    _, _, metrics = call("GET", f"{BASE}/metrics")
    text = metrics if isinstance(metrics, str) else ""
    res["metrics"] = [x for x in text.splitlines() if x.startswith("tasks_")]
    res["schema"] = q("SELECT table_name, column_name, data_type, column_default FROM information_schema.columns "
                      "WHERE table_name IN ('tasks', 'task_results') ORDER BY table_name, ordinal_position")
    res["indexes"] = q("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE tablename IN ('tasks', 'task_results')")
    res["worker_pods"] = ENV.worker_pods()
    try:
        res["prom_targets"] = [(t["labels"].get("job"), t["labels"].get("instance"), t["health"])
                               for t in prom._get(PROM, "/api/v1/targets")["data"]["activeTargets"]
                               if t["labels"].get("job") in ("web", "worker")]
    except Exception as e:  # noqa: BLE001
        res["prom_targets"] = str(e)
    save("step1.json", res)
    print(json.dumps({k: v for k, v in res.items() if k not in ("schema", "indexes")}, indent=2, ensure_ascii=False))


# ---------- крок 2 ----------
def cmd_step2(_):
    prepare(1)
    clean_tasks()
    a = scrape_workers()
    gen = create_tasks("step2", rate=2, count=20)
    wait_drain()
    b = scrape_workers()
    d = delta(a, b)
    means = {s: hist_mean(d, s) for s in ("claim", "fetch", "process", "store")}
    t_io = means["fetch"][0] + means["store"][0]
    t_cpu = means["process"][0]
    # детермінованість: результат воркера збігається з локальним обчисленням за тим самим seed
    rows = q("SELECT t.id, t.seed, r.result, r.worker_id FROM tasks t JOIN task_results r ON r.task_id = t.id "
             "ORDER BY t.id LIMIT 3")
    check = [{"task": r["id"], "seed": r["seed"], "worker": r["result"], "local": process(make_slice(r["seed"])),
              "equal": r["result"] == process(make_slice(r["seed"]))} for r in rows]
    res = {"tasks": gen["created"], "done": counts()["done"], "mean_s": {k: v[0] for k, v in means.items()},
           "count": {k: v[1] for k, v in means.items()}, "t_io": t_io, "t_cpu": t_cpu,
           "X1_formula": 1 / (t_io + t_cpu + T_POLL_MS / 2000), "determinism": check,
           "pods": ENV.worker_pods()}
    save("step2.json", res)
    print(f"t_io = {t_io * 1000:.1f} мс, t_cpu = {t_cpu * 1000:.1f} мс; збіг з локальним: "
          f"{all(c['equal'] for c in check)}")


# ---------- кроки 3-4 ----------
def series(label, sample_locks=False, n=300, rate=100):
    clean_tasks()
    sampler = ENV.lock_sampler(60) if sample_locks else None
    a = scrape_workers()
    x0 = xact_total()
    t0 = time.time()
    gen = create_tasks(label, rate=rate, count=n)
    wait_drain(quiet=1.0)
    t1 = time.time()
    b = scrape_workers()
    x1 = xact_total()
    locks = read_lock_samples(sampler, t0, t1) if sampler else []
    d = delta(a, b)
    res = {"label": label, "created": gen["created"], "non2xx": gen["non2xx"], "duration_s": t1 - t0,
           **dup_stats(), "claims": claims(d), "claim_p99_s": hist_quantile(d, "claim", 0.99),
           "claim_p50_s": hist_quantile(d, "claim", 0.50), "claim_mean_s": hist_mean(d, "claim")[0],
           "worker_counters": worker_counters(d), "db_xact_per_s": (x1["x"] - x0["x"]) / (x1["t"] - x0["t"]),
           "status": counts()}
    if sampler:
        res["lock_samples"] = locks
        res["max_lock_waits"] = max((x["lock_waits"] for x in locks), default=0)
        res["max_not_granted"] = max((x["not_granted"] for x in locks), default=0)
        res["max_lock_waits_20ms"] = max((x["lock_waits_max_20ms"] for x in locks), default=0)
        res["seconds_with_lock_waits_20ms"] = sum(1 for x in locks if x["lock_waits_max_20ms"] > 0)
    log(label, json.dumps({k: res[k] for k in ("D_results", "D_logs", "claims", "claim_p99_s")}, ensure_ascii=False))
    return res


def cmd_step3(_):
    prepare(R, claim_mode="naive", store_mode="plain", lease_s="0", heartbeat="0")
    pk(False)
    out = {"R": R, "pk": has_pk()}
    for tp in (T_POLL_MS, 1):
        set_cfg(t_poll_ms=str(tp))
        out[f"tpoll_{tp}"] = series(f"step3_tpoll{tp}")
        save("step3.json", out)
    clean_tasks()
    pk(True)


def cmd_step4(_):
    prepare(R, store_mode="plain", lease_s="0", heartbeat="0")
    clean_tasks()
    if not has_pk():
        pk(True)
    out = {"R": R, "pk": has_pk()}
    set_cfg(t_poll_ms="1", claim_mode="skip_locked")
    out["skip_locked"] = series("step4_skip", sample_locks=True)
    save("step4.json", out)
    set_cfg(claim_mode="for_update")
    out["for_update"] = series("step4_forupdate", sample_locks=True)
    save("step4.json", out)
    reset_cfg()


# ---------- крок 5 ----------
def cmd_step5(_):
    prepare(2)
    clean_tasks()
    pods = [p["pod"] for p in ENV.worker_pods()]
    victim, other = pods[0], pods[1]
    t_run = time.time()
    gen = create_tasks_bg("step5", rate=10, count=100)
    time.sleep(3)
    target, tries = None, []
    for _ in range(50):
        tries = ENV.signal(victim, "STOP")
        rows = q(f"SELECT id, attempts, token, lease_until, worker_id FROM tasks WHERE status = 'running' "
                 f"AND worker_id = '{victim}'")
        if rows:
            target = rows[0]
            break
        ENV.signal(victim, "CONT")
        time.sleep(0.15)
    if target is None:
        sys.exit("не вдалось зупинити воркер посеред задачі")
    t_stop = time.time()
    log(f"зупинено {victim} на задачі {target['id']} (attempts {target['attempts']}, token {target['token']})")
    while time.time() - t_stop < 2 * LEASE_S:
        time.sleep(0.5)
    mid = q1(f"SELECT id, status, attempts, token, worker_id FROM tasks WHERE id = {target['id']}")
    cont = ENV.signal(victim, "CONT")
    t_cont = time.time()
    gen.wait()
    wait_drain()
    time.sleep(3)
    tid = target["id"]
    after = q1(f"SELECT id, status, attempts, token, worker_id FROM tasks WHERE id = {tid}")
    results = q(f"SELECT task_id, token, worker_id FROM task_results WHERE task_id = {tid}")
    events = q(f"SELECT event, worker_id, token, info, extract(epoch FROM at) AS at FROM task_events "
               f"WHERE task_id = {tid} ORDER BY at")
    victim_log = []
    for x in ENV.logs(victim).splitlines():
        try:
            rec = json.loads(x)
        except ValueError:
            continue
        if rec.get("t", 0) >= t_run and (rec.get("task") == tid or rec.get("event") == "stale"):
            victim_log.append(x)
    res = {"victim": victim, "other": other, "lease_s": LEASE_S, "stop_tries": tries, "cont_tries": cont,
           "stopped_s": t_cont - t_stop, "task_before": target, "task_while_stopped": mid, "task_after": after,
           "finished_by": [r["worker_id"] for r in results], "results_rows": len(results), "events": events,
           "victim_log_lines": victim_log, "all": dup_stats(), "status": counts()}
    save("step5.json", res)
    print(json.dumps({k: res[k] for k in ("task_before", "task_after", "finished_by", "results_rows",
                                          "victim_log_lines")}, indent=2, ensure_ascii=False))


# ---------- крок 6 і Д4: непорожня черга ----------
def backlog_run(label, w, n, restart_at=None):
    """Черга з n задач створюється при 0 воркерів, далі scale до w; X за останні 30 с до спорожнення."""
    ENV.scale(0)
    clean_tasks()
    create_tasks(label, rate=200, count=n)
    xs0 = xact_total()
    cpu0 = ENV.db_cpu_seconds()
    t_scale = time.time()
    ENV.scale(w)
    a = scrape_workers()
    b = watch = None
    if restart_at is not None:
        watch = ENV.watch_pods()
        time.sleep(restart_at)
        log("rollout restart")
        ENV.rollout_restart()
    t_empty = None
    while True:
        c = counts()
        if c["queued"] == 0 and t_empty is None:
            t_empty = q1("SELECT extract(epoch FROM max(started_at)) AS t FROM tasks")["t"]
            b = scrape_workers() if restart_at is None else None
            xs1 = xact_total()
            cpu1 = ENV.db_cpu_seconds()
        if c["queued"] == 0 and c["running"] == 0:
            break
        time.sleep(0.5)
    t_first = q1("SELECT extract(epoch FROM min(finished_at)) AS t FROM tasks")["t"]
    win = min(30.0, math.floor(t_empty - t_first) - 2)
    per_sec = per_second_finished(t_empty - win, t_empty)
    res = {"label": label, "W": w, "n": n, "scale_wait_s": a["t"] - t_scale, "t_first_done": t_first,
           "t_empty": t_empty, "window_s": win, "per_second": per_sec,
           "X": st.fmean(per_sec) if per_sec else None, "X_std": st.stdev(per_sec) if len(per_sec) > 1 else 0.0,
           "run_s": t_empty - t_first, "status": counts(), **dup_stats(),
           "db_xact_per_s": (xs1["x"] - xs0["x"]) / (xs1["t"] - xs0["t"]),
           "db_cpu_cores": ((cpu1 - cpu0) / (xs1["t"] - xs0["t"])) if cpu0 is not None and cpu1 is not None else None}
    if b is not None:
        d = delta(a, b)
        res.update({"claim_p99_s": hist_quantile(d, "claim", 0.99), "claim_p50_s": hist_quantile(d, "claim", 0.5),
                    "claims": claims(d), "means_s": {s: hist_mean(d, s)[0] for s in ("claim", "fetch", "process", "store")}})
    if watch is not None:
        ENV.rollout_wait()
        time.sleep(3)
        res["terminated"] = watch.stop()
        res["returned"] = q1("SELECT count(*) AS c FROM task_events WHERE event = 'returned'")["c"]
        res["exit_events"] = q("SELECT worker_id, info FROM task_events WHERE event = 'exit' ORDER BY at")
        res["attempts_gt1"] = q1("SELECT count(*) AS c FROM tasks WHERE attempts > 1")["c"]
    log(label, f"W={w} X={res['X']:.2f}±{res['X_std']:.2f} (вікно {win:.0f} с)")
    return res


def backlog_size(w):
    """400 задач за умовою; якщо за оцінкою X(1) черга з 400 задач спорожніє швидше ніж за 45 с,
    задач більше, щоб вікно 30 с лежало в сталому режимі (відхилення записується в результат)."""
    try:
        est = w * x1_estimate()
    except FileNotFoundError:
        return 400
    return max(400, int(math.ceil(45 * est / 50.0) * 50))


def cmd_step6(a):
    prepare(1)
    for w in (1, 2, R):
        n = a.tasks or backlog_size(w)
        res = backlog_run(f"step6_w{w}_r{a.run}", w, n)
        res["pods"] = ENV.worker_pods()
        save(f"step6_w{w}_r{a.run}.json", res)
    ENV.scale(R)


# ---------- крок 7 ----------
def little(t0, t1):
    """λ, W_q, L_q за таблицею tasks для вікна [t0, t1): L_q як інтеграл кількості queued за часом / тривалість."""
    w = t1 - t0
    r = q1(f"""SELECT count(*) AS created,
        avg(extract(epoch FROM started_at - created_at)) AS wq,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM started_at - created_at)) AS wq_p50,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM started_at - created_at)) AS wq_p99
        FROM tasks WHERE created_at >= to_timestamp({t0}) AND created_at < to_timestamp({t1})""")
    integ = q1(f"""SELECT coalesce(sum(greatest(0, extract(epoch FROM
            least(coalesce(started_at, to_timestamp({t1})), to_timestamp({t1})) - greatest(created_at, to_timestamp({t0}))))), 0)
            AS s FROM tasks WHERE created_at < to_timestamp({t1})
            AND coalesce(started_at, to_timestamp({t1})) > to_timestamp({t0})""")["s"]
    lam = r["created"] / w
    return {"window": [t0, t1], "lambda": lam, "W_q": r["wq"], "W_q_p50": r["wq_p50"], "W_q_p99": r["wq_p99"],
            "lambda_Wq": lam * (r["wq"] or 0), "L_q_db": integ / w}


def prom_avg(expr, t_end, w):
    try:
        r = prom3.instant(PROM, f"avg_over_time(({expr})[{int(w)}s:1s])", t_end)
        return float(r[0]["value"][1]) if r else None
    except Exception:  # noqa: BLE001
        return None


def cmd_step7(_):
    prepare(R)
    clean_tasks()
    xr = x_of(R)
    out = {"X_R": xr}
    rate = round(0.7 * xr, 2)
    g = create_tasks("step7_low", rate=rate, duration=180)
    wait_drain()
    t0 = g["t0"]
    lt = little(t0 + 60, t0 + 180)
    time.sleep(10)
    lt["L_q_prom"] = prom_avg("max(tasks_queued)", t0 + 180, 120)
    lt["L_q_prom_running"] = prom_avg("max(tasks_running)", t0 + 180, 120)
    lt["diff_pct_prom"] = (100 * (lt["lambda_Wq"] - lt["L_q_prom"]) / lt["L_q_prom"]) if lt["L_q_prom"] else None
    lt["diff_pct_db"] = (100 * (lt["lambda_Wq"] - lt["L_q_db"]) / lt["L_q_db"]) if lt["L_q_db"] else None
    out["low"] = {"rate": rate, "created": g["created"], "non2xx": g["non2xx"], **lt}
    save("step7.json", out)
    log("0,7·X(R):", json.dumps(lt, ensure_ascii=False))
    clean_tasks()
    rate = round(1.3 * xr, 2)
    g = create_tasks("step7_high", rate=rate, duration=120)
    t_end = g["t0"] + 120
    queue_end = counts()["queued"]
    wait_drain(timeout=1800)
    first = q1("SELECT avg(extract(epoch FROM started_at - created_at)) AS wq FROM "
               "(SELECT * FROM tasks ORDER BY created_at LIMIT 100) t")["wq"]
    last = q1("SELECT avg(extract(epoch FROM started_at - created_at)) AS wq FROM "
              "(SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100) t")["wq"]
    time.sleep(10)
    try:
        prom_q = prom3.instant(PROM, "max(tasks_queued)", t_end)
    except Exception:  # noqa: BLE001
        prom_q = None
    out["high"] = {"rate": rate, "created": g["created"], "non2xx": g["non2xx"], "queue_at_end_db": queue_end,
                   "queue_at_end_prom": float(prom_q[0]["value"][1]) if prom_q else None,
                   "W_q_first100": first, "W_q_last100": last,
                   "expected_growth": (rate - xr) * 120}
    save("step7.json", out)
    log("1,3·X(R):", json.dumps(out["high"], ensure_ascii=False))


# ---------- крок 8 ----------
def cmd_step8(_):
    prepare(R)
    clean_tasks()
    xr = x_of(R)
    rate = round(0.7 * xr, 2)
    watch = ENV.watch_pods()
    pods_before = ENV.worker_pods()
    gen = create_tasks_bg("step8", rate=rate, duration=90)
    time.sleep(30)
    t_restart = time.time()
    log("kubectl rollout restart deployment/worker")
    ENV.rollout_restart()
    ENV.rollout_wait()
    t_rolled = time.time()
    gen.wait()
    wait_drain()
    time.sleep(5)
    terminated = watch.stop()
    res = {"rate": rate, "t_restart": t_restart, "rollout_s": t_rolled - t_restart,
           "pods_before": pods_before, "pods_after": ENV.worker_pods(), "terminated": terminated,
           "returned": q1("SELECT count(*) AS c FROM task_events WHERE event = 'returned'")["c"],
           "returned_rows": q("SELECT task_id, worker_id, info FROM task_events WHERE event = 'returned'"),
           "exit_events": q("SELECT worker_id, info, extract(epoch FROM at) AS at FROM task_events "
                            "WHERE event IN ('exit', 'start') ORDER BY at"),
           "failed": counts()["failed"],
           "multi_result": q1("SELECT count(*) AS c FROM (SELECT task_id FROM task_results GROUP BY 1 "
                              "HAVING count(*) > 1) t")["c"],
           "attempts_gt1": q1("SELECT count(*) AS c FROM tasks WHERE attempts > 1")["c"],
           "stale": q1("SELECT count(*) AS c FROM task_events WHERE event = 'stale'")["c"],
           **dup_stats(), "status": counts()}
    save("step8.json", res)
    print(json.dumps({k: res[k] for k in ("terminated", "returned", "failed", "multi_result", "attempts_gt1")},
                     indent=2, ensure_ascii=False))


# ---------- крок 9: FAIL_RATE і сценарій Ф5 ----------
def cmd_step9(a):
    s2 = load("step2.json")
    xr = x_of(R)
    l_f5 = round(s2["t_cpu"] / 2, 4)
    ENV.wait_web()
    reset_cfg()
    ENV.set_env(FAIL_RATE=str(F))  # rollout: нові поди з FAIL_RATE = 0,15
    ENV.scale(R)
    clean_tasks()
    rate = round(0.7 * xr, 2)
    dur = a.duration
    marks = {"f5_no_heartbeat": dur / 2, "f5_heartbeat": dur * 3 / 4}
    restarts0 = {p["pod"]: p["restarts"] for p in ENV.worker_pods()}
    gen = create_tasks_bg("step9", rate=rate, duration=dur)
    time.sleep(1.0)
    t0 = None
    while t0 is None:  # підсумок генератора пишеться лише в кінці, тому початок — за першою задачею
        t0 = q1("SELECT extract(epoch FROM min(created_at)) AS t FROM tasks")["t"]
        if t0 is None:
            time.sleep(0.2)
    t_marks = {}
    while time.time() < t0 + marks["f5_no_heartbeat"]:
        time.sleep(0.2)
    set_cfg(lease_s=str(l_f5), heartbeat="0")
    t_marks["f5_no_heartbeat"] = time.time() - 2.5
    log(f"Ф5: L = {l_f5} с без heartbeat")
    while time.time() < t0 + marks["f5_heartbeat"]:
        time.sleep(0.2)
    set_cfg(heartbeat="1")
    t_marks["f5_heartbeat"] = time.time() - 2.5
    log("Ф5: heartbeat увімкнено")
    gen.wait()
    t_gen_end = t0 + dur
    pods_end = ENV.worker_pods()
    # Статистику знімаю одразу після прогону: перезапуски з наростаючою паузою (CrashLoopBackOff) не дають
    # пулу дообробити чергу з FAIL_RATE = 0,15. L повертається до 15 с; частки рахуються за задачами,
    # що вже в кінцевому стані (done або failed).
    set_cfg(lease_s=str(LEASE_S), heartbeat="1")
    time.sleep(5)
    status_end = counts()
    phases = {"base": (t0, t_marks["f5_no_heartbeat"]),
              "f5_no_heartbeat": (t_marks["f5_no_heartbeat"], t_marks["f5_heartbeat"]),
              "f5_heartbeat": (t_marks["f5_heartbeat"], t_gen_end)}
    ph = {}
    for name, (p0, p1) in phases.items():
        tasks = q1(f"""SELECT count(*) AS created, count(*) FILTER (WHERE status IN ('done', 'failed')) AS n,
            count(*) FILTER (WHERE attempts > 1 AND status IN ('done', 'failed')) AS att_gt1,
            count(*) FILTER (WHERE status = 'failed') AS failed, count(*) FILTER (WHERE status = 'done') AS done
            FROM tasks WHERE created_at >= to_timestamp({p0}) AND created_at < to_timestamp({p1})""")
        ev = q1(f"""SELECT count(*) FILTER (WHERE event = 'stale') AS stale,
            count(*) FILTER (WHERE event = 'stale' AND info->>'stage' = 'heartbeat') AS stale_hb,
            count(*) FILTER (WHERE event = 'stale' AND info->>'stage' = 'store') AS stale_store,
            count(*) FILTER (WHERE event = 'claim') AS claims,
            count(*) FILTER (WHERE event = 'claim' AND token > 1) AS reclaims,
            count(*) FILTER (WHERE event = 'reaped') AS reaped,
            count(*) FILTER (WHERE event = 'done') AS done_events
            FROM task_events WHERE at >= to_timestamp({p0}) AND at < to_timestamp({p1})""")
        fin = per_second_finished(p0, p1)
        ph[name] = {"window": [p0, p1], **tasks, **ev, "X": st.fmean(fin) if fin else None,
                    "share_att_gt1": tasks["att_gt1"] / tasks["n"] if tasks["n"] else None}
    dist = q("SELECT attempts, status, count(*) AS c FROM tasks WHERE status IN ('done', 'failed') "
             "GROUP BY 1, 2 ORDER BY 1, 2")
    base = phases["base"]
    dist_base = q(f"SELECT attempts, status, count(*) AS c FROM tasks WHERE status IN ('done', 'failed') "
                  f"AND created_at >= to_timestamp({base[0]}) "
                  f"AND created_at < to_timestamp({base[1]}) GROUP BY 1, 2 ORDER BY 1, 2")
    per_sec = per_second_finished(t0, t_gen_end)
    res = {"F": F, "rate": rate, "X_R": xr, "duration": dur, "t0": t0, "t_gen_end": t_gen_end,
           "L_f5": l_f5, "t_cpu": s2["t_cpu"], "marks": t_marks, "phases": ph,
           "restarts_before": restarts0, "restarts_end": {p["pod"]: p["restarts"] for p in pods_end},
           "restarts_after_drain": {p["pod"]: p["restarts"] for p in ENV.worker_pods()},
           "pods_text": kc.kubectl("get", "pods", "-l", "app=worker", capture=True).stdout,
           "attempts_distribution": dist, "attempts_distribution_base": dist_base,
           "multi_result": q1("SELECT count(*) AS c FROM (SELECT task_id FROM task_results GROUP BY 1 "
                              "HAVING count(*) > 1) t")["c"],
           "X_run": st.fmean(per_sec) if per_sec else None, "per_second_finished": per_sec,
           "status_end": status_end,
           "expected": {"share_att_gt1": F, "failed_share": F ** 3, "claims_per_task": 1 / (1 - F)}}
    t_end = time.time()
    res["prom"] = {}
    for name, expr in {"tasks_queued": "max(tasks_queued)", "tasks_running": "max(tasks_running)",
                       "restarts": 'sum(kube_pod_container_status_restarts_total{namespace="lab3",container="worker"})'}.items():
        try:
            res["prom"][name] = prom.range_q(PROM, expr, t0 - 10, t_end, 1)
        except Exception as e:  # noqa: BLE001
            res["prom"][name] = str(e)
    save("step9.json", res)
    print(json.dumps({k: res[k] for k in ("phases", "restarts_end", "multi_result", "X_run", "status_end")},
                     indent=2, ensure_ascii=False))
    # дообробка решти черги вже без падінь (нові поди), щоб перевірити, що жодна задача не має двох результатів
    ENV.set_env(FAIL_RATE="0")
    reset_cfg()
    t = time.time()
    wait_drain(timeout=3600)
    res["after_drain"] = {"drain_s": time.time() - t, "status": counts(),
                          "multi_result": q1("SELECT count(*) AS c FROM (SELECT task_id FROM task_results GROUP BY 1 "
                                             "HAVING count(*) > 1) t")["c"],
                          "results_rows": q1("SELECT count(*) AS c FROM task_results")["c"]}
    save("step9.json", res)
    # Ф5 окремими прогонами без падінь: посеред прогону з FAIL_RATE = 0,15 поди більшість часу чекають
    # перезапуску, і задач, які встигли б оброблятися з коротким lease, майже немає
    if a.f5_duration:
        res["f5_runs"] = {}
        for hb in ("0", "1"):
            res["f5_runs"]["heartbeat" if hb == "1" else "no_heartbeat"] = f5_run(l_f5, hb, rate, a.f5_duration)
            save("step9.json", res)
        reset_cfg()


def f5_run(lease, hb, rate, dur):
    set_cfg(lease_s=str(lease), heartbeat=hb)
    label = f"step9_f5_hb{hb}"
    t0 = time.time()
    g = create_tasks(label, rate=rate, duration=dur)
    wait_drain(timeout=1800)
    t1 = time.time()
    tw = f"created_at >= to_timestamp({g['t0'] - 1}) AND created_at < to_timestamp({t1})"
    tasks = q1(f"""SELECT count(*) AS n, count(*) FILTER (WHERE attempts > 1) AS att_gt1,
        count(*) FILTER (WHERE status = 'failed') AS failed, count(*) FILTER (WHERE status = 'done') AS done,
        avg(attempts) AS mean_attempts FROM tasks WHERE {tw}""")
    ev = q1(f"""SELECT count(*) FILTER (WHERE event = 'stale') AS stale,
        count(*) FILTER (WHERE event = 'stale' AND info->>'stage' = 'heartbeat') AS stale_hb,
        count(*) FILTER (WHERE event = 'stale' AND info->>'stage' = 'store') AS stale_store,
        count(*) FILTER (WHERE event = 'claim') AS claims, count(*) FILTER (WHERE event = 'reaped') AS reaped
        FROM task_events WHERE at >= to_timestamp({t0}) AND at < to_timestamp({t1})""")
    multi = q1(f"SELECT count(*) AS c FROM (SELECT r.task_id FROM task_results r JOIN tasks t ON t.id = r.task_id "
               f"WHERE {tw.replace('created_at', 't.created_at')} GROUP BY 1 HAVING count(*) > 1) x")["c"]
    out = {"lease_s": lease, "heartbeat": hb == "1", "rate": rate, "duration": dur, "created": g["created"],
           **tasks, **ev, "multi_result": multi, "drain_s": t1 - t0 - dur,
           "share_att_gt1": tasks["att_gt1"] / tasks["n"] if tasks["n"] else None,
           "fail_rate": 0.0}
    log(label, json.dumps(out, ensure_ascii=False))
    return out


# ---------- крок 10: Д4 ----------
def cmd_d4(a):
    prepare(R)
    out = {}
    for b in (1, 10):
        set_cfg(batch=str(b))
        n = a.tasks or backlog_size(R)
        x = backlog_run(f"d4_b{b}", R, n)
        rs = backlog_run(f"d4_b{b}_sigterm", R, n, restart_at=a.restart_at)
        out[f"b{b}"] = {"X": x, "sigterm": rs}
        save("d4.json", out)
    reset_cfg()


def cmd_restore(_):
    ENV.set_env(FAIL_RATE="0")
    reset_cfg()
    if not has_pk():
        clean_tasks()
        pk(True)
    print("worker_config:", q("SELECT * FROM worker_config"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("step1", "step2", "step3", "step4", "step5", "step7", "step8", "restore"):
        sub.add_parser(name)
    p = sub.add_parser("step6")
    p.add_argument("--run", type=int, default=1)
    p.add_argument("--tasks", type=int, default=0, help="розмір черги (0 = 400 або більше, див. backlog_size)")
    p = sub.add_parser("step9")
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--f5-duration", type=float, default=60, help="окремі прогони Ф5 без падінь, с (0 = не робити)")
    p = sub.add_parser("d4")
    p.add_argument("--tasks", type=int, default=0)
    p.add_argument("--restart-at", type=float, default=8, help="через скільки секунд після scale — rollout restart")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    if not sys.flags.utf8_mode:
        sys.exit(subprocess.run([sys.executable, "-X", "utf8", *sys.argv]).returncode)
    main()
