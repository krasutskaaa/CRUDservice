# Лабораторна робота 1. CRUD-сервіс «Епізод госпіталізації» (варіант 12)

Стек: Python 3.12 + FastAPI + psycopg 3 (пул) + PostgreSQL 16 + nginx.
Звіт: [REPORT.md](REPORT.md).

## Запуск (одна команда)

```bash
cp .env.example .env        # один раз; задайте POSTGRES_PASSWORD
docker compose up -d --build
```

Балансувальник слухає `http://127.0.0.1:8080` (єдиний опублікований порт).
Два вебінстанси замість одного, без правок файлів:

```bash
WEB_REPLICAS=2 docker compose up -d --build
```

Якщо кількість реплік змінюється на вже запущеному стенді, після цього:
`docker compose restart lb` (nginx розвʼязує імена реплік під час старту).

Зупинка: `docker compose down` (дані лишаються), `docker compose down -v` (база порожня).

## Вимоги до хоста

Docker з Compose v2, Python 3.12 (лише для скриптів у `scripts/` та `loadgen/`, сторонніх пакетів вони не потребують).

## Порядок вимірювань

```bash
docker compose build
python scripts/measure_startup.py                 # розмір образу + медіана часу до /healthz
python scripts/verify_cycle.py --persistence      # етап 9, журнал у results/verify_log.md

python scripts/run_series.py --instances 1 --selfcheck   # перевірка генератора проти /noop
python scripts/run_series.py --instances 1               # 3 рівні x 3 прогони, 1 інстанс
python scripts/run_series.py --instances 2               # те саме, 2 інстанси
python scripts/lb_distribution.py --since 30m            # розподіл між репліками (при 2 інстансах)

python scripts/calc.py                                   # таблиця + три розрахунки
python scripts/run_closed.py --instances 1 --level <рівень із calc.py>
python scripts/calc.py                                   # ще раз: з закритим контуром
```

Сирі результати: `results/raw/` (JSON і `.csv.gz` з кожним запитом).
