# Звіт. Лабораторна робота 2: метрики сервісу і межа масштабованості (варіант 12)

Продовження лабораторної 1 (`/admissions`, FastAPI + PostgreSQL + nginx). Параметри варіанта:
розмір пулу зʼєднань **5**, набір бакетів **Г3**, додаткова метрика **М2** (postgres-exporter).
Ліміти контейнера web із лабораторної 1 не змінювались: 1 CPU, 512 МБ ⟨ЗАПОВНИТИ⟩.

Умови вимірювань: Windows, Docker Desktop; Docker виділено ⟨ЗАПОВНИТИ⟩; ноутбук: ⟨ЗАПОВНИТИ⟩.
Генератор навантаження і весь стенд працюють на одному ноутбуці, тому на верхніх рівнях вони конкурують за процесор.

## 1. Метрики сервісу

Бібліотека `prometheus_client`. Метрики реєструються один раз при імпорті модуля й оновлюються в ASGI-обробнику `MetricsMiddleware`.
Метрики надаються за `GET /metrics`; сам цей запит не рахується.

| Метрика | Тип | Мітки | Призначення |
|---|---|---|---|
| `http_requests_total` | counter | `method`, `path`, `code` | кількість запитів |
| `http_request_duration_seconds` | histogram | `method`, `path` | тривалість обробки, с |
| `http_requests_in_progress` | gauge | нема | запитів в обробці |
| `process_cpu_seconds_total` та інші `process_*` | counter | нема | CPU процесу (стандартний колектор) |

У мітці `path` стоїть шаблон маршруту (`/admissions/{admission_id}`), а не конкретний ідентифікатор, інакше кількість рядів необмежено зростала б. Невідомі маршрути мають значення `unmatched`.

**Набір бакетів Г3.** У варіанті задано 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024.
За конвенцією Prometheus межі записані в секундах: `le` = 0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128, 0.256, 0.512, 1.024 (і `+Inf`).

## 2. Зміни сервісу

- **`GET /livez`**: 200, до бази не звертається. Docker-healthcheck тепер перевіряє саме його, щоб перевірка не конкурувала із запитами за зʼєднання пулу під навантаженням. Готовність бази, як і раніше, перевіряє `/healthz`.
- **Імʼя екземпляра:** змінна `POD_NAME`, а якщо її не задано, імʼя хоста контейнера; передається в заголовку `X-Pod` кожної відповіді.
- **Пул зʼєднань:** `DB_POOL_SIZE` (за варіантом 5) задає і `min_size`, і `max_size`: пул фіксований, більше 5 зʼєднань на екземпляр не відкривається. Тому на N екземплярах очікується не більше N·5 зʼєднань із сервісу.
- **Коректна зупинка за SIGTERM.** `CMD` у exec-формі: `["python", "/srv/serve.py"]`, тож сигнал отримує процес сервісу (PID 1). `serve.py` запускає uvicorn із власним `GracefulServer`:
  uvicorn припиняє приймати зʼєднання, дочікується запитів в обробці (до 10 с), закриває пул і завершується з кодом 0. Окремий запуск потрібен, бо нові версії uvicorn після graceful-завершення повторно надсилають собі захоплений сигнал, і процес завершується з кодом 143
  (це моє знання поведінки uvicorn: ⟨ЗАПОВНИТИ⟩). У compose `stop_grace_period: 15s`.
- Nginx під час зупинки екземпляра повторює GET на іншому екземплярі (`proxy_next_upstream error timeout http_502 http_503`).

## 3. Конфігурація стенду

### Dockerfile
```dockerfile
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
COPY app/main.py app/serve.py app/healthcheck.py ./
USER 10001:10001
EXPOSE 8000
# exec-форма: SIGTERM отримує сам процес сервісу (PID 1). Один процес на контейнер.
CMD ["python", "/srv/serve.py"]
```

