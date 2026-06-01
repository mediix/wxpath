"""M4b: TrapFilterFrontier wrapper — drops traps, delegates everything else."""

import pytest

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.memory import InMemoryFrontier
from wxpath.http.frontier.trap import TrapFilterFrontier

TRAP_URL = "http://h/" + "/".join(["a", "b"] * 5)   # period-2 × 5 → trap
GOOD_URL = "http://h/docs/guide/intro/setup"


def _task(url: str, depth: int = 0) -> CrawlTask:
    return CrawlTask(elem=None, url=url, segments=[], depth=depth, backlink=None)


def _wrap():
    inner = InMemoryFrontier()
    return TrapFilterFrontier(inner, max_path_repeat=3, max_period=4), inner


@pytest.mark.asyncio
async def test_trap_url_is_dropped_and_not_seen_by_inner():
    f, inner = _wrap()
    assert await f.push(_task(TRAP_URL)) is False   # dedup-miss contract
    assert f.dropped == 1
    assert await inner.seen(TRAP_URL) is False       # never reached the backend
    assert await f.size() == 0


@pytest.mark.asyncio
async def test_normal_url_is_delegated():
    f, inner = _wrap()
    assert await f.push(_task(GOOD_URL)) is True
    assert f.dropped == 0
    assert await inner.seen(GOOD_URL) is True
    assert await f.size() == 1
    task = await f.pop()
    assert task.url == GOOD_URL


@pytest.mark.asyncio
async def test_dedup_parity_delegates_to_inner():
    f, _ = _wrap()
    assert await f.push(_task(GOOD_URL)) is True
    assert await f.push(_task(GOOD_URL)) is False    # inner dedup, not a trap
    assert f.dropped == 0                            # the second miss was dedup
    assert await f.size() == 1


@pytest.mark.asyncio
async def test_mark_done_and_close_delegate():
    f, inner = _wrap()
    await f.push(_task(GOOD_URL))
    await f.pop()
    await f.mark_done(GOOD_URL)        # no-op on memory, but must not raise
    assert await f.seen(GOOD_URL) is True
    await f.checkpoint()
    await f.close()
    assert inner._closed is True       # close propagated to the wrapped backend


@pytest.mark.asyncio
async def test_disabled_threshold_passes_everything():
    inner = InMemoryFrontier()
    f = TrapFilterFrontier(inner, max_path_repeat=0)  # 0 disables detection
    assert await f.push(_task(TRAP_URL)) is True
    assert f.dropped == 0
