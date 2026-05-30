"""Redis frontier — experimental, distributed.

Backs the frontier with Redis so multiple worker processes can share one queue +
dedup set. The interface is identical to the other backends; switching is a
settings change. **Experimental:** not covered by CI (requires a live Redis); the
backend-agnostic contract tests document the behavior it must satisfy.

Structures (all under ``namespace``):
- ``queued``   sorted set, score = discovery seq (ZPOPMIN = FIFO == BFS for M1)
- ``seen``     set, dedup membership
- ``inflight`` set, popped-but-not-done (reclaimed on resume)
- ``done``     set, fully processed
- ``task:{url}`` hash, the CrawlTask payload (depth, backlink, seq, pickled segments)
"""

import asyncio
import pickle
from typing import Optional

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.base import Frontier
from wxpath.util.logging import get_logger

log = get_logger(__name__)


class RedisFrontier(Frontier):
    def __init__(
        self,
        address: str,
        namespace: str = "wxpath:frontier",
        resume: bool = False,
        poll_interval: float = 0.02,
    ) -> None:
        try:
            from redis import asyncio as aioredis
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "RedisFrontier requires the 'redis' package: "
                "pip install 'wxpath[frontier-redis]'"
            ) from exc
        self._r = aioredis.from_url(address)
        self._ns = namespace
        self._resume = resume
        self._poll_interval = poll_interval
        self._closed = False
        self._init_done = False
        self._init_lock = asyncio.Lock()

    def _k(self, name: str) -> str:
        return f"{self._ns}:{name}"

    def _task_key(self, url: str) -> str:
        return f"{self._ns}:task:{url}"

    async def _ensure_init(self) -> None:
        if self._init_done:
            return
        async with self._init_lock:
            if self._init_done:
                return
            if self._resume:
                # Interrupted in-flight URLs return to the queue at their stored seq.
                for raw in await self._r.smembers(self._k("inflight")):
                    url = raw.decode() if isinstance(raw, bytes) else raw
                    data = await self._r.hgetall(self._task_key(url))
                    seq = float(data.get(b"seq", 0))
                    await self._r.zadd(self._k("queued"), {url: seq})
                    await self._r.srem(self._k("inflight"), url)
            else:
                # Clean start: drop the index structures (orphan task hashes are
                # harmless — a clean 'seen' lets them be overwritten on re-push).
                await self._r.delete(
                    self._k("queued"), self._k("seen"),
                    self._k("inflight"), self._k("done"), self._k("seq"),
                )
            self._init_done = True

    async def push(self, task: CrawlTask) -> bool:
        await self._ensure_init()
        if await self._r.sadd(self._k("seen"), task.url) == 0:
            return False  # already seen → dedup
        seq = await self._r.incr(self._k("seq"))
        await self._r.hset(
            self._task_key(task.url),
            mapping={
                "depth": task.depth,
                "backlink": task.backlink or "",
                "seq": seq,
                "segments": pickle.dumps(task.segments),
            },
        )
        await self._r.zadd(self._k("queued"), {task.url: float(seq)})
        return True

    async def pop(self) -> Optional[CrawlTask]:
        await self._ensure_init()
        while True:
            if self._closed:
                return None
            popped = await self._r.zpopmin(self._k("queued"), 1)
            if popped:
                url_raw, _score = popped[0]
                url = url_raw.decode() if isinstance(url_raw, bytes) else url_raw
                await self._r.sadd(self._k("inflight"), url)
                data = await self._r.hgetall(self._task_key(url))
                return CrawlTask(
                    elem=None,
                    url=url,
                    segments=pickle.loads(data[b"segments"]),
                    depth=int(data[b"depth"]),
                    backlink=(data[b"backlink"].decode() or None),
                )
            await asyncio.sleep(self._poll_interval)

    async def mark_done(self, url: str) -> None:
        await self._r.srem(self._k("inflight"), url)
        await self._r.sadd(self._k("done"), url)

    async def seen(self, url: str) -> bool:
        return bool(await self._r.sismember(self._k("seen"), url))

    async def size(self) -> int:
        return int(await self._r.zcard(self._k("queued")))

    async def checkpoint(self) -> None:
        return

    async def close(self) -> None:
        self._closed = True
        try:
            await self._r.aclose()
        except Exception:  # pragma: no cover - best-effort teardown
            pass
