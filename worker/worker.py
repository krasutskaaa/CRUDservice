"""Воркер черги задач лабораторної 4 (варіант 12: T_poll = 50 мс, L = 15 с, Д4 — захоплення пакетом).

Один процес, одна задача за раз. Задача: GET /admissions/{record_id} у сервісу web (fetch),
обробка зрізу (process), запис результату в базу (store). Потоки: головний (цикл захоплення),
heartbeat (продовження lease, власне зʼєднання з базою), HTTP /metrics.

Змінні середовища:
  DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD   база
  WEB_URL            адреса сервісу web (http://web:8000)
  WORKER_ID          ідентифікатор (у Kubernetes — імʼя пода через Downward API)
  T_POLL_MS          інтервал опитування за порожнього захоплення, мс (50)
  LEASE_S            тривалість lease L, с; 0 = без lease, heartbeat і повернення в чергу (15)
  HEARTBEAT          1/0: продовжувати lease кожні L/3 с (1)
  FAIL_RATE          імовірність os._exit(1) після обробки, до запису результату (0)
  CLAIM_MODE         naive | for_update | skip_locked (skip_locked)
  STORE_MODE         plain (INSERT без ON CONFLICT, без умови за токеном) | idempotent (idempotent)
  BATCH              скільки задач захоплювати однією командою, b (1)
  GRACE_S            terminationGracePeriodSeconds (30): після SIGTERM задачу, що не встигає,
                     воркер повертає в чергу за GRACE_MARGIN_S (5) с до кінця цього строку
  METRICS_PORT       порт /metrics (9100)
Ключі таблиці worker_config (lease_s, heartbeat, t_poll_ms, batch, claim_mode, store_mode, fail_rate)
перекривають змінні середовища; воркер перечитує таблицю раз на секунду.
"""
import http.client
import json
import os
import random
import signal
import sys
import threading
import time
from urllib.parse import urlparse

import psycopg
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from slice_proc import make_slice, process

WORKER_ID = os.environ.get("WORKER_ID") or os.uname().nodename
GRACE_S = float(os.environ.get("GRACE_S", "30"))
GRACE_MARGIN_S = float(os.environ.get("GRACE_MARGIN_S", "5"))

BUCKETS = (0.0005, 0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128, 0.256, 0.512,
           1.024, 2.048, 4.096, 8.192)
TASKS = Counter("worker_tasks_total", "Задачі за результатом", ["result"])
CLAIMS = Counter("worker_claims_total", "Спроби захоплення за результатом", ["result"])
DURATION = Histogram("worker_task_duration_seconds", "Тривалість кроку, с", ["step"], buckets=BUCKETS)
IN_FLIGHT = Gauge("worker_tasks_in_flight", "Захоплені задачі, ще не завершені цим воркером")
for _r in ("done", "failed", "lost"):
    TASKS.labels(_r)
for _r in ("task", "empty"):
    CLAIMS.labels(_r)
for _s in ("claim", "fetch", "process", "store"):
    DURATION.labels(_s)

DEFAULTS = {
    "t_poll_ms": os.environ.get("T_POLL_MS", "50"),
    "lease_s": os.environ.get("LEASE_S", "15"),
    "heartbeat": os.environ.get("HEARTBEAT", "1"),
    "fail_rate": os.environ.get("FAIL_RATE", "0"),
    "claim_mode": os.environ.get("CLAIM_MODE", "skip_locked"),
    "store_mode": os.environ.get("STORE_MODE", "idempotent"),
    "batch": os.environ.get("BATCH", "1"),
}


def conninfo():
    return (f"host={os.environ.get('DB_HOST', 'db')} port={os.environ.get('DB_PORT', '5432')} "
            f"dbname={os.environ.get('DB_NAME', 'admissions')} user={os.environ.get('DB_USER', 'admissions')} "
            f"password={os.environ.get('DB_PASSWORD', '')} connect_timeout=3 application_name={WORKER_ID}")


def log(event, **kw):
    print(json.dumps({"t": round(time.time(), 3), "worker": WORKER_ID, "event": event, **kw},
                     ensure_ascii=False), flush=True)