### docker-compose.yml
```yaml
name: admissions-lab

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-admissions}
      POSTGRES_USER: ${POSTGRES_USER:-admissions}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?задайте POSTGRES_PASSWORD у файлі .env}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/001-schema.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 2s
      timeout: 3s
      retries: 30
    networks: [backend]

  web:
    build: .
    image: admissions-web:lab2
    environment:
      DB_HOST: db
      DB_PORT: "5432"
      DB_NAME: ${POSTGRES_DB:-admissions}
      DB_USER: ${POSTGRES_USER:-admissions}
      DB_PASSWORD: ${POSTGRES_PASSWORD:?задайте POSTGRES_PASSWORD у файлі .env}
      DB_POOL_SIZE: ${DB_POOL_SIZE:-5}
      PORT: "8000"
    depends_on:
      db:
        condition: service_healthy
    healthcheck:
      # без curl і wget; /livez не звертається до бази
      test: ["CMD", "python", "/srv/healthcheck.py"]
      interval: 3s
      timeout: 3s
      retries: 10
      start_period: 3s
    stop_grace_period: 15s
    deploy:
      replicas: ${WEB_REPLICAS:-1}
      resources:
        limits:
          cpus: "1"              # ліміти з лабораторної 1 не змінюються
          memory: 512M
    networks: [backend]

  lb:
    image: nginx:1.27-alpine
    ports:
      - "${LB_PORT:-8080}:80"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      web:
        condition: service_healthy
    networks: [backend]

  postgres-exporter:
    image: quay.io/prometheuscommunity/postgres-exporter:v0.15.0
    command: ["--extend.query-path=/etc/postgres_exporter/queries.yaml"]
    environment:
      DATA_SOURCE_URI: db:5432/${POSTGRES_DB:-admissions}?sslmode=disable
      DATA_SOURCE_USER: ${POSTGRES_USER:-admissions}
      DATA_SOURCE_PASS: ${POSTGRES_PASSWORD:?задайте POSTGRES_PASSWORD у файлі .env}
    volumes:
      - ./postgres-exporter/queries.yaml:/etc/postgres_exporter/queries.yaml:ro
    depends_on:
      db:
        condition: service_healthy
    networks: [backend]

  prometheus:
    image: prom/prometheus:v2.55.1
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=7d
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - promdata:/prometheus
    ports:
      - "${PROM_PORT:-9090}:9090"
    networks: [backend]

networks:
  backend:

volumes:
  pgdata:
  promdata:
```

### prometheus/prometheus.yml
```yaml
global:
  scrape_interval: 5s
  evaluation_interval: 5s

scrape_configs:
  # усі екземпляри web: DNS-запит за імʼям сервісу Compose повертає адреси всіх контейнерів
  - job_name: web
    metrics_path: /metrics
    dns_sd_configs:
      - names: ["web"]
        type: A
        port: 8000
        refresh_interval: 5s

  # postgres-exporter (додаткова метрика варіанта М2)
  - job_name: postgres
    static_configs:
      - targets: ["postgres-exporter:9187"]
```

### postgres-exporter/queries.yaml
```yaml
# Кількість клієнтських зʼєднань з базою за станом (active, idle, ...); зʼєднання експортера виключено.
pg_connections:
  query: |
    SELECT COALESCE(state, 'other') AS state, count(*) AS count
    FROM pg_stat_activity
    WHERE datname = current_database()
      AND backend_type = 'client backend'
      AND pid <> pg_backend_pid()
    GROUP BY 1
  metrics:
    - state:
        usage: "LABEL"
        description: "Стан зʼєднання"
    - count:
        usage: "GAUGE"
        description: "Кількість зʼєднань у цьому стані"
```

### nginx/nginx.conf
```nginx
worker_processes auto;
worker_rlimit_nofile 32768;
events { worker_connections 8192; }

http {
    # up= показує, яка саме репліка обслужила запит (підтвердження розподілу)
    log_format lb '$remote_addr [$time_local] "$request" $status '
                  'rt=$request_time urt=$upstream_response_time up=$upstream_addr';
    access_log /dev/stdout lb;

    # Імʼя web розвʼязується Docker DNS у всі адреси реплік під час старту nginx.
    # Після зміни кількості реплік потрібен `docker compose restart lb`.
    upstream web_pool {
        server web:8000 max_fails=0;
        keepalive 64;
    }

    server {
        listen 80;

        # Ендпоінт, який нічого не робить: для перевірки самого генератора
        location = /noop {
            access_log off;
            return 204;
        }

        location / {
            proxy_pass http://web_pool;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_connect_timeout 2s;
            proxy_read_timeout 30s;
            proxy_next_upstream error timeout http_502 http_503;
            proxy_next_upstream_tries 3;
            add_header X-Upstream $upstream_addr always;
        }
    }
}
```

