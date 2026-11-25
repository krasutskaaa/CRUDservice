"""Хеш-таблиця кешу результатів за record_id (структура С2, варіант 12).

Власна реалізація: масив кошиків, у кожному однозвʼязний ланцюжок вузлів [ключ, значення, наступний].
Сама таблиця не синхронізована: замок бере стенд навколо кожного виклику put/get.
"""

LOAD = 0.75


class HashTable:
    __slots__ = ("_buckets", "_mask", "_size", "resizes")

    def __init__(self, capacity=16):
        cap = 1
        while cap < capacity:
            cap <<= 1
        self._buckets = [None] * cap
        self._mask = cap - 1
        self._size = 0
        self.resizes = 0

    @staticmethod
    def _h(k):
        # перемішування бітів ключа (множення Кнута), щоб сусідні record_id не йшли в сусідні кошики підряд
        return ((k * 2654435761) & 0xFFFFFFFF) >> 7 ^ k

    # ---- запис ----
    def put(self, k, v):
        i = self._h(k) & self._mask
        node = self._buckets[i]
        while node is not None:
            if node[0] == k:
                node[1] = v
                return
            node = node[2]
        self._buckets[i] = [k, v, self._buckets[i]]
        self._size += 1
        if self._size > LOAD * (self._mask + 1):
            self._grow()

    def _grow(self):
        old = self._buckets
        cap = len(old) * 2
        new = [None] * cap
        mask = cap - 1
        for node in old:
            while node is not None:
                nxt = node[2]
                j = self._h(node[0]) & mask
                node[2] = new[j]
                new[j] = node
                node = nxt
        self._buckets = new
        self._mask = mask
        self.resizes += 1

    # ---- читання (жодне поле таблиці і вузлів не змінюється) ----
    def get(self, k):
        node = self._buckets[self._h(k) & self._mask]
        while node is not None:
            if node[0] == k:
                return node[1]
            node = node[2]
        return None

    def size(self):
        return self._size

    # ---- перевірки стенду (викликаються без конкуренції) ----
    def items(self):
        for node in self._buckets:
            while node is not None:
                yield node[0], node[1]
                node = node[2]

    def check_structure(self):
        """Кількість вузлів = size, кожен вузол у своєму кошику, ключі не повторюються."""
        seen = set()
        nodes = 0
        for i, node in enumerate(self._buckets):
            while node is not None:
                nodes += 1
                if self._h(node[0]) & self._mask != i:
                    return False, f"ключ {node[0]} не в своєму кошику"
                if node[0] in seen:
                    return False, f"ключ {node[0]} повторюється"
                seen.add(node[0])
                node = node[2]
        if nodes != self._size:
            return False, f"вузлів {nodes}, size() = {self._size}"
        return True, "ok"

    def fingerprint(self):
        """Знімок усіх полів: масив кошиків (identity), маска, розмір, вузли (identity, ключ, значення, наступний)."""
        fp = [id(self._buckets), self._mask, self._size, self.resizes]
        for node in self._buckets:
            while node is not None:
                fp.append((id(node), node[0], node[1], id(node[2])))
                node = node[2]
        return fp
