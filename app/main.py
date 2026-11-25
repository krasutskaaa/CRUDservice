"""Сервіс обліку епізодів госпіталізації: CRUD над однією таблицею PostgreSQL.
Стану в памʼяті процесу немає, усе лежить у базі.
Лабораторна 4: створення задач обробки запису (черга tasks у тій самій базі) і їхній стан."""
import asyncio
import os
import random
import socket
import time
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

import psycopg
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000_000
MAX_BIGINT = 2**63 - 1
OUTCOMES = ("на лікуванні", "одужання", "покращення", "переведення", "летальний наслідок")
# Розмір пулу: найбільша кількість відкритих зʼєднань екземпляра (за варіантом 5).
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "5"))
POD_NAME = os.environ.get("POD_NAME") or socket.gethostname()
# Набір бакетів Г3: 1, 2, 4, ..., 1024 мс, у секундах за конвенцією Prometheus.
BUCKETS = (0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128, 0.256, 0.512, 1.024)

REQUESTS = Counter("http_requests_total", "Кількість запитів", ["method", "path", "code"])
DURATION = Histogram("http_request_duration_seconds", "Тривалість обробки запиту, с",
                     ["method", "path"], buckets=BUCKETS)
IN_PROGRESS = Gauge("http_requests_in_progress", "Запитів в обробці")
TASKS_QUEUED = Gauge("tasks_queued", "Задач у статусі queued (запит до бази кожні 5 с)")
TASKS_RUNNING = Gauge("tasks_running", "Задач у статусі running (запит до бази кожні 5 с)")
TASK_GAUGE_PERIOD = float(os.environ.get("TASK_GAUGE_PERIOD", "5"))

COLS = "id, patient_code, department, room, admission_date, discharge_date, outcome"


def _conninfo() -> str:
    return (
        f"host={os.environ.get('DB_HOST', 'localhost')} "
        f"port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ.get('DB_NAME', 'admissions')} "
        f"user={os.environ.get('DB_USER', 'admissions')} "
        f"password={os.environ.get('DB_PASSWORD', '')} "
        "connect_timeout=3"
    )


