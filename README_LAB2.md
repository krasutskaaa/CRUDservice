# Лабораторна 2: метрики, Prometheus, межа масштабованості (варіант 12)

Пул = 5, бакети Г3, додаткова метрика М2. Продовжує лабораторну 1 (той самий сервіс `/admissions`).

## Запуск (Windows, cmd, з кореня репозиторію)

    copy .env.example .env        (додай DB_POOL_SIZE=5, PROM_PORT=9090)
    pip install matplotlib        (лише для аналізу)
    docker compose down -v        (старий том з іншим паролем краще видалити)
    set WEB_REPLICAS=1
    docker compose up -d --build

Prometheus: http://127.0.0.1:9090 (Status → Targets: усі цілі `web` UP).

## Порядок вимірювань

    python scripts\check_graceful.py          # docker stop під навантаженням, 2 інстанси → results\lab2\graceful.json
    python scripts\run_lab2.py --noop         # межа генератора G → results\lab2\G.json
    python scripts\run_lab2.py --instances 1
    python scripts\run_lab2.py --instances 2
    python scripts\run_lab2.py --instances 3
    python scripts\run_lab2.py --instances 4
    python scripts\analyze_lab2.py            # results\lab2\summary_lab2.md і results\lab2\figs\

Якщо X(1) впирається в генератор (≥ 0.9·G) або при N>1 менше двох точок без обмеження генератором —
зменште ліміт CPU у `docker-compose.yml` (`cpus: "0.5"`).

Один прогін триває кілька хвилин (профіль закінчується, коли двічі поспіль досягнуто < 0.9 заданої
або задана досягла G).

## Теги

    git log --oneline                                   # знайдіть коміт, поданий як лаб. 1
    git tag -a lab1 <хеш> -m "Лабораторна робота 1"
    git add . && git commit -m "Лабораторна 2"
    git tag -a lab2 -m "Лабораторна робота 2"
    git push origin main lab1 lab2
