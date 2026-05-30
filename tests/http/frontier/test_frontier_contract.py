"""Backend-agnostic contract tests for the Frontier interface.

Parametrized over every backend so each new backend (sqlite, redis) inherits the
same behavioral guarantees. SQLite is added to ``BACKENDS`` when that backend
lands; the assertions below are written to hold for all of them.
"""

import pytest

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.memory import InMemoryFrontier

BACKENDS = ["memory", "sqlite"]


def _task(url: str, depth: int = 0) -> CrawlTask:
    return CrawlTask(elem=None, url=url, segments=[], depth=depth, backlink=None)


@pytest.fixture(params=BACKENDS)
def make_frontier(request, tmp_path):
    """Return a zero-arg factory producing a fresh frontier for the param backend."""
    def _make():
        if request.param == "memory":
            return InMemoryFrontier()
        if request.param == "sqlite":
            from wxpath.http.frontier.sqlite import SQLiteFrontier
            return SQLiteFrontier(path=str(tmp_path / "frontier.db"), resume=False)
        raise ValueError(f"unknown backend: {request.param}")
    return _make


@pytest.mark.asyncio
async def test_push_then_pop_returns_task(make_frontier):
    f = make_frontier()
    assert await f.push(_task("http://x/a")) is True
    assert await f.size() == 1
    task = await f.pop()
    assert task.url == "http://x/a"
    await f.close()


@pytest.mark.asyncio
async def test_push_dedups(make_frontier):
    f = make_frontier()
    assert await f.push(_task("http://x/a")) is True
    assert await f.push(_task("http://x/a")) is False  # duplicate URL
    assert await f.size() == 1
    await f.close()


@pytest.mark.asyncio
async def test_seen_across_states(make_frontier):
    f = make_frontier()
    await f.push(_task("http://x/a"))
    assert await f.seen("http://x/a") is True          # queued
    task = await f.pop()
    assert await f.seen("http://x/a") is True          # inflight
    await f.mark_done(task.url)
    assert await f.seen("http://x/a") is True          # done
    assert await f.seen("http://x/never") is False
    await f.close()


@pytest.mark.asyncio
async def test_size_counts_only_queued(make_frontier):
    f = make_frontier()
    for u in ("a", "b", "c"):
        await f.push(_task(f"http://x/{u}"))
    assert await f.size() == 3
    await f.pop()                                       # one -> inflight
    assert await f.size() == 2
    await f.close()


@pytest.mark.asyncio
async def test_fifo_order_at_equal_priority(make_frontier):
    f = make_frontier()
    for u in ("a", "b", "c"):
        await f.push(_task(f"http://x/{u}", depth=1))
    assert (await f.pop()).url == "http://x/a"
    assert (await f.pop()).url == "http://x/b"
    assert (await f.pop()).url == "http://x/c"
    await f.close()


@pytest.mark.asyncio
async def test_pop_after_close_returns_none(make_frontier):
    f = make_frontier()
    await f.close()
    assert await f.pop() is None