class Db:
    """Одне зʼєднання з перепідключенням (autocommit; транзакції — явні)."""

    def __init__(self, name):
        self.name = name
        self.conn = None

    def get(self):
        delay = 0.2
        while self.conn is None or self.conn.closed or self.conn.broken:
            try:
                self.conn = psycopg.connect(conninfo(), autocommit=True)
                log("db_connected", conn=self.name)
            except psycopg.OperationalError as e:
                log("db_connect_error", conn=self.name, error=str(e).strip()[:200])
                time.sleep(delay)
                delay = min(delay * 2, 5)
        return self.conn

    def execute(self, sql, params=None):
        return self.get().execute(sql, params)


class Settings:
    def __init__(self, db):
        self.db = db
        self.values = dict(DEFAULTS)
        self.loaded = 0.0
        self.apply()

    def refresh(self):
        if time.time() - self.loaded < 1.0:
            return
        self.loaded = time.time()
        try:
            rows = self.db.execute("SELECT key, value FROM worker_config").fetchall()
        except psycopg.Error:
            return
        vals = dict(DEFAULTS)
        vals.update({k: v for k, v in rows if k in DEFAULTS})
        if vals != self.values:
            log("config", **vals)
            self.values = vals
            self.apply()

    def apply(self):
        v = self.values
        self.t_poll = float(v["t_poll_ms"]) / 1000
        self.lease = float(v["lease_s"])
        self.heartbeat = v["heartbeat"] not in ("0", "false", "no")
        self.fail_rate = float(v["fail_rate"])
        self.claim_mode = v["claim_mode"]
        self.store_mode = v["store_mode"]
        self.batch = max(1, int(v["batch"]))