Prometheus збирає метрики кожні 5 с. Екземпляри web знаходяться через `dns_sd_configs` (`type: A`, порт 8000, `refresh_interval: 5s`): DNS-запит за імʼям сервісу `web` повертає адреси всіх контейнерів.
Дані Prometheus лежать в іменованому томі `promdata`. Додатково кожен прогін зберігає потрібні ряди в `results/lab2/prom_*.json`, тому діаграми можна будувати й після видалення томів.

**Перевірка Targets** (`WEB_REPLICAS=4 docker compose up -d`, `docker compose restart lb`, http://127.0.0.1:9090/targets):
⟨ЗАПОВНИТИ⟩

## 4. Коректна зупинка під навантаженням

Перевірка: `python scripts/check_graceful.py` (2 екземпляри, 300 req/s, на 20-й секунді `docker stop` одного з контейнерів; вікно ±5 с навколо зупинки).
Код завершення контейнера (`docker inspect --format '{{.State.ExitCode}}'`): ⟨ЗАПОВНИТИ⟩.
Відповідей не 2xx у вікні: ⟨ЗАПОВНИТИ⟩; помилок зʼєднання: ⟨ЗАПОВНИТИ⟩. Результат: `results/lab2/graceful.json`.

## 5. Генератор навантаження і межа G

`loadgen/stepload.py`: відкритий контур, асинхронний клієнт (asyncio), задана інтенсивність ділиться між процесами за кількістю ядер.
Профіль: прогрів 10 с на 200 req/s, далі сходинки по 20 с від 200 req/s з кроком 200 req/s. Моменти відправки обчислюються наперед від початку прогону, затримка рахується від запланованого моменту.
Тайм-аут запиту 5 с (від запланованого моменту); запит, що його перевищив, рахується відповіддю не 2xx. Помилки зʼєднання (скидання, відмова) рахуються окремо.
Досягнута інтенсивність секунди: кількість відповідей 2xx, завершених у цю секунду. Для сходинки береться середнє за останні 15 с (перші 5 с перехідні). Посекундний запис: `results/lab2/run_*_seconds.csv` (задана, фактична відправка, досягнута, 2xx, не-2xx, помилки зʼєднання, p50/p95/p99, CPU генератора).
Зупинка прогону: двічі поспіль досягнута < 0.9 заданої, або після сходинки, задана інтенсивність якої досягла G (так я прочитав формулювання «сходинка, що досягла межі G»), або після 60 сходинок.

**Межа генератора G:** `python scripts/run_lab2.py --noop` (nginx відповідає 204 на `/noop` без звернення до web, через той самий порт 8080, без обмеження межею G).
G = ⟨ЗАПОВНИТИ⟩ req/s (розкид ⟨ЗАПОВНИТИ⟩, сходинка ⟨ЗАПОВНИТИ⟩).

## 6. Результати прогонів на 1, 2, 3 і 4 екземплярах

Команди: `python scripts/run_lab2.py --instances N`, N = 1..4. Таблиця з `results/lab2/summary_lab2.md` (`python scripts/analyze_lab2.py`):

⟨ЗАПОВНИТИ⟩

Точка N вважається обмеженою генератором, якщо X(N) ≥ 0.9·G або на останній сходинці досягнута інтенсивність не нижча за 0.9 заданої. Такі точки не входять у підбір.
p99 сервера оцінений за приростами бакетів на вікні сходинки (сума по всіх екземплярах, найближчі до меж вікна збори Prometheus, тобто точність по часу близько ±2.5 с).

## 7. Діаграми

Для прогонів на 1 і 4 екземплярах (`results/lab2/figs/`):

- інтенсивність і p99 клієнта/сервера: `timeline_n1.png`, `timeline_n4.png`;
- запити за секунду, CPU і запити в обробці по екземплярах: `instances_n1.png`, `instances_n4.png`.

⟨ЗАПОВНИТИ⟩

## 8. p99 клієнта і p99 сервера на одному екземплярі

⟨ЗАПОВНИТИ⟩

Висновок: ⟨ЗАПОВНИТИ⟩
(p99 клієнта рахується від запланованого моменту відправки й містить час у генераторі, мережі й черзі балансувальника. p99 сервера міряє лише час обробки запиту в процесі сервісу, тому за перевантаження p99 клієнта зростає на порядки швидше. Якщо ранг p99 лежить за межею 1.024 с, оцінка подається як «> 1024 мс» без верхньої границі похибки.)

## 9. Універсальний закон масштабованості

Формули (див. завдання): C(N) = X(N)/X(1); y = N/C − 1; u = N − 1; v = N(N − 1);
σ = (Σv²·Σuy − Σuv·Σvy) / (Σu²·Σv² − (Σuv)²); κ = (Σu²·Σvy − Σuv·Σuy) / (Σu²·Σv² − (Σuv)²);
N* = √((1 − σ)/κ) при κ > 0 і σ < 1.

⟨ЗАПОВНИТИ⟩

Діаграма: `results/lab2/figs/usl.png` (виміряні X(N) з розкидом, крива закону, пряма λN, λ = X(1)).
Інтерпретація: ⟨ЗАПОВНИТИ⟩

## 10. Спільний ресурс, що обмежує зростання X(N)

Використання ресурсів на сходинці X(4) (`docker stats --no-stream` під час прогону + CPU генератора):

⟨ЗАПОВНИТИ⟩

**М2: зʼєднання з базою і транзакції** (postgres-exporter; зʼєднання за станом порівнюються з N·5; транзакції за секунду це сума commit і rollback з `pg_stat_database`):

⟨ЗАПОВНИТИ⟩

Діаграма: `results/lab2/figs/m2.png`. Висновок про спільний ресурс: ⟨ЗАПОВНИТИ⟩

## 11. Відкинуті рішення

1. **`BaseHTTPMiddleware` Starlette для метрик.** Відхилено: додає накладні витрати на кожен запит і працює повільніше за чистий ASGI-обробник; при ліміті 1 CPU це зменшило б виміряну пропускну здатність. Обрано ASGI-обробник.
2. **Запуск сервісу через `uvicorn` у `CMD` без власного обробника сигналів.** Відхилено: за відомостями про нові версії uvicorn, процес після graceful-завершення завершується з кодом 143, а завдання вимагає код 0. Обрано `serve.py` без повторного надсилання сигналу.
3. **Генератор лабораторної 1 (потік на запит) для ступінчастого профілю.** Відхилено: під перевантаженням у польоті опиняється rate × тайм-аут запитів (тисячі), потоків стільки не створити, і фактична інтенсивність відправки падала б нижче заданої. Обрано asyncio.
4. **Конкретний `id` у мітці `path`.** Відхилено: кардинальність рядів необмежена. Використано шаблон маршруту.
5. **Динамічний пул (`min_size` < `max_size`).** Відхилено: для порівняння кількості зʼєднань із N·5 (М2) потрібен фіксований пул.
6. **Healthcheck контейнера через `/healthz`.** Відхилено для лабораторної 2: під навантаженням перевірка конкурує із запитами за зʼєднання пулу. Використано `/livez`.
7. **Вбудовані запити postgres-exporter для кількості зʼєднань.** Відхилено: набір вбудованих метрик залежить від версії експортера. Використано власний запит у `queries.yaml`.

## 12. Використання генеративних моделей

Модель (Claude) використана для: шаблону цього звіту.
Числа в таблицях і висновки в розділах 6-10 беруться з моїх прогонів (`results/lab2/`).

## 13. Теги git

Стан лабораторної 1 позначено тегом `lab1` (поставлений пізніше на коміт здачі лабораторної 1), стан лабораторної 2 тегом `lab2`.
⟨ЗАПОВНИТИ⟩
