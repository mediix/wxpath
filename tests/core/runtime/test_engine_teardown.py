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
import logging
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


def _wide_corpus(n: int) -> dict[str, bytes]:
    """A root linking to ``n`` children — a frontier far larger than the budget.

    With the budget tripping after only a few yields, many children are still
    queued (and ``submitter()`` is still mid-flight awaiting ``frontier.pop()``)
    at the moment the consumer aborts — the exact condition M0 hardens.
    """
    links = b"".join(
        f"<a href='p{i}.html'>{i}</a>".encode() for i in range(n)
    )
    pages = {"http://test/": b"<html><body>" + links + b"</body></html>"}
    for i in range(n):
        pages[f"http://test/p{i}.html"] = (
            f"<html><body><p>{i}</p></body></html>".encode()
        )
    return pages


def test_blocking_iter_budget_abort_tears_down_cleanly(monkeypatch):
    """Mirror the benchmark driver's ``max_urls`` budget abort end-to-end.

    The driver iterates ``wxpath_async_blocking_iter`` and breaks out of the
    ``for item in ...`` loop the instant ``urls_seen >= max_urls`` (see
    ``benchmarks/driver.py``). That break is the production abort path. With a
    frontier far larger than the budget, the background ``submitter()`` task is
    still pending when the generator is torn down.

    Acceptance (FRONTIER_ROADMAP M0 item 1): the abort path is clean — neither a
    ``RuntimeError: Event loop is closed`` unraisable (pending task destroyed
    against a dead loop) NOR an asyncio ``Task was destroyed but it is pending!``
    log record (task abandoned without cancellation).
    """
    pages = _wide_corpus(40)
    eng = WXPathEngine(crawler=MockCrawler(pages=pages))

    # (1) Capture unraisable exceptions (the "Event loop is closed" path fires
    #     from BaseEventLoop.__del__ during GC, never as a normal raise).
    unraisable: list[BaseException | None] = []
    monkeypatch.setattr(
        sys,
        "unraisablehook",
        lambda hook_args: unraisable.append(hook_args.exc_value),
    )

    # (2) Capture asyncio's own warning channel — a pending Task that is
    #     destroyed without being cancelled logs "Task was destroyed but it is
    #     pending!" through the 'asyncio' logger.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.addHandler(handler)

    max_urls = 3  # budget far below the 40-page frontier
    urls_seen = 0
    aborted = False
    try:
        it = engine.wxpath_async_blocking_iter(
            "url('http://test/')//url(//a/@href)",
            max_depth=1,
            engine=eng,
            yield_errors=True,  # match the driver's call exactly
        )
        # Replicate driver.py's budget loop verbatim.
        for _item in it:
            urls_seen += 1
            if urls_seen >= max_urls:
                aborted = True
                break
        it.close()  # deterministic GeneratorExit -> teardown finally
        del it
        for _ in range(3):
            gc.collect()
    finally:
        asyncio_logger.removeHandler(handler)

    assert aborted and urls_seen == max_urls, "budget abort did not fire as designed"

    loop_closed = [
        exc
        for exc in unraisable
        if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc)
    ]
    assert not loop_closed, f"abort destroyed a task against a closed loop: {loop_closed}"

    pending_task_warnings = [
        r for r in records if "Task was destroyed but it is pending" in r.getMessage()
    ]
    assert not pending_task_warnings, (
        "abort abandoned a pending task without cancelling it: "
        f"{[r.getMessage() for r in pending_task_warnings]}"
    )