class Worker:
    def __init__(self):
        self.db = Db("main")
        self.cfg = Settings(self.db)
        self.stop = threading.Event()
        self.term_at = None
        self.lock = threading.Lock()
        self.in_flight = {}        # id → token: захоплені, ще не завершені
        self.lost = set()          # id, для яких уже зафіксовано втрату (щоб не рахувати двічі)
        self.current = None        # (id, token) задачі в обробці
        u = urlparse(os.environ.get("WEB_URL", "http://web:8000"))
        self.web = (u.hostname, u.port or 80)
        self.http = None

    # ---------- журнал ----------
    def event(self, event, task_id=None, token=None, db=None, **info):
        log(event, task=task_id, token=token, **info)
        try:
            (db or self.db).execute(
                "INSERT INTO task_events (task_id, token, worker_id, event, info) VALUES (%s, %s, %s, %s, %s)",
                (task_id, token, WORKER_ID, event, json.dumps(info) if info else None))
        except psycopg.Error as e:
            log("event_write_error", error=str(e).strip()[:200])

    def mark_lost(self, task_id, token, stage, db=None):
        with self.lock:
            first = task_id not in self.lost
            self.lost.add(task_id)
            self.in_flight.pop(task_id, None)
            IN_FLIGHT.set(len(self.in_flight))
        if first:
            TASKS.labels("lost").inc()
        self.event("stale", task_id, token, db=db, stage=stage, rows=0)

    # ---------- захоплення ----------
    def reap(self):
        """Повертає в чергу задачі зі спливлим lease; після max_attempts — failed."""
        rows = self.db.execute(
            "UPDATE tasks SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END, "
            "worker_id = NULL, "
            "finished_at = CASE WHEN attempts >= max_attempts THEN clock_timestamp() ELSE NULL END "
            "WHERE id IN (SELECT id FROM tasks WHERE status = 'running' AND lease_until < clock_timestamp() "
            # SKIP LOCKED: рядок, заблокований транзакцією зупиненого воркера, не блокує решту
            "FOR UPDATE SKIP LOCKED) RETURNING id, token, status, attempts, worker_id").fetchall()
        for tid, tok, stt, att, _ in rows:
            if stt == "failed":
                TASKS.labels("failed").inc()
            self.event("reaped", tid, tok, to=stt, attempts=att)

    def claim(self):
        c = self.cfg
        lease = c.lease if c.lease > 0 else None
        set_part = ("status = 'running', worker_id = %s, token = token + 1, "
                    "lease_until = CASE WHEN %s::float8 IS NULL THEN NULL "
                    "ELSE clock_timestamp() + make_interval(secs => %s::float8) END, "
                    "attempts = attempts + 1, started_at = clock_timestamp()")
        if c.claim_mode == "naive":
            # дві окремі команди без транзакції і без блокувань: між SELECT і UPDATE
            # інший воркер встигає прочитати ті самі рядки
            ids = self.db.execute(
                "SELECT id FROM tasks WHERE status = 'queued' ORDER BY created_at LIMIT %s",
                (c.batch,)).fetchall()
            out = []
            for (tid,) in ids:
                r = self.db.execute(f"UPDATE tasks SET {set_part} WHERE id = %s "
                                    "RETURNING id, record_id, seed, token, attempts",
                                    (WORKER_ID, lease, lease, tid)).fetchone()
                if r:
                    out.append(r)
            return out
        lock = "FOR UPDATE SKIP LOCKED" if c.claim_mode == "skip_locked" else "FOR UPDATE"
        return self.db.execute(
            f"UPDATE tasks SET {set_part} WHERE id IN (SELECT id FROM tasks WHERE status = 'queued' "
            f"ORDER BY created_at LIMIT %s {lock}) RETURNING id, record_id, seed, token, attempts",
            (WORKER_ID, lease, lease, c.batch)).fetchall()

    # ---------- кроки задачі ----------
    def fetch(self, record_id):
        for attempt in (1, 2):
            try:
                if self.http is None:
                    self.http = http.client.HTTPConnection(*self.web, timeout=10)
                self.http.request("GET", f"/admissions/{record_id}")
                r = self.http.getresponse()
                body = r.read()
                if r.status != 200:
                    raise RuntimeError(f"GET /admissions/{record_id} → {r.status}")
                return json.loads(body)
            except (OSError, http.client.HTTPException):
                if self.http is not None:
                    self.http.close()
                self.http = None
                if attempt == 2:
                    raise

    def store(self, tid, token, result):
        c = self.cfg
        if c.store_mode == "plain":
            try:
                self.db.execute("INSERT INTO task_results (task_id, token, worker_id, result) "
                                "VALUES (%s, %s, %s, %s)", (tid, token, WORKER_ID, json.dumps(result)))
            except psycopg.errors.UniqueViolation:
                self.event("duplicate_result", tid, token)
            self.db.execute("UPDATE tasks SET status = 'done', finished_at = clock_timestamp() WHERE id = %s",
                            (tid,))
            return True
        conn = self.db.get()
        with conn.transaction():
            n = conn.execute("UPDATE tasks SET status = 'done', finished_at = clock_timestamp() "
                             "WHERE id = %s AND token = %s AND status = 'running'", (tid, token)).rowcount
            if n == 1:
                conn.execute("INSERT INTO task_results (task_id, token, worker_id, result) "
                             "VALUES (%s, %s, %s, %s) ON CONFLICT (task_id) DO NOTHING",
                             (tid, token, WORKER_ID, json.dumps(result)))
        return n == 1

    def run_task(self, task):
        tid, record_id, seed, token, attempts = task
        with self.lock:
            self.current = (tid, token)
        try:
            t = time.perf_counter()
            self.fetch(record_id)
            t1 = time.perf_counter()
            DURATION.labels("fetch").observe(t1 - t)
            result = process(make_slice(seed))
            t2 = time.perf_counter()
            DURATION.labels("process").observe(t2 - t1)
            if self.cfg.fail_rate and random.random() < self.cfg.fail_rate:
                log("crash", task=tid, token=token)  # лише stdout: після os._exit запис у базу неможливий
                os._exit(1)  # без обробки сигналів і без закриття зʼєднань
            ok = self.store(tid, token, result)
            DURATION.labels("store").observe(time.perf_counter() - t2)
            if ok:
                TASKS.labels("done").inc()
                self.event("done", tid, token, attempts=attempts)
            else:
                self.mark_lost(tid, token, "store")
        except Exception as e:  # noqa: BLE001
            # задача лишається running; після спливання lease її поверне в чергу будь-який воркер
            self.event("task_error", tid, token, error=f"{type(e).__name__}: {e}"[:300])
        finally:
            with self.lock:
                self.current = None
                self.in_flight.pop(tid, None)
                IN_FLIGHT.set(len(self.in_flight))

    def give_back(self, tasks, reason, db=None):
        """Повертає в чергу ще не виконані захоплені задачі (лише свої: умова за токеном)."""
        n = 0
        for tid, token in tasks:
            rows = (db or self.db).execute(
                "UPDATE tasks SET status = 'queued', worker_id = NULL WHERE id = %s AND token = %s "
                "AND status = 'running'", (tid, token)).rowcount
            if rows:
                n += 1
                self.event("returned", tid, token, db=db, reason=reason)
            else:
                self.mark_lost(tid, token, "return", db=db)
        return n

    # ---------- потоки ----------
    def heartbeat_loop(self):
        db = Db("heartbeat")
        while True:
            c = self.cfg
            period = c.lease / 3 if c.lease > 0 else 1.0
            time.sleep(period)
            if c.lease <= 0 or not c.heartbeat:
                continue
            with self.lock:
                items = list(self.in_flight.items())
            for tid, token in items:
                try:
                    n = db.execute("UPDATE tasks SET lease_until = clock_timestamp() + make_interval(secs => %s) "
                                   "WHERE id = %s AND token = %s AND status = 'running'",
                                   (c.lease, tid, token)).rowcount
                except psycopg.Error as e:
                    log("heartbeat_error", task=tid, error=str(e).strip()[:200])
                    continue
                if n == 0 and tid in self.in_flight:
                    self.mark_lost(tid, token, "heartbeat", db=db)

    def watchdog(self):
        """Після SIGTERM: якщо задача не завершується до GRACE_S − GRACE_MARGIN_S, повертає її і виходить."""
        self.stop.wait()
        deadline = self.term_at + GRACE_S - GRACE_MARGIN_S
        while time.time() < deadline:
            time.sleep(0.2)
        with self.lock:
            items = list(self.in_flight.items())
        if items:
            db = Db("watchdog")
            n = self.give_back(items, "grace_timeout", db=db)
            self.event("exit", db=db, code=0, returned=n, reason="grace_timeout")
            os._exit(0)

    def on_sigterm(self, *_):
        if not self.stop.is_set():
            self.term_at = time.time()
            log("sigterm")
            self.stop.set()

    def main(self):
        signal.signal(signal.SIGTERM, self.on_sigterm)
        signal.signal(signal.SIGINT, self.on_sigterm)
        start_http_server(int(os.environ.get("METRICS_PORT", "9100")))
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        threading.Thread(target=self.watchdog, daemon=True).start()
        self.event("start", **self.cfg.values)
        returned = 0
        while not self.stop.is_set():
            try:
                self.cfg.refresh()
                if self.cfg.lease > 0:
                    self.reap()
                t = time.perf_counter()
                tasks = self.claim()
                DURATION.labels("claim").observe(time.perf_counter() - t)
            except psycopg.Error as e:
                log("db_error", error=str(e).strip()[:200])
                time.sleep(self.cfg.t_poll)
                continue
            if not tasks:
                CLAIMS.labels("empty").inc()
                self.stop.wait(self.cfg.t_poll)
                continue
            CLAIMS.labels("task").inc()
            with self.lock:
                for tid, _, _, token, _ in tasks:
                    self.in_flight[tid] = token
                IN_FLIGHT.set(len(self.in_flight))
            for tid, _, _, token, attempts in tasks:
                self.event("claim", tid, token, attempts=attempts, batch=len(tasks))
            for k, task in enumerate(tasks):
                if self.stop.is_set():
                    # SIGTERM посеред пакета: решту пакета повертаємо в чергу одразу
                    with self.lock:
                        rest = [(t[0], t[3]) for t in tasks[k:] if t[0] in self.in_flight]
                    returned += self.give_back(rest, "sigterm")
                    break
                if task[0] in self.in_flight:
                    self.run_task(task)
        self.event("exit", code=0, returned=returned, reason="sigterm")
        sys.exit(0)


if __name__ == "__main__":
    Worker().main()
