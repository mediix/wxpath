"""In-memory frontier — the default, behavior-preserving backend.

A faithful wrapper over ``asyncio.Queue`` + a ``set``, reproducing the engine's
historical FIFO/BFS behavior exactly (Invariant I5). M2 will swap the FIFO queue
for a priority queue; M1 keeps it FIFO.
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
        # asyncio.Queue binds to the running loop lazily (Py>=3.10), so it is safe
        # to construct outside an active event loop.
        self._queue: "asyncio.Queue[CrawlTask]" = asyncio.Queue()
        self._seen: set[str] = set()
        self._closed = False

    async def push(self, task: CrawlTask) -> bool:
        if task.url in self._seen:
            return False
        self._seen.add(task.url)
        self._queue.put_nowait(task)
        return True

    async def pop(self) -> Optional[CrawlTask]:
        if self._closed and self._queue.empty():
            return None
        # Blocks until an item arrives, exactly like the historical
        # ``await queue.get()``. On engine teardown the submitter task is cancelled,
        # which interrupts this await cleanly (M0).
        return await self._queue.get()

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
