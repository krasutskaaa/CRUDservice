#!/usr/bin/env python3
"""Навчання CNN на OCTMNIST за даними на CPU (лабораторна 6, варіант 12).

Один процес без torch.distributed:
  python ddp/train.py --task quality --sync none --batch 384
P процесів:
  python -m torch.distributed.run --standalone --nproc_per_node=4 ddp/train.py --task speed --sync ddp --batch 384

--task   speed    10 кроків прогріву + 50 вимірюваних, t_step = медіана часу кроку процесу 0, X = 50 · B / t
         quality  n_ep = 3 епохи, втрата на кожному кроці (середня за всіма процесами), ACC і AUC на test
         equiv    перевірка ḡ = g на першому батчі + сума ваг процесів після 50 кроків (лише --sync manual)
--sync   none (без torch.distributed) | manual (all_reduce по тензорах) | ddp | ddp_nosync (backward у no_sync)
--batch  глобальний батч B; батч процесу b = B / P
Контрольні точки: --ckpt-every K_c --workdir DIR (там ckpt.pt, events.jsonl, losses.jsonl), відмова: --fail-step s_f.
"""
import time

T_START = time.time()          # момент старту процесу, до імпорту torch (для t_r у кроці 5)

import argparse
import json
import os
import platform
import statistics as st
import sys
from contextlib import nullcontext
from pathlib import Path

import torch
import torch.distributed as dist
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, DistributedSampler

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as cm  # noqa: E402


def parse():
    p = argparse.ArgumentParser()
    p.add_argument("--task", choices=["speed", "quality", "equiv"], required=True)
    p.add_argument("--sync", choices=["none", "manual", "ddp", "ddp_nosync"], required=True)
    p.add_argument("--batch", type=int, default=cm.VARIANT["B"], help="глобальний батч B")
    p.add_argument("--lr", type=float, default=cm.LR0)
    p.add_argument("--epochs", type=int, default=cm.N_EP)
    p.add_argument("--threads", type=int, default=1, help="n_th, потоків PyTorch на процес")
    p.add_argument("--ckpt-every", type=int, default=0)
    p.add_argument("--fail-step", type=int, default=0)
    p.add_argument("--workdir", default=None)
    p.add_argument("--out", default=None, help="JSON з результатом (пише процес 0)")
    return p.parse_args()