pool = AsyncConnectionPool(
    conninfo=_conninfo(),
    min_size=POOL_SIZE,   # фіксований пул: понад POOL_SIZE зʼєднань не відкривається
    max_size=POOL_SIZE,
    open=False,
    timeout=5,
    check=AsyncConnectionPool.check_connection,
    kwargs={"row_factory": dict_row, "autocommit": True},
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # wait=False: сервіс стартує навіть якщо база ще недоступна,
    # /healthz у цей час чесно віддає 503.
    await pool.open(wait=False)
    refresher = asyncio.create_task(refresh_task_gauges())
    yield
    refresher.cancel()
    await pool.close()


async def refresh_task_gauges():
    """Кожні TASK_GAUGE_PERIOD с оновлює tasks_queued і tasks_running запитом до бази."""
    while True:
        try:
            async with pool.connection(timeout=2) as conn:
                cur = await conn.execute(
                    "SELECT count(*) FILTER (WHERE status = 'queued') AS q, "
                    "count(*) FILTER (WHERE status = 'running') AS r "
                    "FROM tasks WHERE status IN ('queued', 'running')")
                row = await cur.fetchone()
            TASKS_QUEUED.set(row["q"])
            TASKS_RUNNING.set(row["r"])
        except Exception:  # база недоступна або таблиці ще немає: лишаємо попередні значення
            pass
        await asyncio.sleep(TASK_GAUGE_PERIOD)


class MetricsMiddleware:
    """Чистий ASGI-обробник: рахує запити, міряє тривалість, веде вимірювач запитів
    в обробці і додає до кожної відповіді заголовок X-Pod."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500
        measured = scope["path"] != "/metrics"

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message,
                           "headers": [*message.get("headers", []), (b"x-pod", POD_NAME.encode())]}
            await send(message)

        if measured:
            IN_PROGRESS.inc()
        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if measured:
                elapsed = time.perf_counter() - start
                route = scope.get("route")  # шаблон шляху, а не конкретний id
                path = getattr(route, "path", None) or "unmatched"
                IN_PROGRESS.dec()
                REQUESTS.labels(scope["method"], path, str(status)).inc()
                DURATION.labels(scope["method"], path).observe(elapsed)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(MetricsMiddleware)


# ---------- моделі та валідація ----------
class AdmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    patient_code: str = Field(min_length=1, max_length=32)
    department: str = Field(min_length=1, max_length=64)
    room: str = Field(min_length=1, max_length=16)
    admission_date: date
    discharge_date: Optional[date] = None
    outcome: str

    @field_validator("patient_code", "department", "room")
    @classmethod
    def no_nul(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("нульовий символ у рядку недопустимий")
        return v

    @field_validator("admission_date", "discharge_date", mode="before")
    @classmethod
    def iso_string_only(cls, v):
        if v is not None and not isinstance(v, str):
            raise ValueError("очікується рядок у форматі YYYY-MM-DD")
        return v

    @field_validator("discharge_date")
    @classmethod
    def discharge_not_before_admission(cls, v, info: ValidationInfo):
        adm = info.data.get("admission_date")
        if v is not None and adm is not None and v < adm:
            raise ValueError("дата виписки не може бути ранішою за дату надходження")
        return v

    @field_validator("outcome")
    @classmethod
    def known_outcome(cls, v: str) -> str:
        if v not in OUTCOMES:
            raise ValueError("допустимі значення: " + ", ".join(OUTCOMES))
        return v


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    details = []
    for e in exc.errors():
        if e.get("type") == "json_invalid":
            details.append({"field": "body", "message": "тіло запиту не є коректним JSON"})
            continue
        loc = [str(p) for p in e["loc"] if p not in ("body", "query", "path")]
        msg = e["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        details.append({"field": ".".join(loc) or "body", "message": msg})
    return JSONResponse({"error": "validation_error", "details": details}, status_code=400)


@app.exception_handler(psycopg.OperationalError)  # включно з PoolTimeout
async def db_unavailable_handler(_: Request, __: Exception):
    return JSONResponse({"error": "database_unavailable"}, status_code=503)


# ---------- допоміжне ----------
def row_out(r: dict) -> dict:
    return {
        **r,
        "admission_date": r["admission_date"].isoformat(),
        "discharge_date": r["discharge_date"].isoformat() if r["discharge_date"] else None,
    }


def parse_id(raw: str) -> Optional[int]:
    if not (raw.isascii() and raw.isdigit()):
        return None
    v = int(raw)
    return v if 1 <= v <= MAX_BIGINT else None


def not_found() -> JSONResponse:
    return JSONResponse({"error": "not_found", "message": "запис не знайдено"}, status_code=404)


# ---------- ендпоінти ----------
@app.get("/livez")
async def livez():
    """Живість процесу: до бази не звертається."""
    return {"status": "alive"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def healthz():
    try:
        async with pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@app.post("/admissions")
async def create_admission(body: AdmissionIn):
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"INSERT INTO admissions (patient_code, department, room, admission_date, "
            f"discharge_date, outcome) VALUES (%s, %s, %s, %s, %s, %s) RETURNING {COLS}",
            (body.patient_code, body.department, body.room, body.admission_date,
             body.discharge_date, body.outcome),
        )
        row = await cur.fetchone()
    return JSONResponse(row_out(row), status_code=201,
                        headers={"Location": f"/admissions/{row['id']}"})


@app.get("/admissions")
async def list_admissions(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    department: Optional[str] = Query(None, min_length=1, max_length=64),
):
    where, params = "", []
    if department is not None:
        where, params = "WHERE department = %s", [department]
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT count(*) AS n FROM admissions {where}", params)
        total = (await cur.fetchone())["n"]
        cur = await conn.execute(
            f"SELECT {COLS} FROM admissions {where} ORDER BY id LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = await cur.fetchall()
    return {"items": [row_out(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


@app.get("/admissions/{admission_id}")
async def get_admission(admission_id: str):
    i = parse_id(admission_id)
    if i is None:
        return not_found()
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT {COLS} FROM admissions WHERE id = %s", (i,))
        row = await cur.fetchone()
    return row_out(row) if row else not_found()


@app.put("/admissions/{admission_id}")
async def replace_admission(admission_id: str, body: AdmissionIn):
    i = parse_id(admission_id)
    if i is None:
        return not_found()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE admissions SET patient_code=%s, department=%s, room=%s, "
            "admission_date=%s, discharge_date=%s, outcome=%s "
            f"WHERE id=%s RETURNING {COLS}",
            (body.patient_code, body.department, body.room, body.admission_date,
             body.discharge_date, body.outcome, i),
        )
        row = await cur.fetchone()
    return row_out(row) if row else not_found()


@app.delete("/admissions/{admission_id}")
async def delete_admission(admission_id: str):
    i = parse_id(admission_id)
    if i is None:
        return not_found()
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM admissions WHERE id = %s", (i,))
        deleted = cur.rowcount
    return Response(status_code=204) if deleted else not_found()


# ---------- лабораторна 4: задачі обробки ----------
TASK_COLS = ("id, record_id, status, seed, attempts, max_attempts, token, lease_until, worker_id, "
             "created_at, started_at, finished_at")
TASK_STATUSES = ("queued", "running", "done", "failed")


def task_out(r: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}


@app.post("/admissions/{admission_id}/tasks")
async def create_task(admission_id: str):
    i = parse_id(admission_id)
    if i is None:
        return not_found()
    seed = random.randint(1, 2**31 - 1)  # seed генерує сервіс; 0 не видається
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"INSERT INTO tasks (record_id, seed) SELECT id, %s FROM admissions WHERE id = %s "
            f"RETURNING {TASK_COLS}", (seed, i))
        row = await cur.fetchone()
    if row is None:
        return not_found()
    return JSONResponse(task_out(row), status_code=202, headers={"Location": f"/tasks/{row['id']}"})


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    i = parse_id(task_id)
    if i is None:
        return JSONResponse({"error": "not_found", "message": "задачу не знайдено"}, status_code=404)
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT {TASK_COLS} FROM tasks WHERE id = %s", (i,))
        row = await cur.fetchone()
        if row is None:
            return JSONResponse({"error": "not_found", "message": "задачу не знайдено"}, status_code=404)
        cur = await conn.execute(
            "SELECT token, worker_id, result, created_at FROM task_results WHERE task_id = %s "
            "ORDER BY created_at LIMIT 1", (i,))
        res = await cur.fetchone()
    out = task_out(row)
    out["result"] = task_out(res) if res else None
    return out


@app.get("/tasks")
async def list_tasks(
    status: Optional[str] = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
):
    if status is not None and status not in TASK_STATUSES:
        return JSONResponse({"error": "validation_error", "details": [
            {"field": "status", "message": "допустимі значення: " + ", ".join(TASK_STATUSES)}]}, status_code=400)
    where, params = "", []
    if status is not None:
        where, params = "WHERE status = %s", [status]
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT count(*) AS n FROM tasks {where}", params)
        total = (await cur.fetchone())["n"]
        cur = await conn.execute(f"SELECT {TASK_COLS} FROM tasks {where} ORDER BY id LIMIT %s OFFSET %s",
                                 params + [limit, offset])
        rows = await cur.fetchall()
    return {"items": [task_out(r) for r in rows], "total": total, "limit": limit, "offset": offset}
