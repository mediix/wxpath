"""Deterministic URL-path-repeat trap detection (M4b).

A *trap* is a URL whose path contains a short segment-cycle repeated consecutively
beyond a threshold — the shape that infinite crawls take: ``/a/b/a/b/a/b/…``,
``/next/next/next/…``, self-deepening calendar/pagination loops. Detection is a
**pure function of the URL string** (no DOM, clock, RNG, or network), so a
trap-pruned crawl stays bit-reproducible.

The filter is applied by :class:`TrapFilterFrontier`, a composable wrapper around any
:class:`~wxpath.http.frontier.base.Frontier` backend. It drops traps at ``push()``
(returning ``False``, the same contract as a dedup miss) and delegates every other
method unchanged. ``get_frontier_backend()`` wraps the configured backend with it
only when ``http.client.frontier.trap.enabled`` — so with the feature off (the
default) the frontier object graph is identical to pre-M4b and default crawls remain
byte-identical (Invariant I5). See ``design/M4b-trap-detection.md``.
"""

import urllib.parse
from typing import List, Optional, Tuple

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.base import Frontier
from wxpath.util.logging import get_logger

log = get_logger(__name__)


def _segments(url: str) -> List[str]:
    """Non-empty path segments of ``url`` (query/fragment ignored).

    Cycle traps live in the path; query-string traps (``?page=∞`` etc.) are a
    distinct signal class handled separately (see design §6).
    """
    path = urllib.parse.urlsplit(url).path
    return [s for s in path.split("/") if s]


def max_consecutive_cycle(segments: List[str], max_period: int) -> Tuple[int, int]:
    """Return ``(period, repeats)`` of the longest consecutive block-repeat.

    Scans cycle lengths ``1..max_period`` and, for each start position, counts how
    many times the block ``segments[i:i+p]`` repeats back-to-back. ``repeats == 1``
    means no block repeats (every path occurs at least once). O(n²) over the short
    segment list — path segment counts are tiny.
    """
    best_period, best_reps = 1, 1
    n = len(segments)
    upper = min(max_period, n // 2)
    for p in range(1, upper + 1):
        i = 0
        while i + p <= n:
            block = segments[i:i + p]
            reps = 1
            j = i + p
            while j + p <= n and segments[j:j + p] == block:
                reps += 1
                j += p
            if reps > best_reps:
                best_period, best_reps = p, reps
            i += 1
    return best_period, best_reps


def is_path_trap(url: str, max_path_repeat: int = 3, max_period: int = 4) -> bool:
    """``True`` if ``url``'s path repeats a ≤``max_period`` cycle more than
    ``max_path_repeat`` times consecutively.

    ``max_path_repeat < 1`` disables detection (defensive: never drop). A path of
    all-distinct segments (a legitimate deep chain) has a max run of 1 and is never
    a trap, regardless of length.
    """
    if max_path_repeat < 1:
        return False
    _, reps = max_consecutive_cycle(_segments(url), max_period)
    return reps > max_path_repeat


class TrapFilterFrontier(Frontier):
    """Composable admission filter: drop URL-path-repeat traps at ``push()``.

    Wraps any :class:`Frontier` backend, applies :func:`is_path_trap` in
    :meth:`push`, and delegates ``pop``/``seen``/``size``/``mark_done``/
    ``checkpoint``/``close`` to the inner backend. Constructed by
    ``get_frontier_backend()`` only when ``http.client.frontier.trap.enabled``.
    """

    def __init__(
        self,
        inner: Frontier,
        *,
        max_path_repeat: int = 3,
        max_period: int = 4,
    ) -> None:
        self._inner = inner
        self._max_path_repeat = max_path_repeat
        self._max_period = max_period
        self.dropped = 0  # observability: count of traps pruned this run

    async def push(self, task: CrawlTask) -> bool:
        if is_path_trap(task.url, self._max_path_repeat, self._max_period):
            self.dropped += 1
            log.debug("trap pruned", extra={"url": task.url})
            return False  # same contract as a dedup miss → engine does not enqueue
        return await self._inner.push(task)

    async def pop(self) -> Optional[CrawlTask]:
        return await self._inner.pop()

    async def mark_done(self, url: str) -> None:
        return await self._inner.mark_done(url)

    async def seen(self, url: str) -> bool:
        return await self._inner.seen(url)

    async def size(self) -> int:
        return await self._inner.size()

    async def checkpoint(self) -> None:
        return await self._inner.checkpoint()

    async def close(self) -> None:
        return await self._inner.close()
