# Лабораторна 6: розподілене навчання нейромережі за даними на CPU (варіант 12)

Варіант 12: датасет **Д6** (OCTMNIST, K = 4 класи, C = 1 канал), ширина **Ш1** (c1 / c2 / h = 16 / 32 / 128),
**P_max = 4**, **B = 384**, контрольна точка кожні **K_c = 10** кроків, відмова на кроці **s_f = 25**.
PyTorch для CPU, бекенд gloo, процеси через torchrun, цикл навчання власний (без Trainer, accelerate, Lightning).

## Що додано

| файл | що в ньому |
|---|---|
| `ddp/common.py` | варіант, завантаження `data/octmnist.npz` з Zenodo, `load_split`, мережа `Net`, ACC і AUC |
| `ddp/train.py` | навчання: `--sync none / manual / ddp / ddp_nosync`, `--task speed / quality / equiv`, контрольні точки і відмова процесу |
| `ddp/torchrun_win.py` | запуск torchrun на Windows: TCPStore без libuv і зупинка воркерів через TerminateProcess замість CTRL_C |
| `scripts/run_lab6.py` | усі прогони кроків 1-5, кожен окремим запуском, JSON у `results/lab6/` |
| `scripts/analyze_lab6.py` | таблиці 1-6 і графіки → `results/lab6/summary_lab6.md`, `analysis.json`, `figs/` |

`data/` і `*.pt` додано в `.gitignore`: датасет завантажується скриптом, контрольні точки тимчасові.

## Встановлення

    python -m pip install torch numpy scikit-learn matplotlib

## Порядок вимірювань (Windows, cmd, з кореня репозиторію)

Перед замірами зупинити все інше: `k3d cluster stop lab3`, `docker compose stop`.

    python scripts\run_lab6.py --quick      перевірка стенду (1 повтор, results\lab6\quick\)
    python scripts\run_lab6.py              усі прогони, 3 повтори вимірів швидкості (~17 хв)
    python scripts\analyze_lab6.py          → results\lab6\summary_lab6.md, analysis.json, figs\

Частину можна повторити окремо: `--only quality`, `--only speed`, `--only fault`.

Один прогін вручну (Linux; на Windows замість `-m torch.distributed.run` — `ddp\torchrun_win.py`):

    python ddp/train.py --task quality --sync none --batch 384
    python -m torch.distributed.run --standalone --nproc_per_node=4 ddp/train.py --task equiv --sync manual
    python -m torch.distributed.run --standalone --nproc_per_node=4 ddp/train.py --task speed --sync ddp
    python -m torch.distributed.run --nnodes=1 --nproc_per_node=4 --max-restarts=1 --rdzv-backend=static \
        --master-addr=127.0.0.1 --master-port=29500 --node-rank=0 ddp/train.py --task quality --sync ddp \
        --ckpt-every 10 --fail-step 25 --workdir results/lab6/fault

## Тег

    git add .
    git commit -m "Лабораторна 6"
    git tag -a lab6 -m "Лабораторна робота 6"
    git push origin main lab6
