"""Regression tests for clean async teardown of the blocking iterator.

`wxpath_async_blocking_iter` spins up its own event loop and drives
`WXPathEngine.run` (an async generator) by hand. `run` starts a background
`submitter()` task that sits awaiting `queue.get()`. When a consumer stops
iterating early (e.g. a benchmark driver aborting on a ``max_urls`` budget),
the sync generator is closed via ``GeneratorExit`` and the engine's async
generator is ``aclose``-d. If the submitter task is not cancelled before the
loop closes, it is later destroyed against a closed loop and surfaces an
ignored ``RuntimeError: Event loop is closed`` during garbage collection.
"""

from __future__ import annotations

import gc
import sys

from tests.utils import MockCrawler
from wxpath.core.runtime import engine
from wxpath.core.runtime.engine import WXPathEngine


def _multi_page_corpus() -> dict[str, bytes]:
    """A root page linking to several children — enough to break out early."""
    pages = {
        "http://test/": (
            b"<html><body>"
            b"<a href='a.html'>A</a><a href='b.html'>B</a>"
            b"<a href='c.html'>C</a><a href='d.html'>D</a>"
            b"<a href='e.html'>E</a><a href='f.html'>F</a>"
            b"</body></html>"
        ),
    }
    for name in "abcdef":
        pages[f"http://test/{name}.html"] = (
            f"<html><body><p>{name}</p></body></html>".encode()
        )
    return pages


def test_blocking_iter_early_break_tears_down_cleanly(monkeypatch):
    """Breaking out of the iterator early must not leak a pending submitter task.

    Without clean teardown the abandoned ``submitter()`` task is destroyed
    against a closed loop, raising ``RuntimeError: Event loop is closed`` as an
    unraisable exception during GC. We capture unraisables via
    ``sys.unraisablehook`` and assert none of them is that error.
    """
    pages = _multi_page_corpus()
    eng = WXPathEngine(crawler=MockCrawler(pages=pages))

    unraisable: list[BaseException | None] = []
    monkeypatch.setattr(
        sys,
        "unraisablehook",
        lambda hook_args: unraisable.append(hook_args.exc_value),
    )

    it = engine.wxpath_async_blocking_iter(
        "url('http://test/')//url(//a/@href)",
        max_depth=1,
        engine=eng,
    )

    collected = []
    for item in it:
        collected.append(item)
        if len(collected) >= 2:
            break

    # Deterministically trigger the generator's teardown (GeneratorExit ->
    # the ``finally`` in wxpath_async_blocking_iter) instead of relying on GC.
    it.close()
    del it
    for _ in range(3):
        gc.collect()

    assert collected, "iterator should yield results before the early break"

    loop_closed = [
        exc
        for exc in unraisable
        if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc)
    ]
    assert not loop_closed, (
        "early break left a pending task to be destroyed against a closed "
        f"loop: {loop_closed}"
    )


def test_blocking_iter_full_iteration_still_works():
    """The teardown fix must not regress a normal, fully-consumed crawl."""
    pages = _multi_page_corpus()
    eng = WXPathEngine(crawler=MockCrawler(pages=pages))

    results = list(
        engine.wxpath_async_blocking_iter(
            "url('http://test/')//url(//a/@href)",
            max_depth=1,
            engine=eng,
        )
    )

    # One element yielded per linked child page.
    assert len(results) == 6
