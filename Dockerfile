# syntax=docker/dockerfile:1

# ---------- Стадія 1: збірка залежностей ----------
FROM python:3.12-slim AS builder
WORKDIR /build
COPY app/requirements.txt .
# Усі пакети мають готові колеса (psycopg[binary], uvloop, httptools),
# тому компілятор не потрібен навіть тут.
RUN pip install --no-cache-dir --target=/install -r requirements.txt

# ---------- Стадія 2: фінальний образ ----------
FROM python:3.12-slim AS final
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/deps \
    PORT=8000
# Із фінальної стадії прибираємо засоби збірки пакетів
RUN python -m pip uninstall -y pip setuptools wheel || true
COPY --from=builder /install /opt/deps
WORKDIR /srv
COPY app/main.py app/healthcheck.py ./
USER 10001:10001
EXPOSE 8000
# Один воркер на контейнер: контейнеру видається 1 CPU, масштабуємо репліками.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--loop", "uvloop", "--http", "httptools", "--no-access-log"]
