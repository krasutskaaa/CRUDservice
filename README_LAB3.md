# Лабораторна 3: сервіс у Kubernetes з автомасштабуванням і без нього (варіант 12)

R = 3, кластер k3d (1 server + 2 agent), CPU requests / limits 250m / 500m, ціль HPA 70 %,
рівень стрибка 2,5·X(1), додаткова вимога Д2. Розмір пулу 5 (з лабораторної 2).
Сервіс той самий, що в лабораторних 1 і 2 (`/admissions`).

## Що потрібно

Docker Desktop, k3d, kubectl, Python 3 (matplotlib лише для аналізу). Helm не потрібен:
моніторинг розгортається звичайними маніфестами з `k8s/monitoring/`.

## Розгортання і видалення (Windows, cmd, з кореня репозиторію)

    copy .env.example .env             (задайте POSTGRES_PASSWORD; файл .env не комітиться)
    python scripts\k8s_up.py           кластер, мітка role=infra, збірка й імпорт образу, усі маніфести, моніторинг
    python scripts\k8s_down.py         повне видалення кластера разом із томами

web: http://127.0.0.1:8080, Prometheus: http://127.0.0.1:9090.

| файл | що в ньому |
|---|---|
| `k3d/cluster.yaml` | кластер: server з taint NoSchedule, 2 agent, порти 8080→30080 і 9090→30090 на server:0 |
| `k8s/db.yaml` | PostgreSQL: ConfigMap, headless Service, StatefulSet з PVC, `max_connections` з ConfigMap |
| `k8s/migrate-job.yaml` | Job міграції схеми (`db/init.sql` через ConfigMap) |
| `k8s/web.yaml` | ConfigMap, Deployment без `replicas`, Service NodePort |
| `k8s/hpa.yaml` | HPA (лише для кроку 11) |
| `k8s/monitoring/` | Prometheus, kube-state-metrics, postgres-exporter |

## Порядок вимірювань

    python scripts\run_lab3.py db-persist            крок 3
    python scripts\run_lab3.py crud                  крок 4
    python scripts\run_lab3.py monitoring            крок 5
    python scripts\run_lab3.py steps --replicas 1    крок 7
    python scripts\run_lab3.py steps --replicas 2
    python scripts\run_lab3.py steps --replicas 3 --run 1
    python scripts\run_lab3.py steps --replicas 3 --run 2
    python scripts\run_lab3.py steps --replicas 3 --run 3
    python scripts\run_lab3.py profile --mode fixed  крок 10
    python scripts\run_lab3.py profile --mode hpa    крок 11
    python scripts\run_lab3.py d2                    крок 13 (Д2)
    python scripts\run_lab3.py restore               повернути max_connections = 100 і DB_POOL_SIZE = 5
    python scripts\analyze_lab3.py                   кроки 8, 9, 12 → results\lab3\summary_lab3.md, results\lab3\figs\

Генератор (`loadgen/stepload.py`) у всіх прогонах лабораторної 3 тримає 8·R = 24 постійні зʼєднання
і закриває кожне після 100 запитів (`--conns 24 --reqs-per-conn 100`); сходинка 30 с, середнє за останні 20 с.

## Тег

    git add .
    git commit -m "Лабораторна 3"
    git tag -a lab3 -m "Лабораторна робота 3"
    git push origin main lab3
