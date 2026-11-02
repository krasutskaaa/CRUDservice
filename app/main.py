"""Сервіс обліку епізодів госпіталізації: CRUD над однією таблицею PostgreSQL.
Стану в памʼяті процесу немає, усе лежить у базі."""
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

import psycopg
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000_000
MAX_BIGINT = 2**63 - 1
OUTCOMES = ("на лікуванні", "одужання", "покращення", "переведення", "летальний наслідок")
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
    min_size=int(os.environ.get("POOL_MIN", "2")),
    max_size=int(os.environ.get("POOL_MAX", "10")),
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
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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
