# Лабораторна 4: синхронізація пула воркерів (варіант 12)

Кластер k3d і сервіс web з лабораторної 3 (простір імен `lab3`, R = 3), до них додано чергу задач у PostgreSQL
і воркер окремим образом. Параметри варіанта 12: q_lo / q_hi = **0,01 / 0,99**, T_poll = **50 мс**,
L = **15 с**, сценарій відмови **Ф5**, додаткова вимога **Д4** (захоплення пакетом), S = 256, R = 3.

## Що додано

| файл | що в ньому |
|---|---|
| `db/002_tasks.sql` | міграція: таблиці `tasks`, `task_results`, журнал `task_events`, `worker_config`; індекс `(status, created_at)` |
| `k8s/migrate-job.yaml` | Job міграції тепер виконує `init.sql`, далі `0*.sql` за порядком імен |
| `app/main.py` | `POST /admissions/{id}/tasks`, `GET /tasks/{id}`, `GET /tasks`; вимірювачі `tasks_queued`, `tasks_running` (запит до бази кожні 5 с) |
| `worker/worker.py`, `worker/slice_proc.py`, `worker/Dockerfile` | воркер: захоплення (naive / FOR UPDATE / SKIP LOCKED, пакет b), lease, heartbeat, fencing token, ідемпотентний запис, SIGTERM, FAIL_RATE; обробка зрізу без NumPy |
| `k8s/worker.yaml` | ConfigMap `worker-config` і Deployment `worker` без `replicas` (250m / 500m / 512Mi, grace 30 с, WORKER_ID з імені пода) |
| `k8s/monitoring/prometheus.yaml` | новий job `worker` (поди `app=worker`, порт 9100) |
| `loadgen/stepload.py` | режим створення задач `--tasks-rate λ --tasks-count n` або `--tasks-duration D` |
| `scripts/run_lab4.py`, `scripts/analyze_lab4.py` | прогони кроків 1-10 і підсумок |

## Розгортання (Windows, cmd, з кореня репозиторію)

    python scripts\k8s_up.py           (кластер з лаб. 3 + образ воркера + міграція + Deployment worker)

Якщо кластер лабораторної 3 уже запущений: `python scripts\k8s_up.py --no-cluster`
(перезбирає обидва образи, накочує міграцію і застосовує всі маніфести).

## Порядок вимірювань

    python scripts\run_lab4.py step1              крок 1: ендпоінти і /metrics
    python scripts\run_lab4.py step2              крок 2: 20 задач на 1 воркері, t_io і t_cpu
    python scripts\run_lab4.py step3              крок 3: наївне захоплення, T_poll = 50 і 1 мс
    python scripts\run_lab4.py step4              крок 4: SKIP LOCKED і FOR UPDATE
    python scripts\run_lab4.py step5              крок 5: kill -STOP на 2·L = 30 с
    python scripts\run_lab4.py step6              крок 6: W = 1, 2, 3
    python scripts\run_lab4.py step7              крок 7: закон Літтла (5 хв)
    python scripts\run_lab4.py step8              крок 8: rollout restart під навантаженням
    python scripts\run_lab4.py step9              крок 9: FAIL_RATE = 0,15 і Ф5 (5 хв + дообробка)
    python scripts\run_lab4.py d4                 крок 10: Д4, b = 1 і b = 10
    python scripts\run_lab4.py restore            налаштування за замовчуванням
    python scripts\analyze_lab4.py                → results\lab4\summary_lab4.md, results\lab4\figs\

Режими воркера (claim_mode, store_mode, t_poll_ms, lease_s, heartbeat, batch) скрипт перемикає через таблицю
`worker_config`, яку воркер перечитує раз на секунду; значення за замовчуванням беруться зі змінних середовища
(`k8s/worker.yaml`). FAIL_RATE у кроці 9 задається в Deployment (`kubectl set env`).

## Тег

    git add .
    git commit -m "Лабораторна 4"
    git tag -a lab4 -m "Лабораторна робота 4"
    git push origin main lab4
