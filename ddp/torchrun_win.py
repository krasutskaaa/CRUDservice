#!/usr/bin/env python3
"""torchrun для збірок PyTorch без libuv (Windows): python ddp/torchrun_win.py <аргументи torchrun>.

У PyTorch 2.14 для Windows конструктор TCPStore за замовчуванням вмикає libuv і не читає USE_LIBUV,
а rendezvous-бекенди torchrun (c10d для --standalone, static) створюють TCPStore без use_libuv,
тому агент падає з "use_libuv was requested but PyTorch was built without libuv support".
Тут TCPStore підміняється підкласом, що бере use_libuv з USE_LIBUV (за замовчуванням "1", як у PyTorch),
Також сигнал зупинки воркерів CTRL_C_EVENT замінено на SIGTERM (див. patch()).
Після цього викликається звичайний torch.distributed.run.main().
"""
import os

import torch.distributed as dist

_Base = dist.TCPStore


class TCPStore(_Base):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("use_libuv", os.environ.get("USE_LIBUV", "1") == "1")
        super().__init__(*args, **kwargs)


def patch():
    # Зупинка процесів: на Windows torchrun шле CTRL_C_EVENT (і як «м'який», і як «примусовий» сигнал).
    # CTRL_C отримує вся консоль, зокрема сам агент, а воркер, що висить у gloo all_reduce після відмови
    # сусіда, його не обробляє, тож перезапуск після відмови не відбувається. SIGTERM на Windows —
    # це TerminateProcess, тобто негайне завершення, як SIGKILL на Linux.
    import signal
    import sys
    if sys.platform == "win32":
        from torch.distributed.elastic.multiprocessing import api as mp_api
        from torch.distributed.elastic.multiprocessing.subprocess_handler import subprocess_handler as sh
        mp_api._get_kill_signal = mp_api._get_default_signal = sh._get_default_signal = lambda: signal.SIGTERM

    import torch.distributed.elastic.rendezvous as rdzv
    from torch.distributed.elastic.rendezvous import (c10d_rendezvous_backend, dynamic_rendezvous,
                                                      static_tcp_rendezvous)
    dist.TCPStore = TCPStore
    for m in (rdzv, c10d_rendezvous_backend, dynamic_rendezvous, static_tcp_rendezvous):
        if getattr(m, "TCPStore", None) is _Base:
            m.TCPStore = TCPStore


if __name__ == "__main__":
    patch()
    from torch.distributed.run import main
    main()
