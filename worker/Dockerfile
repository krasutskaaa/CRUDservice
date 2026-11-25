# syntax=docker/dockerfile:1
# Образ воркера лабораторної 4. Збірка з кореня репозиторію:
#   docker build -f worker/Dockerfile -t admissions-worker:lab4 .
FROM python:3.12-slim AS builder
COPY worker/requirements.txt .
RUN pip install --no-cache-dir --target=/install -r requirements.txt

FROM python:3.12-slim AS final
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/deps
RUN python -m pip uninstall -y pip setuptools wheel || true
COPY --from=builder /install /opt/deps
WORKDIR /srv
COPY worker/worker.py worker/slice_proc.py ./
USER 10001:10001
EXPOSE 9100
# exec-форма: SIGTERM отримує сам воркер (PID 1)
CMD ["python", "/srv/worker.py"]
