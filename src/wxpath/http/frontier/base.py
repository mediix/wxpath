"""The engine-level URL frontier interface.

The frontier owns the set of URLs the crawl still needs to visit (the queue) and
the dedup membership ({queued, inflight, done}). It is distinct from the
``Crawler``: the crawler is a concurrency-limited fetch pool; the frontier is the
scheduler that decides *what URL to hand the crawler next*.

Backends (memory / sqlite / redis) are selected by settings via
``get_frontier_backend()``. See ``design/M1-pluggable-frontier.md`` for the full
specification and the I5 determinism contract.
"""

from typing import Optional, Protocol, runtime_checkable

from wxpath.core.models import CrawlTask


@runtime_checkable
class Frontier(Protocol):
    """Engine-level URL frontier + dedup. Owns {queued, inflight, done, seen}.

    Ordering contract: :meth:`pop` returns the highest-priority queued task, ties
    broken by ascending discovery order (a monotonic counter, never wall-clock).
    With all priorities equal (the M1 default, ``priority == depth``), this
    reproduces FIFO/BFS order exactly (Invariant I5).
    """

    async def push(self, task: CrawlTask) -> bool:
        """Enqueue a task, deduplicating by URL.

        If the URL has ever been seen (queued, inflight, or done), do nothing and
        return ``False``. Otherwise record it ``queued`` and return ``True``.
        Idempotent.
        """
        ...

    async def pop(self) -> Optional[CrawlTask]:
        """Return the next task (highest priority, earliest discovery).

        Atomically flips the returned task ``queued -> inflight``. Awaits while the
        queue is empty; returns ``None`` only after :meth:`close` has been signalled
        and the queue is drained.
        """
        ...

    async def mark_done(self, url: str) -> None:
        """Mark a URL ``done`` (its response was fully processed).

        Lets a resuming crawl distinguish completed work from interrupted in-flight
        work. A no-op for non-persistent backends.
        """
        ...

    async def seen(self, url: str) -> bool:
        """Return ``True`` if the URL exists in any state (queued | inflight | done)."""
        ...

    async def size(self) -> int:
        """Return the count of ``queued`` tasks (drives the terminal condition)."""
        ...

    async def checkpoint(self) -> None:
        """Durability barrier (commit/flush). A no-op for in-memory backends."""
        ...

    async def close(self) -> None:
        """Release resources and unblock any pending :meth:`pop`."""
        ...
