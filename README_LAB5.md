# Лабораторна 5: потокобезпечна структура даних (варіант 12)

Варіант 12: структура **С2**, хеш-таблиця кешу результатів за `record_id`, ключі **0…9999**, **n = 9** потоків
(n_w = 5 записують, n_r = 4 читають). Перед прогоном таблиця заповнена половиною ключів (5000).
Мова: Python (стандартна бібліотека), сторонній пакет лише matplotlib для діаграм.

Кеш продовжує лабораторні 1-4: ключ — `record_id` запису `/admissions`, значення — «результат» `(record_id, потік,
номер запису потоку)`, як рядок `task_results` з лабораторної 4. За цим значенням після прогону перевіряється,
що get повернув саме те, що хтось записав для цього ключа.

## Що додано

| файл | що в ньому |
|---|---|
| `rwcache/table.py` | хеш-таблиця: масив кошиків, ланцюжки вузлів `[ключ, значення, наступний]`, подвоєння при заповненні 0,75; `put`, `get`, перевірки структури |
| `rwcache/locks.py` | `GlobalLock` (один `threading.Lock` на запис і читання), `RWLock` на `threading.Condition` з політиками `readers`, `writers`, `fair` |
| `rwcache/bench.py` | стенд: n потоків, непарні пишуть, парні читають; режими `run`, `x1`, `tcs`, `readonly`; JSON з результатами |
| `scripts/run_lab5.py` | усі прогони: t_cs, X_1, 3 повтори кожного способу, перевірка «читання не змінює полів» |
| `scripts/analyze_lab5.py` | таблиці 1-7 і діаграми → `results/lab5/summary_lab5.md`, `figs/` |

Готові потокобезпечні колекції не використовуються: таблиця побудована на списках Python, синхронізація лише через
`threading.Lock` і `threading.Condition`.

## Порядок вимірювань (Windows, cmd, з кореня репозиторію)

Перед замірами зупинити все інше: `k3d cluster stop lab3`, `docker compose stop`.

    python scripts\run_lab5.py --quick      перевірка стенду (D = 2 с, 1 повтор, results\lab5\quick\)
    python scripts\run_lab5.py              усі прогони, D = 10 с + прогрів 1 с, 3 повтори (~10 хв)
    python scripts\analyze_lab5.py          → results\lab5\summary_lab5.md, analysis.json, figs\

Один прогін вручну:

    python rwcache\bench.py --lock global
    python rwcache\bench.py --lock rw --policy readers
    python rwcache\bench.py --mode x1 --lock global
    python rwcache\bench.py --mode tcs
    python rwcache\bench.py --mode readonly --lock rw --policy readers

Код виходу 2 означає, що порушено інваріант (або в режимі `readonly` таблиця змінилась).

## Тег

    git add .
    git commit -m "Лабораторна 5"
    git tag -a lab5 -m "Лабораторна робота 5"
    git push origin main lab5
