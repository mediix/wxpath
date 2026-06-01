"""M6: CanonicalizingFrontier wrapper — normalizes URLs, then delegates."""

import pytest

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.canonical import CanonicalizingFrontier
from wxpath.http.frontier.memory import InMemoryFrontier


def _task(url: str, depth: int = 0) -> CrawlTask:
    return CrawlTask(elem=None, url=url, segments=[], depth=depth, backlink=None)


def _wrap():
    inner = InMemoryFrontier()
    return CanonicalizingFrontier(inner), inner


@pytest.mark.asyncio
async def test_push_rewrites_task_url_to_canonical():
    f, inner = _wrap()
    task = _task("HTTP://H:80/p/1/?utm_source=x#frag")
    assert await f.push(task) is True
    assert task.url == "http://h/p/1"          # mutated in place to canonical
    assert await inner.seen("http://h/p/1") is True
    assert f.canonicalized == 1


@pytest.mark.asyncio
async def test_variants_dedup_to_one_push():
    f, inner = _wrap()
    variants = [
        "http://h/p/1",
        "http://h/p/1?utm_source=x",
        "http://h/p/1#section",
        "http://h/p/1/",
    ]
    results = [await f.push(_task(u)) for u in variants]
    assert results == [True, False, False, False]   # first wins, rest are dedup misses
    assert await f.size() == 1


@pytest.mark.asyncio
async def test_already_canonical_url_not_counted():
    f, _ = _wrap()
    assert await f.push(_task("http://h/p/1")) is True
    assert f.canonicalized == 0                      # nothing to rewrite


@pytest.mark.asyncio
async def test_pop_returns_canonical_task():
    f, _ = _wrap()
    await f.push(_task("http://h/p/1/?utm_source=x"))
    task = await f.pop()
    assert task.url == "http://h/p/1"


@pytest.mark.asyncio
async def test_seen_canonicalizes_argument():
    f, _ = _wrap()
    await f.push(_task("http://h/p/1"))
    # A noisy variant of an already-seen URL reads as seen.
    assert await f.seen("http://h/p/1/?utm_source=x#frag") is True
    assert await f.seen("http://h/other") is False


@pytest.mark.asyncio
async def test_mark_done_and_close_delegate():
    f, inner = _wrap()
    await f.push(_task("http://h/p/1/"))
    await f.pop()
    await f.mark_done("http://h/p/1?utm_source=x")   # canonical match, no raise
    assert await f.seen("http://h/p/1") is True
    await f.checkpoint()
    await f.close()
    assert inner._closed is True


@pytest.mark.asyncio
async def test_strip_list_is_configurable():
    inner = InMemoryFrontier()
    # Empty strip list ⇒ tracking params are preserved (so variants stay distinct).
    f = CanonicalizingFrontier(inner, strip_params=())
    assert await f.push(_task("http://h/p?utm_source=x")) is True
    assert await f.push(_task("http://h/p?utm_source=y")) is True
    assert await f.size() == 2
