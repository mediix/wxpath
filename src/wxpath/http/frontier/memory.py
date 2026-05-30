"""In-memory frontier — the default backend.

An ``asyncio.PriorityQueue`` + a ``set``. Items are ordered by ``(priority, seq)``:
lower priority value is popped sooner (the ``models.py`` convention), ties broken by
insertion order (FIFO). With the default ``priority == depth``, this reproduces the
engine's historical FIFO/BFS order exactly (Invariant I5) — see §4 of
``design/M2-priority-scheduling.md``.
"""

import asyncio
from typing import Optional

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.base import Frontier


class InMemoryFrontier(Frontier):
    """Frontier backed by an in-process queue and seen-set.

    Dedup is enforced at :meth:`push` (the URL enters the queue at most once); this
    is equivalent to the engine's historical submit-time dedup because the
    first-discovered copy is the one that would have been submitted anyway (see
    ``design/M1-pluggable-frontier.md`` §7).
    """

    def __init__(self) -> None:
        # PriorityQueue binds to the running loop lazily (Py>=3.10), so it is safe
        # to construct outside an active event loop. Items: (priority, seq, task).
        self._queue: "asyncio.PriorityQueue[tuple[int, int, CrawlTask]]" = (
            asyncio.PriorityQueue()
        )
        self._seen: set[str] = set()
        self._seq = 0  # monotonic insertion counter → FIFO tiebreak (deterministic)
        self._closed = False

    async def push(self, task: CrawlTask) -> bool:
        if task.url in self._seen:
            return False
        self._seen.add(task.url)
        # (priority, seq) makes the heap order deterministic and never compares the
        # CrawlTask itself (seq is unique).
        self._queue.put_nowait((task.priority, self._seq, task))
        self._seq += 1
        return True

    async def pop(self) -> Optional[CrawlTask]:
        if self._closed and self._queue.empty():
            return None
        # Blocks until an item arrives. On engine teardown the submitter task is
        # cancelled, which interrupts this await cleanly (M0).
        _priority, _seq, task = await self._queue.get()
        return task

    async def mark_done(self, url: str) -> None:
        # No persistence → nothing to record.
        return

    async def seen(self, url: str) -> bool:
        return url in self._seen

    async def size(self) -> int:
        return self._queue.qsize()

    async def checkpoint(self) -> None:
        return

    async def close(self) -> None:
        self._closed = True
