"""Deterministic in-process fixture server + synthetic corpus generator.

Engine-baseline benchmarks crawl this loopback server instead of a live site so
that results are reproducible. The link graph, page sizes, and extractable
elements are fixed, which makes page counts, depth distribution, and extraction
totals identical on every run; only throughput (pages/s) varies with machine
load. See ``benchmarks/README.md`` ("Reproducible engine baseline").

The corpus is a breadth-first ``fan_out``-ary tree of ``page_count`` nodes.
Node 0 is served at ``/``; node ``i`` (>= 1) at ``/p/{i}``. Each node links to
its children — indices ``i*fan_out + 1 .. i*fan_out + fan_out`` that are
``< page_count`` — with **root-relative** ``<a href="/p/{child}">`` anchors, so
the HTML is independent of the ephemeral port the server binds.

Crawled with ``url('<base>')///url(//@href)`` the engine fetches every node
exactly once (URL dedup) and yields one value per node **except** the seed/root
page (a literal ``url('...')`` produces only a crawl intent, no extracted
value). Hence ``pages_completed == page_count`` and
``extracted_total == page_count - 1``.
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from dataclasses import dataclass

from aiohttp import web


# --------------------------------------------------------------------------- #
# Corpus generation
# --------------------------------------------------------------------------- #
def _children(i: int, fan_out: int, page_count: int) -> list[int]:
    first = i * fan_out + 1
    return [c for c in range(first, first + fan_out) if c < page_count]


def _path(i: int) -> str:
    return '/' if i == 0 else f'/p/{i}'


def _render_page(i: int, child_indices: list[int], filler_bytes: int) -> bytes:
    anchors = ''.join(f'<a href="{_path(c)}">node {c}</a>\n' for c in child_indices)
    filler = ('<!--' + 'x' * filler_bytes + '-->\n') if filler_bytes > 0 else ''
    page = (
        '<!DOCTYPE html>\n'
        f'<html><head><title>node {i}</title></head><body>\n'
        f'<h1>node {i}</h1>\n'
        f'{anchors}'
        f'{filler}'
        '</body></html>\n'
    )
    return page.encode('utf-8')


@dataclass
class Corpus:
    """A generated corpus plus the exact metrics a correct crawl must produce.

    The ``expected_*`` fields are the single source of truth for both the smoke
    test and the README — they are derived from the graph, not hand-counted.
    """

    pages: dict[str, bytes]
    page_count: int
    fan_out: int
    filler_bytes: int
    expected_pages_completed: int            # every node fetched exactly once
    expected_extractions: int                # one value per node except the seed/root
    expected_unique_urls: int                # distinct fetched URLs (observed)
    expected_depth_histogram: dict[int, int]
    tree_height: int

    @property
    def expected_selector_hit_rate(self) -> float:
        return self.expected_extractions / self.expected_pages_completed


def build_corpus(page_count: int, fan_out: int, filler_bytes: int = 0) -> Corpus:
    """Generate the deterministic BFS ``fan_out``-ary tree corpus."""
    if page_count < 1:
        raise ValueError('page_count must be >= 1')
    if fan_out < 1:
        raise ValueError('fan_out must be >= 1')

    # Children always have a larger index than their parent, so iterating in
    # ascending order guarantees a node's depth is known before its children.
    depth: dict[int, int] = {0: 0}
    pages: dict[str, bytes] = {}
    for i in range(page_count):
        kids = _children(i, fan_out, page_count)
        for c in kids:
            depth[c] = depth[i] + 1
        pages[_path(i)] = _render_page(i, kids, filler_bytes)

    hist = Counter(depth[i] for i in range(page_count))
    return Corpus(
        pages=pages,
        page_count=page_count,
        fan_out=fan_out,
        filler_bytes=filler_bytes,
        expected_pages_completed=page_count,
        expected_extractions=page_count - 1,
        expected_unique_urls=page_count,
        expected_depth_histogram=dict(sorted(hist.items())),
        tree_height=max(depth.values()),
    )


# --------------------------------------------------------------------------- #
# Fixture server
# --------------------------------------------------------------------------- #
class FixtureServer:
    """Serve a corpus over loopback from a private event loop on a daemon thread.

    The wxpath driver runs its crawl on the calling thread's own event loop
    (``wxpath_async_blocking_iter`` spins up a fresh loop), so the server must
    live on a *separate* thread with its own loop. Lifecycle::

        srv = FixtureServer(corpus.pages)
        base_url = srv.start()      # blocks until the socket is bound
        ...                          # crawl base_url
        srv.stop()                   # always call (driver does this in finally)

    Also usable as a context manager for direct unit tests.
    """

    def __init__(self, pages: dict[str, bytes], *, startup_timeout: float = 10.0):
        self._pages = pages
        self._startup_timeout = startup_timeout
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self.base_url: str | None = None

    def start(self) -> str:
        self._thread = threading.Thread(
            target=self._serve, name='wxbench-fixture', daemon=True
        )
        self._thread.start()
        if not self._ready.wait(self._startup_timeout):
            raise RuntimeError('fixture server did not start within '
                               f'{self._startup_timeout}s')
        if self._error is not None:
            raise RuntimeError(f'fixture server failed to start: {self._error!r}')
        assert self.base_url is not None
        return self.base_url

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if self._runner is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._runner.cleanup(), loop)
                fut.result(timeout=10)
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self) -> 'FixtureServer':
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- internals (run on the server thread) ------------------------------- #
    async def _handle(self, request: web.Request) -> web.Response:
        body = self._pages.get(request.path)
        if body is None:
            return web.Response(status=404, body=b'not found')
        return web.Response(body=body, content_type='text/html')

    async def _setup(self) -> None:
        app = web.Application()
        app.router.add_route('GET', '/{tail:.*}', self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self._runner = runner
        # Ephemeral port chosen by the OS; read it back from the bound socket.
        host, port = runner.addresses[0][:2]
        self.base_url = f'http://127.0.0.1:{port}/'

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._setup())
        except BaseException as exc:  # noqa: BLE001 — surface to start()
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