def main():
    a = parse()
    torch.set_num_threads(a.threads)
    distributed = "WORLD_SIZE" in os.environ
    if a.sync == "none" and distributed and int(os.environ["WORLD_SIZE"]) > 1:
        sys.exit("--sync none лише для одного процесу без torchrun")
    if distributed and a.sync != "none":
        init_group()
        rank, P = dist.get_rank(), dist.get_world_size()
    else:
        distributed = False
        rank, P = 0, 1
    if a.sync != "none" and not distributed:
        sys.exit(f"--sync {a.sync} потребує запуску через torchrun")
    t_group = time.time()
    attempt = int(os.environ.get("TORCHELASTIC_RESTART_COUNT", "0"))
    assert a.batch % P == 0, "B має ділитися на P"
    b = a.batch // P

    wd = Path(a.workdir) if a.workdir else None
    if wd:
        wd.mkdir(parents=True, exist_ok=True)

    def event(**kw):                 # журнал подій процесу 0 (кроки 5: t_c, t_f, t_r)
        if wd and rank == 0:
            with open(wd / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(attempt=attempt, t=time.time(), **kw)) + "\n")

    event(ev="start", t_proc=T_START, t_group=t_group)

    path = cm.npz_path()
    train = cm.load_split(path, "train", n=cm.N_TRAIN)
    test = cm.load_split(path, "test")

    model = cm.make_model()
    res = dict(task=a.task, sync=a.sync, P=P, B=a.batch, b=b, lr=a.lr, n_th=a.threads,
               torch=torch.__version__, python=platform.python_version(), attempt=attempt,
               n_params=sum(p.numel() for p in model.parameters()))

    if a.task == "equiv":
        run_equiv(a, model, train, rank, P, b, res)
        finish(a, res, rank, distributed)
        return

    # відновлення з контрольної точки (до обгортання в DDP; opt створюється нижче)
    ckpt = None
    if wd and (wd / "ckpt.pt").exists():
        ckpt = torch.load(wd / "ckpt.pt", map_location="cpu")
        model.load_state_dict(ckpt["model"])

    if a.sync in ("ddp", "ddp_nosync"):
        net = DDP(model)                       # розсилає параметри процесу 0 і ділить їх на кошики
    else:
        net = model
        if a.sync == "manual":
            for p in model.parameters():
                dist.broadcast(p.data, src=0)
    params = list(model.parameters())
    opt = torch.optim.SGD(params, lr=a.lr, momentum=cm.MOMENTUM)
    start_epoch, skip, step = 0, 0, 0
    if ckpt is not None:
        opt.load_state_dict(ckpt["opt"])
        start_epoch, skip, step = ckpt["epoch"], ckpt["step_in_epoch"], ckpt["step"]
        event(ev="resumed", step=step, epoch=start_epoch, step_in_epoch=skip)
        res["resumed_from_step"] = step

    sampler = DistributedSampler(train, num_replicas=P, rank=rank, shuffle=True, seed=0)
    loader = DataLoader(train, batch_size=b, sampler=sampler, drop_last=True, num_workers=0)
    res["steps_per_epoch"] = len(loader)

    def batches(n_epochs):
        for epoch in range(start_epoch, n_epochs):
            sampler.set_epoch(epoch)
            for i, (xb, yb) in enumerate(loader):
                if epoch == start_epoch and i < skip:
                    continue
                yield epoch, i, xb, yb

    def train_step(xb, yb):
        t0 = time.perf_counter()
        opt.zero_grad(set_to_none=True)
        ctx = net.no_sync() if a.sync == "ddp_nosync" else nullcontext()
        with ctx:
            loss = F.cross_entropy(net(xb), yb)
            loss.backward()
        t_sync = None
        if a.sync == "manual":
            ts = time.perf_counter()
            for p in params:
                dist.all_reduce(p.grad, op=dist.ReduceOp.SUM)
                p.grad /= P
            t_sync = time.perf_counter() - ts
        opt.step()
        return loss, time.perf_counter() - t0, t_sync

    if a.task == "speed":
        n_total = cm.WARMUP_STEPS + cm.MEASURE_STEPS
        times, syncs, t_begin = [], [], None
        for k, (epoch, i, xb, yb) in enumerate(batches(10 ** 6)):
            if k == cm.WARMUP_STEPS:
                t_begin = time.perf_counter()
            _, dt, ts = train_step(xb, yb)
            if k >= cm.WARMUP_STEPS:
                times.append(dt)
                if ts is not None:
                    syncs.append(ts)
            if k + 1 == n_total:
                break
        t = time.perf_counter() - t_begin
        res.update(t_step_ms=st.median(times) * 1e3, t_total_s=t, X=cm.MEASURE_STEPS * a.batch / t,
                   step_ms=[x * 1e3 for x in times])
        if syncs:
            res.update(t_sync_ms=st.median(syncs) * 1e3, sync_ms=[x * 1e3 for x in syncs])
        if isinstance(net, DDP):
            res["ddp_buckets"] = ddp_buckets(net)   # після 1-го кроку DDP перебудовує кошики
        finish(a, res, rank, distributed)
        return

    # task == quality
    losses = []
    t0 = time.perf_counter()
    first_after_resume = ckpt is not None
    for epoch, i, xb, yb in batches(a.epochs):
        if a.fail_step and step + 1 == a.fail_step and rank == P - 1 and wd \
                and not (wd / "failed.marker").exists():
            (wd / "failed.marker").write_text(json.dumps({"t_f": time.time(), "step": step + 1}))
            os._exit(1)                         # аварійне завершення без запису контрольної точки
        loss, _, _ = train_step(xb, yb)
        step += 1
        lg = loss.detach().clone()
        if distributed:
            dist.all_reduce(lg)
            lg /= P
        lv = float(lg)
        losses.append(dict(step=step, epoch=epoch, loss=lv))
        if wd and rank == 0:
            with open(wd / "losses.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(attempt=attempt, step=step, epoch=epoch, loss=lv)) + "\n")
        if first_after_resume:
            event(ev="first_step_after_resume", step=step)
            first_after_resume = False
        if a.ckpt_every and step % a.ckpt_every == 0:
            if rank == 0:
                state = {"model": model.state_dict(), "opt": opt.state_dict(), "epoch": epoch,
                         "step_in_epoch": i + 1, "step": step}
                torch.save(state, wd / "ckpt.tmp")
                os.replace(wd / "ckpt.tmp", wd / "ckpt.pt")
                event(ev="ckpt", step=step)
            if distributed:
                dist.barrier()
    res["train_s"] = time.perf_counter() - t0
    res["steps"] = step
    res["losses"] = losses
    if rank == 0:
        res["acc"], res["auc"] = cm.evaluate(model, test)
        last_ep = a.epochs - 1
        res["max_loss_last_epoch"] = max(x["loss"] for x in losses if x["epoch"] == last_ep)
    event(ev="done", step=step)
    if isinstance(net, DDP):
        res["ddp_buckets"] = ddp_buckets(net)   # після 1-го кроку DDP перебудовує кошики
    finish(a, res, rank, distributed)


def init_group():
    """dist.init_process_group(backend="gloo").

    Після перезапуску torchrun зі статичною адресою (--rdzv-backend=static) процеси підключаються до того самого
    сховища TCPStore агента, у якому лишились ключі gloo від першої спроби, і init_process_group зависає.
    Тому ключі кожної спроби відокремлюються префіксом з номером перезапуску TORCHELASTIC_RESTART_COUNT.
    """
    from datetime import timedelta
    if os.environ.get("TORCHELASTIC_USE_AGENT_STORE") == "True":
        store = dist.TCPStore(os.environ["MASTER_ADDR"], int(os.environ["MASTER_PORT"]),
                              is_master=False, timeout=timedelta(seconds=300),
                              use_libuv=os.environ.get("USE_LIBUV", "1") == "1")   # Windows: без libuv
        store = dist.PrefixStore(f"attempt_{os.environ.get('TORCHELASTIC_RESTART_COUNT', '0')}", store)
        dist.init_process_group(backend="gloo", store=store, rank=int(os.environ["RANK"]),
                                world_size=int(os.environ["WORLD_SIZE"]))
    else:
        dist.init_process_group(backend="gloo")


def ddp_buckets(net):
    """Розміри кошиків DDP у байтах (рядок з логів DDP), якщо версія PyTorch його дає."""
    try:
        d = net._get_ddp_logging_data()
        return dict(bucket_sizes_bytes=d.get("bucket_sizes"), bucket_cap_bytes=d.get("bucket_cap_bytes"))
    except Exception:  # noqa: BLE001
        return None


def run_equiv(a, model, train, rank, P, b, res):
    """Крок 2: ḡ ручної синхронізації на P процесах проти g одного процесу на тому самому батчі B."""
    assert a.sync == "manual" and P > 1
    x, y = train.tensors
    xB, yB = x[:a.batch], y[:a.batch]                # перший батч навчальної частини, B прикладів
    for p in model.parameters():
        dist.broadcast(p.data, src=0)
    F.cross_entropy(model(xB[rank::P]), yB[rank::P]).backward()   # приклади r, r + P, ...
    for p in model.parameters():
        dist.all_reduce(p.grad, op=dist.ReduceOp.SUM)
        p.grad /= P
    if rank == 0:
        ref = cm.make_model()                        # один процес на всьому батчі
        F.cross_entropy(ref(xB), yB).backward()
        per = []
        for (n, p), q in zip(model.named_parameters(), ref.parameters()):
            g, gbar = q.grad, p.grad
            per.append(dict(name=n, shape=list(p.shape), max_abs_g=float(g.abs().max()),
                            max_abs_diff=float((g - gbar).abs().max()),
                            ratio=float((g - gbar).abs().max() / g.abs().max())))
        res["grad_check"] = per
        res["max_ratio"] = max(r["ratio"] for r in per)

    # 50 кроків з ручною синхронізацією, далі сума ваг у float64 кожного процесу
    model.zero_grad(set_to_none=True)
    params = list(model.parameters())
    opt = torch.optim.SGD(params, lr=a.lr, momentum=cm.MOMENTUM)
    sampler = DistributedSampler(train, num_replicas=P, rank=rank, shuffle=True, seed=0)
    loader = DataLoader(train, batch_size=b, sampler=sampler, drop_last=True)
    step, epoch = 0, 0
    while step < cm.MEASURE_STEPS:
        sampler.set_epoch(epoch)
        for xb, yb in loader:
            opt.zero_grad(set_to_none=True)
            F.cross_entropy(model(xb), yb).backward()
            for p in params:
                dist.all_reduce(p.grad, op=dist.ReduceOp.SUM)
                p.grad /= P
            opt.step()
            step += 1
            if step == cm.MEASURE_STEPS:
                break
        epoch += 1
    s = torch.tensor([sum(float(p.detach().double().sum()) for p in params)], dtype=torch.float64)
    sums = [torch.zeros(1, dtype=torch.float64) for _ in range(P)]
    dist.all_gather(sums, s)
    sums = [float(t) for t in sums]
    res["weight_sums"] = sums
    res["weight_sum_spread"] = max(sums) - min(sums)
    res["equiv_steps"] = step


def finish(a, res, rank, distributed):
    if rank == 0 and a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        brief = {k: res[k] for k in ("task", "sync", "P", "B", "b", "t_step_ms", "t_sync_ms", "X", "acc", "auc",
                                     "steps", "max_ratio", "weight_sum_spread") if k in res}
        print(json.dumps(brief, ensure_ascii=False), flush=True)
    if distributed:
        dist.barrier()
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
