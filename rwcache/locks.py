"""Способи синхронізації стенду: один глобальний мʼютекс і RWLock на threading.Condition.

Обидва мають однаковий інтерфейс: acquire_write/release_write, acquire_read/release_read.
"""
import threading


class GlobalLock:
    """Один мʼютекс на всі операції: і запис, і читання захоплюють той самий threading.Lock."""

    def __init__(self):
        lk = threading.Lock()
        self.acquire_write = self.acquire_read = lk.acquire
        self.release_write = self.release_read = lk.release
        self.name = "global"


class RWLock:
    """Замок читання-запису на одній умовній змінній.

    policy:
      fair    — писець, що чекає, зупиняє нових читачів; після звільнення запису спершу заходять
                читачі, які вже чекали на той момент (пакет), далі наступний писець;
      writers — перевага писцям: поки є хоч один писець, що чекає, жоден новий читач не заходить;
      readers — перевага читачам: читач заходить завжди, коли немає активного писця.
    """

    def __init__(self, policy="fair"):
        if policy not in ("fair", "writers", "readers"):
            raise ValueError(policy)
        self.policy = policy
        self.name = f"rw-{policy}"
        self._c = threading.Condition(threading.Lock())
        self._readers = 0          # активні читачі
        self._writer = False       # активний писець
        self._ww = 0               # писці, що чекають
        self._rw = 0               # читачі, що чекають
        self._batch = 0            # скільки читачів ще можна впустити поза чергою писців (fair)

    def acquire_read(self):
        c = self._c
        with c:
            if self.policy == "readers":
                while self._writer:
                    c.wait()
            elif self.policy == "writers":
                while self._writer or self._ww:
                    c.wait()
            else:
                self._rw += 1
                while self._writer or (self._ww and not self._batch):
                    c.wait()
                self._rw -= 1
                if self._batch:
                    self._batch -= 1
            self._readers += 1

    def release_read(self):
        c = self._c
        with c:
            self._readers -= 1
            if self._readers == 0 and self._ww:
                c.notify_all()

    def acquire_write(self):
        c = self._c
        with c:
            self._ww += 1
            while self._writer or self._readers or self._batch:
                c.wait()
            self._ww -= 1
            self._writer = True

    def release_write(self):
        c = self._c
        with c:
            self._writer = False
            if self.policy == "fair":
                self._batch = self._rw
            c.notify_all()


def make_lock(kind, policy="fair"):
    return GlobalLock() if kind == "global" else RWLock(policy)
