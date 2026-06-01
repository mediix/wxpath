"""Accumulators: quantile sketch, HyperLogLog, status histogram.

numpy and datasketch are lazy-imported so `wxpath-bench list` stays snappy.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

_RESERVOIR_CAP = 200_000


class Quantiles:
    """Reservoir-sampled float buffer with percentile readout.

    Honest stop-gap: full sample below the cap, uniform reservoir sampling
    above. Swap for DDSketch when wxpath gains per-URL latency emission and
    sample counts can exceed reservoir cap routinely.
    """

    def __init__(self, cap: int = _RESERVOIR_CAP):
        self._cap = cap
        self._buf: list[float] = []
        self._seen = 0

    def add(self, x: float) -> None:
        self._seen += 1
        if len(self._buf) < self._cap:
            self._buf.append(float(x))
            return
        j = random.randint(0, self._seen - 1)
        if j < self._cap:
            self._buf[j] = float(x)

    @property
    def count(self) -> int:
        return self._seen

    def percentile(self, p: float) -> float | None:
        if not self._buf:
            return None
        import numpy as np
        return float(np.percentile(self._buf, p))

    def min(self) -> float | None:
        return min(self._buf) if self._buf else None

    def max(self) -> float | None:
        return max(self._buf) if self._buf else None


class UniqueSet:
    """HyperLogLog cardinality estimator."""

    def __init__(self, p: int = 14):
        from datasketch import HyperLogLog
        self._hll = HyperLogLog(p=p)
        self._added = 0

    def add(self, item: str | bytes) -> None:
        if isinstance(item, str):
            item = item.encode('utf-8')
        self._hll.update(item)
        self._added += 1

    @property
    def added(self) -> int:
        return self._added

    def estimate(self) -> int:
        return int(self._hll.count())


class StatusHistogram:
    """Bucketed HTTP status code counter."""

    def __init__(self):
        self._counts: Counter[int] = Counter()

    def add(self, status: int) -> None:
        self._counts[int(status)] += 1

    def buckets(self) -> dict[str, int]:
        out = {'2xx': 0, '3xx': 0, '4xx': 0, '5xx': 0, 'other': 0}
        for code, n in self._counts.items():
            if 200 <= code < 300:
                out['2xx'] += n
            elif 300 <= code < 400:
                out['3xx'] += n
            elif 400 <= code < 500:
                out['4xx'] += n
            elif 500 <= code < 600:
                out['5xx'] += n
            else:
                out['other'] += n
        return out

    def raw(self) -> dict[int, int]:
        return dict(self._counts)

    @property
    def total(self) -> int:
        return sum(self._counts.values())
