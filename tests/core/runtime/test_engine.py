from __future__ import annotations

import asyncio

import pytest

from tests.utils import MockCrawler, MockCrawlerWithErrors
from wxpath.core.runtime import engine
from wxpath.core.runtime.engine import WXPathEngine
from wxpath.http.client.request import Request
from wxpath.http.client.response import Response


def _generate_fake_fetch_html(pages: dict[str, bytes]):
    """Return a stub that replaces `core.fetch_html`."""
    def _fake_fetch_html(url: str) -> bytes:
        try:
            return pages[url]
        except KeyError as exc:
            raise AssertionError(f"Unexpected URL fetched: {url!r}") from exc
    return _fake_fetch_html


async def _collect_async(gen):
    """Consume an **async** generator and return a list of its items."""
    return [item async for item in gen]


class _FakeCrawlerWithStatus:
    """Minimal crawler stub that lets us control response status codes."""
    def __init__(self, responses_by_url):
        self._q = asyncio.Queue()
        self._responses_by_url = responses_by_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def submit(self, request):
        resp = self._responses_by_url.get(request.url)
        if resp is None:
            raise AssertionError(f"Unexpected URL fetched: {request.url!r}")
        self._q.put_nowait(resp)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# M3 — declarative scoring (priority=) end-to-end
# --------------------------------------------------------------------------- #
# A page whose links are in document order low, high, mid but carry data-rank
# 1, 9, 5. Pure BFS fetches them in document order; a priority= score reorders
# the frontier so the highest-ranked page is fetched first.
_SCORING_PAGES = {
    'http://test/': b"""<html><body>
        <a href="low.html"  data-rank="1">low</a>
        <a href="high.html" data-rank="9">high</a>
        <a href="mid.html"  data-rank="5">mid</a>
    </body></html>""",
    'http://test/low.html':  b"<html><body><p>low</p></body></html>",
    'http://test/high.html': b"<html><body><p>high</p></body></html>",
    'http://test/mid.html':  b"<html><body><p>mid</p></body></html>",
}


def _run_scoring(monkeypatch, expr, max_depth=1):
    monkeypatch.setattr(
        engine, "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=_SCORING_PAGES, **k),
    )
    return asyncio.run(_collect_async(engine.WXPathEngine().run(expr, max_depth=max_depth)))


def test_engine_priority_orders_fetches(monkeypatch):
    """Dynamic priority=: highest @data-rank is fetched first (not doc order)."""
    expr = "url('http://test/')//url(//a/@href, priority=number(./@data-rank))"
    results = _run_scoring(monkeypatch, expr)
    assert [e.base_url for e in results] == [
        "http://test/high.html",  # rank 9
        "http://test/mid.html",   # rank 5
        "http://test/low.html",   # rank 1
    ]


def test_engine_constant_priority_keeps_bfs_order(monkeypatch):
    """A constant priority= ties every link, so the seq tiebreak ⇒ BFS order."""
    expr = "url('http://test/')//url(//a/@href, priority=5)"
    results = _run_scoring(monkeypatch, expr)
    assert [e.base_url for e in results] == [
        "http://test/low.html",
        "http://test/high.html",
        "http://test/mid.html",
    ]


def test_engine_no_priority_is_pure_bfs(monkeypatch):
    """Regression / I5: no priority= ⇒ document (BFS) order, unchanged."""
    expr = "url('http://test/')//url(//a/@href)"
    results = _run_scoring(monkeypatch, expr)
    assert [e.base_url for e in results] == [
        "http://test/low.html",
        "http://test/high.html",
        "http://test/mid.html",
    ]


def test_engine_priority_is_deterministic(monkeypatch):
    """Same page + same expression ⇒ identical order on every run."""
    expr = "url('http://test/')//url(//a/@href, priority=number(./@data-rank))"
    first = [e.base_url for e in _run_scoring(monkeypatch, expr)]
    second = [e.base_url for e in _run_scoring(monkeypatch, expr)]
    assert first == second == [
        "http://test/high.html", "http://test/mid.html", "http://test/low.html"]


def test_engine_priority_changes_order_not_data(monkeypatch):
    """Invariant I4: scoring changes traversal ORDER, never extracted DATA.

    The same pages are visited and the same map{} is extracted regardless of
    priority; only the order differs.
    """
    tail = "/map { 'p': string((//p/text())[1]) }"
    scored = _run_scoring(
        monkeypatch,
        "url('http://test/')//url(//a/@href, priority=number(./@data-rank))" + tail)
    bfs = _run_scoring(
        monkeypatch, "url('http://test/')//url(//a/@href)" + tail)

    scored = [dict(r.items()) for r in scored]
    bfs = [dict(r.items()) for r in bfs]

    assert scored != bfs                       # order differs...
    assert sorted(map(repr, scored)) == sorted(map(repr, bfs))  # ...data identical
    assert scored[0] == {"p": "high"}          # high-rank page extracted first


# --- M4a: provenance scoring end-to-end -----------------------------------

_PROVENANCE_PAGES = {
    'http://test/': b"""<html><body>
        <footer><a href="foot.html">footer link</a></footer>
        <main><a href="main.html">main link</a></main>
    </body></html>""",
    'http://test/main.html': b"<html><body><p>main</p></body></html>",
    'http://test/foot.html': b"<html><body><p>foot</p></body></html>",
}


def _run_provenance(monkeypatch, expr, pages=_PROVENANCE_PAGES, max_depth=1):
    monkeypatch.setattr(
        engine, "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    return asyncio.run(_collect_async(engine.WXPathEngine().run(expr, max_depth=max_depth)))


def test_engine_provenance_main_before_footer(monkeypatch):
    """M4a: ./wx:parent-tag() promotes the <main> link over the <footer> link.

    The footer link appears first in document order, so BFS would fetch it
    first; provenance scoring flips that — main is crawled before footer.
    """
    expr = ("url('http://test/')//url(//a/@href, "
            "priority=(if (./wx:parent-tag() = 'main') then 10 else 1))")
    results = _run_provenance(monkeypatch, expr)
    assert [e.base_url for e in results] == [
        "http://test/main.html",   # parent-tag main ⇒ score 10
        "http://test/foot.html",   # parent-tag footer ⇒ score 1
    ]


def test_engine_wx_depth_in_priority_regression(monkeypatch):
    """Regression (M4 §0 / corrects M3 §4.3): a `wx:` function with a leading
    axis evaluates inside priority= without raising XPathContextRequired."""
    # depth at discovery is 1 (links found on the depth-0 root); 10 - 1 = 9.
    expr = "url('http://test/')//url(//a/@href, priority=10 - /wx:depth())"
    results = _run_provenance(monkeypatch, expr)
    # Both links score equally (same depth) ⇒ no crash, BFS tiebreak preserved.
    assert {e.base_url for e in results} == {
        "http://test/main.html", "http://test/foot.html"}


# --- M5: semantic frontier scoring end-to-end -----------------------------

# Root links, in document order: an OFF-topic link first, an ON-topic link second.
# Pure BFS fetches them in document order (off, on). A semantic scorer keyed to the
# objective reorders the frontier so the on-topic page is fetched first — while the
# extracted data stays identical (Invariant I4/I10).
_SEMANTIC_PAGES = {
    'http://test/': b"""<html><body>
        <a href="off.html">celebrity gossip news</a>
        <a href="on.html">lithium battery recycling guide</a>
    </body></html>""",
    'http://test/on.html':  b"<html><body><p>on</p></body></html>",
    'http://test/off.html': b"<html><body><p>off</p></body></html>",
}

_SEMANTIC_VOCAB = ["lithium", "battery", "recycling", "guide",
                   "celebrity", "gossip", "news"]


class _BagOfWordsEmbedder:
    """Deterministic 0/1 bag-of-words embedder over a fixed vocab (no ML dep)."""

    def __init__(self, vocab):
        self._vocab = list(vocab)

    def embed(self, texts):
        return [[1.0 if v in set(t.lower().split()) else 0.0 for v in self._vocab]
                for t in texts]


def _run_semantic(monkeypatch, expr, objective, max_depth=1):
    from wxpath.settings import SETTINGS
    frontier = SETTINGS.http.client.frontier
    monkeypatch.setitem(frontier, "scorer", "semantic")
    monkeypatch.setitem(frontier, "objective", objective)
    monkeypatch.setattr(
        "wxpath.http.frontier.embedders.get_embedder",
        lambda cfg: _BagOfWordsEmbedder(_SEMANTIC_VOCAB),
    )
    monkeypatch.setattr(
        engine, "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=_SEMANTIC_PAGES, **k),
    )
    return asyncio.run(_collect_async(engine.WXPathEngine().run(expr, max_depth=max_depth)))


def test_engine_semantic_orders_on_topic_first(monkeypatch):
    """scorer=semantic: the on-topic link is fetched before the off-topic one,
    though it appears second in document order (BFS would fetch off first)."""
    expr = "url('http://test/')//url(//a/@href)"
    results = _run_semantic(monkeypatch, expr, objective="lithium battery recycling")
    assert [e.base_url for e in results] == [
        "http://test/on.html",    # on-topic anchor ⇒ higher cosine ⇒ popped first
        "http://test/off.html",   # off-topic anchor ⇒ cosine 0 ⇒ popped last
    ]


def test_engine_semantic_changes_order_not_data(monkeypatch):
    """Invariant I4/I10: the semantic scorer changes ORDER, never extracted DATA.

    The same map{} records are produced as a plain BFS crawl; only order differs.
    """
    tail = "/map { 'p': string((//p/text())[1]) }"
    expr = "url('http://test/')//url(//a/@href)" + tail
    semantic = _run_semantic(monkeypatch, expr, objective="lithium battery recycling")

    # Re-run the same pages with the default deterministic scorer → BFS order.
    from wxpath.settings import SETTINGS
    monkeypatch.setitem(SETTINGS.http.client.frontier, "scorer", "deterministic")
    bfs = asyncio.run(_collect_async(engine.WXPathEngine().run(expr, max_depth=1)))

    semantic = [dict(r.items()) for r in semantic]
    bfs = [dict(r.items()) for r in bfs]
    assert semantic != bfs                                        # order differs...
    assert sorted(map(repr, semantic)) == sorted(map(repr, bfs))  # ...data identical
    assert semantic[0] == {"p": "on"}                             # on-topic extracted first


# --- M4b: trap detection end-to-end ---------------------------------------

_TRAP_PAGES = {
    # root links into a self-deepening period-2 cycle AND a legit deep chain
    'http://t/': b'<html><body>'
                 b'<a href="http://t/loop/a/b">loop</a>'
                 b'<a href="http://t/docs/d1">docs</a>'
                 b'</body></html>',
    'http://t/loop/a/b': b'<html><body>'
                         b'<a href="http://t/loop/a/b/a/b">deeper</a></body></html>',
    'http://t/loop/a/b/a/b': b'<html><body>'
                             b'<a href="http://t/loop/a/b/a/b/a/b">deeper</a></body></html>',
    'http://t/loop/a/b/a/b/a/b': b'<html><body><p>trap leaf</p></body></html>',
    'http://t/docs/d1': b'<html><body>'
                        b'<a href="http://t/docs/d1/d2">d2</a></body></html>',
    'http://t/docs/d1/d2': b'<html><body>'
                           b'<a href="http://t/docs/d1/d2/d3">d3</a></body></html>',
    'http://t/docs/d1/d2/d3': b'<html><body><p>docs leaf</p></body></html>',
}


def _run_trap(monkeypatch, frontier, max_depth=3):
    monkeypatch.setattr(
        engine, "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=_TRAP_PAGES, **k),
    )
    eng = WXPathEngine(frontier=frontier)
    expr = "url('http://t/')///url(//a/@href)"
    results = asyncio.run(_collect_async(eng.run(expr, max_depth=max_depth)))
    return {e.base_url for e in results}


def test_engine_trap_pruned_legit_chain_survives(monkeypatch):
    """M4b acceptance: the URL-path-repeat trap is pruned at the threshold while a
    legitimate deep chain of equal length is crawled in full."""
    from wxpath.http.frontier.memory import InMemoryFrontier
    from wxpath.http.frontier.trap import TrapFilterFrontier

    frontier = TrapFilterFrontier(
        InMemoryFrontier(), max_path_repeat=2, max_period=4)
    crawled = _run_trap(monkeypatch, frontier)

    # period-2 cycle a/b repeated 3× (reps=3 > 2) is dropped at push...
    assert "http://t/loop/a/b/a/b/a/b" not in crawled
    assert frontier.dropped == 1
    # ...but the shallower loop pages (reps ≤ 2) and the full legit chain survive.
    assert "http://t/loop/a/b/a/b" in crawled
    assert "http://t/docs/d1/d2/d3" in crawled


def test_engine_no_trap_filter_crawls_everything(monkeypatch):
    """Contrast: without the filter the same trap URL IS fetched — proving the
    prune above is the filter's doing, not a missing fixture page."""
    from wxpath.http.frontier.memory import InMemoryFrontier

    crawled = _run_trap(monkeypatch, InMemoryFrontier())
    assert "http://t/loop/a/b/a/b/a/b" in crawled
    assert "http://t/docs/d1/d2/d3" in crawled


def test_engine_run__crawl(monkeypatch):
    """A single `url()` segment should yield the parsed root element."""
    pages = {
        "http://test/": b"<html><body><p>Hello</p></body></html>",
    }

    # Monkey‑patch network helpers.
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )

    expr = "url('http://test/')"

    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )

    assert len(results) == 1
    root = results[0]
    assert root.get("depth") == "0"
    assert root.base_url == "http://test/"


def test_engine_run__crawl__crawl_with_xpath(monkeypatch):
    """
    Root page contains two links; both should be fetched concurrently at depth 1.
    """
    pages = {
        "http://test/": b"""
            <html><body>
              <a href="a.html">A</a>
              <a href="b.html">B</a>
            </body></html>
        """,
        "http://test/a.html": b"<html><body><p>A</p></body></html>",
        "http://test/b.html": b"<html><body><p>B</p></body></html>",
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()

    expr = "url('http://test/')//url(//@href)"

    
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=1)
        )
    )

    # Order should follow BFS discovery order.
    assert [e.base_url for e in results] == [
        "http://test/a.html",
        "http://test/b.html",
    ]
    assert all(e.get("depth") == "1" for e in results)


def test_engine_run__crawl_with_follow__extract(monkeypatch):
    """
    There are multiple links on the page; only one should be followed until the end.
    """
    pages = {
        "http://test/": b"""
            <html><body>
              <div class="quote"><p>"The only true wisdom is in knowing you know nothing."</p></div>
              <a class="next" href="a.html">A</a>
              <a href="X.html">X</a>
            </body></html>
        """,
        "http://test/a.html": b"""
            <html><body>
              <div class="quote"><p>"Knowing yourself is the beginning of all wisdom."</p></div>
              <a class="next" href="b.html">B</a>
              <a href="X.html">X</a>
            </body></html>
        """,
        "http://test/b.html": b"""
            <html><body>
              <div class="quote">
                <p>"There is only one good, knowledge, and one evil, ignorance."</p>
              </div>
              <a href="X.html">X</a>
            </body></html>
        """,
        "http://test/X.html": b"""
            <html><body>
              <div class="quote">
                <p>"You shall not pass!"</p>
              </div>
            </body></html>
        """,
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()

    expr = """
        url('http://test/', follow=//a[@class='next']/@href)
    """
    
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )

    assert [e.base_url for e in results] == [
        "http://test/",
        "http://test/a.html",
        "http://test/b.html",
    ]


def test_engine_run__crawl__crawl_with_xpath_2(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <main>
                <a href="a1.html">A</a>
                <a href="a2.html">B</a>
              </main>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a1.html': b"<html><body><p>Page A1</p></body></html>",
        'http://test/a2.html': b"<html><body><p>Page A2</p></body></html>",
        'http://test/b.html': b"<html><body><p>Page B</p></body></html>",
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()

    expr = "url('http://test/')//url(//main//a/@href)"
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=1)
        )
    )

    assert len(results) == 2
    assert results[0].get('depth') == '1'
    assert results[1].get('depth') == '1'
    assert {e.base_url for e in results} == {
        'http://test/a1.html',
        'http://test/a2.html',
    }


def test_engine_run__crawl__crawl_with_xpath_3(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <main>
                <a href="a1.html">A</a>
                <a href="a2.html">B</a>
                <a href="http://different/a3.html">C</a>
              </main>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a1.html': b"<html><body><p>Page A1</p></body></html>",
        'http://test/a2.html': b"<html><body><p>Page A2</p></body></html>",
        'http://test/b.html': b"<html><body><p>Page B</p></body></html>",
        'http://different/a3.html': b"<html><body><p>Page A3</p></body></html>",
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()

    expr = "url('http://test/')//url(//main//a/@href)"
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=1)
        )
    )

    assert len(results) == 3
    assert results[0].get('depth') == '1'
    assert results[1].get('depth') == '1'
    assert results[2].get('depth') == '1'
    assert {e.base_url for e in results} == {
        'http://test/a1.html',
        'http://test/a2.html',
        'http://different/a3.html'
    }


def test_engine_run__crawl__xpath__crawl_with_xpath(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <main>
                <a href="a1.html">A</a>
                <a href="a2.html">B</a>
              </main>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a1.html': b"<html><body><p>Page A1</p></body></html>",
        'http://test/a2.html': b"<html><body><p>Page A2</p></body></html>",
        'http://test/b.html': b"<html><body><p>Page B</p></body></html>",
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()

    expr = "url('http://test/')//main//a/url(@href)"
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=1)
        )
    )

    assert len(results) == 2
    assert results[0].get('depth') == '1'
    assert results[1].get('depth') == '1'
    assert {e.base_url for e in results} == {
        'http://test/a1.html',
        'http://test/a2.html',
    }


def test_engine_run__crawl__xpath__crawl_2(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <main>
                <a href="a1.html">A</a>
                <a href="a2.html">B</a>
              </main>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a1.html': b"<html><body><p>Page A1</p></body></html>",
        'http://test/a2.html': b"<html><body><p>Page A2</p></body></html>",
        'http://test/b.html': b"<html><body><p>Page B</p></body></html>",
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()

    expr = "url('http://test/')//main//a/@href/url(.)"
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=1)
        )
    )

    assert len(results) == 2
    assert results[0].get('depth') == '1'
    assert results[1].get('depth') == '1'
    assert {e.base_url for e in results} == {
        'http://test/a1.html',
        'http://test/a2.html',
    }


def test_engine_run__crawl__crawl__crawl(monkeypatch):
    pages = {
      'http://test/': b"<html><body><a href='lvl1.html'>L1</a></body></html>",
      'http://test/lvl1.html': b"<html><body><a href='lvl2.html'>L2</a></body></html>",
      'http://test/lvl2.html': b"<html><body><p>Reached L2</p></body></html>",
    }
    expr = "url('http://test/')//url(//@href)//url(//@href)"

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )

    assert len(results) == 1
    assert results[0].get('depth') == '2'
    assert results[0].base_url == 'http://test/lvl2.html'


def test_engine_run__crawl__crawl_with_xpath__xpath(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <a href="a.html">A</a>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a.html': b"<html><body><a href='page1.html'>L2</a></body></html>",
        'http://test/b.html': b"<html><body><a href='page2.html'>L2</a></body></html>",
    }
    
    expr = "url('http://test/')//url(//@href)//a/@href"
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=1)
        )
    )

    assert len(results) == 2
    assert results == [
        'page1.html',
        'page2.html',
    ]


def test_engine_run__crawl__crawl_with_xpath__crawl_with_xpath__xpath(monkeypatch):
    pages = {
      'http://test/': b"<html><body><a href='lvl1.html'>L1</a></body></html>",
      'http://test/lvl1.html': b"<html><body><a href='lvl2.html'>L2</a></body></html>",
      'http://test/lvl2.html': b"<html><body><a href='lvl3.html'>L3</a></body></html>",
      'http://test/lvl3.html': b"<html><body><p>Reached L3</p></body></html>",
    }

    expr = "url('http://test/')//url(//@href)//url(//@href)//a/@href"

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )
    assert len(results) == 1
    assert results[0] == 'lvl3.html'


def test_engine_run__crawl_four_levels_and_query_and_max_depth_2(monkeypatch):
    pages = {
      'http://test/': b"<html><body><a href='lvl1.html'>L1</a></body></html>",
      'http://test/lvl1.html': b"<html><body><a href='lvl2.html'>L2</a></body></html>",
      'http://test/lvl2.html': b"<html><body><a href='lvl3.html'>L3</a></body></html>",
      'http://test/lvl3.html': b"<html><body><a href='lvl4.html'>L4</a></body></html>",
      'http://test/lvl4.html': b"<html><body><a href='lvl5.html'>L4</a></body></html>",
    }

    expr = "url('http://test/')//url(//@href)//url(//@href)//a/@href"
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )
    assert len(results) == 1
    assert results[0] == 'lvl3.html'


# Test multiple crawls with filtered (e.g., `url(@href[starts-with(., '/wiki/')])`) crawl
def test_engine_run__filtered_crawl(monkeypatch):
    pages = {
      'http://test/': b"""
            <html><body>
              <a href="lvl1a.html">A</a>
              <a href="lvl1b.html">B</a>
            </body></html>
        """,
      'http://test/lvl1a.html': b"<html><body><a href='lvl2.html'>L2</a></body></html>",
      'http://test/lvl1b.html': b"<html><body><a href='lvl99999.html'>L99999</a></body></html>",
      'http://test/lvl2.html': b"<html><body><a href='lvl3.html'>L3</a></body></html>",
      'http://test/lvl3.html': b"<html><body><p>Reached L3</p></body></html>",
    }
    
    expr = "url('http://test/')//url(//@href[starts-with(., 'lvl1a')])//a/@href"
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )
    assert len(results) == 1
    assert results[0] == 'lvl2.html'


# Test infinite crawl using ///url()
def test_engine_run__infinite_crawl_max_depth_uncapped(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <a href="a.html">A</a>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a.html': b"<html><body><a href='a1.html'>L2</a></body></html>",
        'http://test/b.html': b"<html><body><a href='b1.html'>L2</a></body></html>",
        'http://test/a1.html': b"<html><body></body></html>",
        'http://test/b1.html': b"<html><body></body></html>",
    }
    
    expr = "url('http://test/')///url(//@href)"
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=9999)
        )
    )

    assert len(results) == 4
    assert [e.base_url for e in results] == [
        'http://test/a.html',
        'http://test/b.html',
        'http://test/a1.html',
        'http://test/b1.html',
    ]


def test_engine_run__infinite_crawl_max_depth_1(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <a href="a.html">A</a>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a.html': b"<html><body><a href='a1.html'>L2</a></body></html>",
        'http://test/b.html': b"<html><body><a href='b1.html'>L2</a></body></html>",
        'http://test/a1.html': b"<html><body><a href='a2.html'>L3</a></body></html>",
        'http://test/b1.html': b"<html><body><a href='b2.html'>L3</a></body></html>",
    }
    expr = "url('http://test/')///url(//@href)"
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=1)
        )
    )
    assert len(results) == 2
    assert [e.base_url for e in results] == [
        'http://test/a.html',
        'http://test/b.html',
    ]


@pytest.mark.asyncio
async def test_engine_allow_redirects_accepts_3xx(monkeypatch):
    """When allow_redirects=True, 3xx responses are accepted."""
    target_url = "http://redirect.test/"
    responses = {
        target_url: Response(
            request=Request(target_url),
            status=301,
            body=b"<html><body><p>redir</p></body></html>",
            headers={},
        )
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: _FakeCrawlerWithStatus(responses),
    )

    eng = WXPathEngine(allow_redirects=True)
    results = await _collect_async(eng.run(f"url('{target_url}')", max_depth=0))

    assert len(results) == 1
    assert getattr(results[0], "base_url", None) == target_url


@pytest.mark.asyncio
async def test_engine_allow_redirects_false_drops_3xx(monkeypatch):
    """When allow_redirects=False, 3xx responses are filtered out."""
    target_url = "http://redirect.test/"
    responses = {
        target_url: Response(
            request=Request(target_url),
            status=302,
            body=b"<html><body><p>redir</p></body></html>",
            headers={},
        )
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: _FakeCrawlerWithStatus(responses),
    )

    eng = WXPathEngine(allow_redirects=False)
    results = await _collect_async(eng.run(f"url('{target_url}')", max_depth=0))

    assert results == []


def test_engine_run__infinite_crawl__query__max_depth_1(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <a href="a.html">A</a>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a.html': b"<html><body><a href='a1.html'>L2</a></body></html>",
        'http://test/b.html': b"<html><body><a href='b1.html'>L2</a></body></html>",
        'http://test/a1.html': b"<html><body><a href='a2.html'>L3</a></body></html>",
        'http://test/b1.html': b"<html><body><a href='b2.html'>L3</a></body></html>",
    }
    
    expr = "url('http://test/')///url(//@href)//a/@href"
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=1)
        )
    )

    assert len(results) == 2
    assert results == [
        'a1.html',
        'b1.html',
    ]


# # TODO: refactor with fixtures
def test_engine_run__crawl__inf_crawl__query__max_depth_2(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <a href="a.html">A</a>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a.html': b"<html><body><a href='a1.html'>L2</a></body></html>",
        'http://test/b.html': b"<html><body><a href='b1.html'>L2</a></body></html>",
        'http://test/a1.html': b"<html><body><a href='a2.html'>L3</a></body></html>",
        'http://test/b1.html': b"<html><body><a href='b2.html'>L3</a></body></html>",
    }
    expr = "url('http://test/')///url(//@href)//a/@href"
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )
    assert len(results) == 4
    assert results == [
        'a1.html',
        'b1.html',
        'a2.html',
        'b2.html',
    ]


def test_engine_run__crawl__inf_crawl__query__dupe_link__max_depth_2(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <a href="a.html">A</a>
              <a href="b.html">B</a>
              <a href="a.html">A dupe</a>
            </body></html>
        """,
        'http://test/a.html': b"<html><body><a href='a1.html'>L2</a></body></html>",
        'http://test/b.html': b"<html><body><a href='b1.html'>L2</a></body></html>",
        'http://test/a1.html': b"<html><body><a href='a2.html'>L3</a></body></html>",
        'http://test/b1.html': b"<html><body><a href='b2.html'>L3</a></body></html>",
    }
    expr = "url('http://test/')///url(//@href)//a/@href"
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )
    assert len(results) == 4
    assert results == [
        'a1.html',
        'b1.html',
        'a2.html',
        'b2.html',
    ]


def test_engine_run__inf_crawl__xpath_map__max_depth_2(monkeypatch):
    pages = {
        'http://test/': b"""
            <html>
                <body>
                    <a href='a.html'>A</a>
                    <a href='b.html'>B</a>
                </body>
            </html>""",
        'http://test/a.html': b"<html><body><a href='a1.html'>A1</a></body></html>",
        'http://test/b.html': b"<html><body><a href='b1.html'>B1</a></body></html>",
        'http://test/a1.html': b"<html><body><a href='a2.html'>A2</a></body></html>",
        'http://test/b1.html': b"<html><body><a href='b2.html'>B2</a></body></html>",
    }
    
    expr = """
        url('http://test/')
            ///url(//@href)
                /map { 'link_text': string(//a/text()) }
    """
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )

    results = [dict(r.items()) for r in results]

    assert len(results) == 4
    assert results == [
        {'link_text': 'A1'},
        {'link_text': 'B1'},
        {'link_text': 'A2'},
        {'link_text': 'B2'},
    ]


def test_engine_run__xpath_fn_map_frag__crawl(monkeypatch):
    pages = {
        'http://test/1': b"""
            <html><body></body></html>""",
        'http://test/2': b"""
            <html><body></body></html>""",
        'http://test/3': b"""
            <html><body></body></html>""",
    }
    expr = "(1 to 3) ! ('http://test/' || .) ! url(.)"
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )
    assert len(results) == 3
    assert set(r.base_url for r in results) == {
        'http://test/1',
        'http://test/2',
        'http://test/3',
    }


# NOTE: I'm considering removing the wxpath expr equality of
# ///main//a/url(@href) and url(//main//a/@href)
# Test evaluate_wxpath_bfs_iter() with filtered (argument) infinite crawl - type 1
# def test_engine_run__infinite_crawl_with_inf_filter_before_url_op(monkeypatch):
#     pages = {
#         'http://test/': b"""
#             <html><body>
#               <main><a href="a.html">A</a></main>
#               <a href="b.html">B</a>
#             </body></html>
#         """,
#         'http://test/a.html':
#             b"<html><body><main><a href='a1.html'>Depth 1</a></main></body></html>",
#         'http://test/b.html':
#             b"<html><body><main><a href='b1.html'>Depth 1</a></main></body></html>",
#         'http://test/a1.html': b"<html><body><a href='a2.html'>Depth 2</a></body></html>",
#         'http://test/b1.html': b"<html><body><a href='b2.html'>Depth 2</a></body></html>",
#     }
    
#     expr = "url('http://test/')///main/a/url(//@href)"

#     monkeypatch.setattr(
#         engine,
#         "Crawler",
#         lambda *a, **k: MockCrawler(*a, pages=pages, **k),
#     )
#     eng = engine.WXPathEngine()
#     results = asyncio.run(
#         _collect_async(
#             eng.run(expr, max_depth=2)
#         )
#     )
    
#     assert len(results) == 2
#     assert [e.base_url for e in results] == [
#         'http://test/a.html',
#         'http://test/a1.html'
#     ]
    

# NOTE: I'm considering removing the wxpath expr equality of
# ///main//a/url(@href) and url(//main//a/@href)
# Test evaluate_wxpath_bfs_iter() with filtered (argument) infinite crawl - type 1
# def test_engine_run___crawl_xpath_crawl_max_depth_1(monkeypatch):
#     pages = {
#         'http://test/': b"""
#             <html><body>
#               <main><a href="a.html">A</a></main>
#               <a href="b.html">B</a>
#             </body></html>
#         """,
#         'http://test/a.html': b"<html><body><main><a href='a1.html'>L2</a></main></body></html>",
#         'http://test/b.html': b"<html><body><main><a href='b1.html'>L2</a></main></body></html>",
#         'http://test/a1.html': b"<html><body><a href='a2.html'>L3</a></body></html>",
#         'http://test/b1.html': b"<html><body><a href='b2.html'>L3</a></body></html>",
#     }
#     expr = "url('http://test/')///main/a/url(//@href)"

#     monkeypatch.setattr(
#         engine,
#         "Crawler",
#         lambda *a, **k: MockCrawler(*a, pages=pages, **k),
#     )
#     eng = engine.WXPathEngine()
#     results = asyncio.run(
#         _collect_async(
#             eng.run(expr, max_depth=1)
#         )
#     )
#     assert len(results) == 1
#     assert results[0].get('depth') == '1'
#     assert [e.base_url for e in results] == [
#         'http://test/a.html'
#     ]


# Test evaluate_wxpath_bfs_iter() with filtered (argument) infinite crawl - type 2
def test_engine_run___crawl_inf_crawl_with_filter(monkeypatch):
    pages = {
        'http://test/': b"""
            <html><body>
              <main><a href="a.html">A</a></main>
              <a href="b.html">B</a>
            </body></html>
        """,
        'http://test/a.html': b"<html><body><main><a href='a1.html'>L2</a></main></body></html>",
        'http://test/b.html': b"<html><body><main><a href='b1.html'>L2</a></main></body></html>",
        'http://test/a1.html': b"<html><body><a href='a2.html'>L3</a></body></html>",
        'http://test/b1.html': b"<html><body><a href='b2.html'>L3</a></body></html>",
    }
    expr = "url('http://test/')///url(//main/a/@href)"
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )
    eng = engine.WXPathEngine()
    results = asyncio.run(
        _collect_async(
            eng.run(expr, max_depth=2)
        )
    )
    assert len(results) == 2
    assert [e.get('depth') for e in results if e.base_url == 'http://test/a.html'] == ['1']
    assert [e.get('depth') for e in results if e.base_url == 'http://test/a1.html'] == ['2']
    assert [e.base_url for e in results] == [
        'http://test/a.html',
        'http://test/a1.html'
    ]

# -----------------------------
# Test: infinite crawl with max depth
# -----------------------------
@pytest.mark.asyncio
async def test_engine_infinite_crawl_max_depth(monkeypatch):
    pages = {
        "http://root/": b"<html><a href='a.html'>A</a><a href='b.html'>B</a></html>",
        "http://root/a.html": b"<html></html>",
        "http://root/b.html": b"<html></html>",
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )

    eng = WXPathEngine(concurrency=2)
    results = await _collect_async(eng.run("url('http://root/')///url(//@href)", max_depth=1))

    urls = [r.base_url for r in results]
    assert "http://root/a.html" in urls
    assert "http://root/b.html" in urls


# -----------------------------
# Test: engine does not hang on duplicate URLs
# -----------------------------
@pytest.mark.asyncio
async def test_engine_deduplicates_urls(monkeypatch):
    pages = {
        "http://root/": b"<html><a href='a.html'>A</a><a href='a.html'>A dup</a></html>",
        "http://root/a.html": b"<html></html>",
    }

    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawler(*a, pages=pages, **k),
    )

    eng = WXPathEngine(concurrency=2)
    results = await _collect_async(eng.run("url('http://root/')///url(//@href)", max_depth=1))

    urls = [r.base_url for r in results]
    # Only one instance of the duplicate URL should be returned
    assert urls.count("http://root/a.html") == 1


# -----------------------------
# Test: yield_errors option
# -----------------------------
@pytest.mark.asyncio
async def test_yield_errors_network_error(monkeypatch):
    """Test that network errors are yielded when yield_errors=True."""
    target_url = "http://error.test/"
    error = ConnectionError("Connection refused")
    
    responses = {}
    error_responses = {target_url: error}
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawlerWithErrors(
            responses_by_url=responses,
            error_responses=error_responses
        ),
    )

    eng = WXPathEngine()
    results = await _collect_async(
        eng.run(f"url('{target_url}')", max_depth=0, yield_errors=True)
    )

    assert len(results) == 1
    assert isinstance(results[0], dict)
    assert results[0]["__type__"] == "error"
    assert results[0]["url"] == target_url
    assert results[0]["reason"] == "network_error"
    assert "exception" in results[0]
    assert str(error) in results[0]["exception"]


@pytest.mark.asyncio
async def test_yield_errors_network_error_disabled(monkeypatch):
    """Test that network errors are NOT yielded when yield_errors=False."""
    target_url = "http://error.test/"
    error = ConnectionError("Connection refused")
    
    responses = {}
    error_responses = {target_url: error}
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawlerWithErrors(
            responses_by_url=responses,
            error_responses=error_responses
        ),
    )

    eng = WXPathEngine()
    results = await _collect_async(
        eng.run(f"url('{target_url}')", max_depth=0, yield_errors=False)
    )

    # Should yield nothing when errors are not yielded
    assert len(results) == 0


@pytest.mark.asyncio
async def test_yield_errors_bad_status(monkeypatch):
    """Test that bad status codes are yielded when yield_errors=True."""
    target_url = "http://badstatus.test/"
    
    responses = {
        target_url: Response(
            request=Request(target_url),
            status=404,
            body=b"<html><body>Not Found</body></html>",
            headers={},
        )
    }
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawlerWithErrors(responses_by_url=responses),
    )

    eng = WXPathEngine(allowed_response_codes={200})
    results = await _collect_async(
        eng.run(f"url('{target_url}')", max_depth=0, yield_errors=True)
    )

    assert len(results) == 1
    assert isinstance(results[0], dict)
    assert results[0]["__type__"] == "error"
    assert results[0]["url"] == target_url
    assert results[0]["reason"] == "bad_status"
    assert results[0]["status"] == 404
    assert "body" in results[0]


@pytest.mark.asyncio
async def test_yield_errors_bad_status_empty_body(monkeypatch):
    """Test that empty body responses are yielded when yield_errors=True."""
    target_url = "http://emptybody.test/"
    
    responses = {
        target_url: Response(
            request=Request(target_url),
            status=200,
            body=b"",  # Empty body
            headers={},
        )
    }
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawlerWithErrors(responses_by_url=responses),
    )

    eng = WXPathEngine()
    results = await _collect_async(
        eng.run(f"url('{target_url}')", max_depth=0, yield_errors=True)
    )

    assert len(results) == 1
    assert isinstance(results[0], dict)
    assert results[0]["__type__"] == "error"
    assert results[0]["url"] == target_url
    assert results[0]["reason"] == "bad_status"
    assert results[0]["status"] == 200


@pytest.mark.asyncio
async def test_yield_errors_unexpected_response(monkeypatch):
    """Test that unexpected responses are yielded when yield_errors=True."""
    expected_url = "http://expected.test/"
    unexpected_url = "http://unexpected.test/"
    
    # Normal response for expected URL
    responses = {
        expected_url: Response(
            request=Request(expected_url),
            status=200,
            body=b"<html><body>OK</body></html>",
            headers={},
        )
    }
    
    # Unexpected response (URL not in inflight dict - never submitted)
    # This will be yielded by the crawler before the expected response
    unexpected_responses = [
        Response(
            request=Request(unexpected_url),
            status=200,
            body=b"<html><body>Unexpected</body></html>",
            headers={},
        )
    ]
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawlerWithErrors(
            responses_by_url=responses,
            unexpected_responses=unexpected_responses
        ),
    )

    eng = WXPathEngine()
    # Submit a request for expected_url, but crawler will first yield unexpected response
    results = await _collect_async(
        eng.run(f"url('{expected_url}')", max_depth=0, yield_errors=True)
    )

    # Should get both the unexpected error and the normal result
    assert len(results) >= 1
    
    # Find the unexpected_response error
    error_results = [r for r in results if isinstance(r, dict) and r.get("__type__") == "error"]
    unexpected_errors = [
        r for r in error_results 
        if r.get("reason") == "unexpected_response"
    ]
    assert len(unexpected_errors) >= 1
    
    error_result = unexpected_errors[0]
    assert error_result["url"] == unexpected_url
    assert error_result["reason"] == "unexpected_response"
    assert "body" in error_result


@pytest.mark.asyncio
async def test_yield_errors_mixed_success_and_errors(monkeypatch):
    """Test that both successful results and errors are yielded when yield_errors=True."""
    root_url = "http://root.test/"
    success_url = "http://success.test/"
    error_url = "http://error.test/"
    
    responses = {
        root_url: Response(
            request=Request(root_url),
            status=200,
            body=f"""<html><body><a href="{success_url}">Success</a><a href="{error_url}">Error</a>
            </body></html>""".encode(),
            headers={},
        ),
        success_url: Response(
            request=Request(success_url),
            status=200,
            body=b"<html><body>Success</body></html>",
            headers={},
        )
    }
    
    error_responses = {
        error_url: ConnectionError("Network error")
    }
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawlerWithErrors(
            responses_by_url=responses,
            error_responses=error_responses
        ),
    )

    eng = WXPathEngine()
    
    # Create expression that will crawl root, then follow links to both success and error URLs
    expr = f"url('{root_url}')//url(//a/@href)"
    results = await _collect_async(
        eng.run(expr, max_depth=1, yield_errors=True)
    )

    # Should have at least one success result and one error
    assert len(results) >= 2
    
    # Check for success result (HTML element)
    success_results = [r for r in results if not isinstance(r, dict)]
    assert len(success_results) >= 1
    
    # Check for error result
    error_results = [r for r in results if isinstance(r, dict) and r.get("__type__") == "error"]
    assert len(error_results) >= 1
    
    # Verify error details
    error_result = error_results[0]
    assert error_result["reason"] == "network_error"
    assert error_result["url"] == error_url


@pytest.mark.asyncio
async def test_yield_errors_default_false(monkeypatch):
    """Test that yield_errors defaults to False (errors not yielded)."""
    target_url = "http://error.test/"
    error = ConnectionError("Connection refused")
    
    responses = {}
    error_responses = {target_url: error}
    
    monkeypatch.setattr(
        engine,
        "Crawler",
        lambda *a, **k: MockCrawlerWithErrors(
            responses_by_url=responses,
            error_responses=error_responses
        ),
    )

    eng = WXPathEngine()
    # Don't pass yield_errors, should default to False
    results = await _collect_async(
        eng.run(f"url('{target_url}')", max_depth=0)
    )

    # Should yield nothing when errors are not yielded (default)
    assert len(results) == 0


# -----------------------------
# Deadlock tests that need to be fixed
#  (likely need to rewrite the MockCrawler)
# -----------------------------

# -----------------------------
# Test: engine handles retry that eventually succeeds
# -----------------------------
# @pytest.mark.asyncio
# async def test_engine_retry_then_success(monkeypatch):
#     pages = {
#         "http://retry/": b"<html>success</html>",
#     }

#     crawler = MockCrawler(pages=pages)
#     original_submit = crawler.submit

#     # Make submit fail first time, then succeed
#     called = []

#     def submit_side_effect(req):
#         if not called:
#             called.append(True)
#             # Simulate retry by not putting response
#             return
#         original_submit(req)

#     crawler.submit = submit_side_effect

#     monkeypatch.setattr(engine, "Crawler", lambda *a, **k: crawler)

#     eng = WXPathEngine(concurrency=1)
#     results = await _collect_async(eng.run("url('http://retry/')", max_depth=0))

#     # Should eventually yield the page
#     assert results
#     assert b"success" in results[0].body


# -----------------------------
# Test: engine terminates cleanly with no pages
# -----------------------------
# @pytest.mark.asyncio
# async def test_engine_empty_pages(monkeypatch):
#     pages = {}

#     monkeypatch.setattr(
#         engine,
#         "Crawler",
#         lambda *a, **k: MockCrawler(*a, pages=pages, **k),
#     )

#     eng = WXPathEngine(concurrency=2)
#     results = await _collect_async(eng.run("url('http://root/')", max_depth=1))

#     # No results should be returned, but engine should not hang
#     assert results == []


# @pytest.mark.asyncio
# async def test_engine_does_not_hang_on_unexpected_response(monkeypatch):

#     pages = {
#         'http://test/': b"""
#             <html><body>
#               <main><a href="a.html">A</a></main>
#               <a href="b.html">B</a>
#             </body></html>
#         """
#     }

#     monkeypatch.setattr(
#         engine,
#         "Crawler",
#         lambda *a, **k: MockCrawler(*a, pages=pages, **k),
#     )
#     eng = engine.WXPathEngine()

#     # engine.crawler = crawler

#     async def run():
#         async for _ in eng.run("url('http://example.com')", max_depth=0):
#             pass

#     await asyncio.wait_for(run(), timeout=1.0)


# @pytest.mark.asyncio
# async def test_engine_does_not_hang_on_unexpected_response(monkeypatch):
#     class FakeRequest:
#         def __init__(self, url):
#             self.url = url


#     class FakeResponse:
#         def __init__(self, url, body=b"<html></html>", status=200, error=None):
#             self.request = FakeRequest(url)
#             self.body = body
#             self.status = status
#             self.error = error


#     class FakeCrawler:
#         def __init__(self, responses):
#             self._responses = asyncio.Queue()
#             for r in responses:
#                 self._responses.put_nowait(r)

#         async def __aenter__(self):
#             return self

#         async def __aexit__(self, *exc):
#             return False

#         def submit(self, request):
#             pass  # no-op for fake

#         def __aiter__(self):
#             return self

#         async def __anext__(self):
#             if self._responses.empty():
#                 await asyncio.sleep(3600)  # simulate hang
#             return await self._responses.get()
        
#     crawler = FakeCrawler([
#         FakeResponse("http://unexpected.com"),
#     ])

#     eng = engine.WXPathEngine()

#     eng.crawler = crawler

#     async def run():
#         async for _ in eng.run("url('http://example.com')", max_depth=0):
#             pass

#     await asyncio.wait_for(run(), timeout=1.0)



# @pytest.mark.asyncio
# async def test_engine_does_not_hang_on_request_failure(monkeypatch):
#     """
#     Verify that when a request fails after all retries, the engine
#     receives an error Response, yields an error result, and shuts down cleanly
#     instead of hanging.
#     """
#     failing_url = "http://will-always-fail.com"
#     expression = f"url('{failing_url}')"

#     eng = engine.WXPathEngine(concurrency=1)

#     # We patch the crawler's _fetch_one method to simulate a failed request
#     # that has exhausted all retries. The crawler should return a Response
#     # with an error, not raise an exception.
#     error = RuntimeError("All retries failed")
    
#     async def mock_fetch_one(self, req: Request):
#         if req.url == failing_url:
#             # Simulate crawler returning an error response after retries
#             return Response(request=req, status=0, body=b"", error=error)
#         # For any other URL, we can return a success to not interfere
#         return Response(request=req, status=200, body=b"<html></html>")

#     with patch('wxpath.http.client.crawler.Crawler._fetch_one', new=mock_fetch_one):
#         results = []
#         async for result in eng.run(expression, max_depth=0):
#             results.append(result)

#     assert len(results) == 1
#     assert results[0] == {'error': str(error), 'url': failing_url}

#     # The fact that the test completes without timing out proves the engine did not hang.


## Obsolete
# def test_engine_run__object_extraction(monkeypatch):
#     pages = {
#         'http://test/': b"""
#             <html><body>
#               <h1>The Test Page</h1>
#               <p>Alpha</p><p>Beta</p>
#             </body></html>
#         """
#     }

#     monkeypatch.setattr('wxpath.engine.helpers.fetch_html', _generate_fake_fetch_html(pages))

#     expr = (
#         "url('http://test/')/map { "
#         "'title'://h1/text()/string(), "
#         "'paragraphs'://p/text() "
#         "}"
#     )
#     segments = parse_wxpath_expr(expr)
#     results = list(evaluate_wxpath_bfs_iter(None, segments, max_depth=0))

#     assert len(results) == 1
#     obj = results[0]
#     assert obj['title'] == 'The Test Page'
#     assert obj['paragraphs'] == ['Alpha', 'Beta']


# def test_engine_run__object_indexing(monkeypatch):
#     pages = {
#         'http://test/': b"""
#             <html><body>
#               <p>One</p><p>Two</p><p>Three</p>
#             </body></html>
#         """
#     }

#     monkeypatch.setattr('wxpath.engine.helpers.fetch_html', _generate_fake_fetch_html(pages))

#     expr = (
#         "url('http://test/')/ map{ "
#         "'first':string((//p/text())[1]), "
#         "'second':string((//p/text())[2]), "
#         "'all'://p/text() "
#         "}"
#     )
#     segments = parse_wxpath_expr(expr)
#     results = list(evaluate_wxpath_bfs_iter(None, segments, max_depth=0))

#     assert len(results) == 1
#     obj = results[0]
#     assert obj['first'] == 'One'
#     assert obj['second'] == 'Two'
#     assert obj['all'] == ['One', 'Two', 'Three']

# Tests for WXPathEngine

# @pytest.mark.asyncio
# async def test_engine_deduplication():
#     """
#     Engine should not fetch the same URL twice.
#     """
#     pages = {
#         "http://test.com": 
#           b'<html><body><a href="/page">1</a><a href="/page">2</a></body></html>',
#         "http://test.com/page": b'<html><body>content</body></html>',
#     }
#     crawler = MockCrawler(pages=pages)
#     crawler.submit = MagicMock(wraps=crawler.submit)
#     engine = WXPathEngine()
#     engine.crawler = crawler
    
#     path_expr = "url('http://test.com')//url(@href)"
    
#     results = []
#     async for result in engine.run(path_expr, max_depth=1):
#         results.append(result)

#     # The crawler's submit method should have been called for 'http://test.com' and 'http://test.com/page'
#     assert crawler.submit.call_count == 2
#     urls_submitted = {call.args[0].url for call in crawler.submit.call_args_list}
#     assert urls_submitted == {"http://test.com", "http://test.com/page"}


# @pytest.mark.asyncio
# async def test_engine_handles_crawl_race_condition():
#     """
#     Test that the engine correctly handles a race condition where a URL is
#     queued for crawling multiple times before it has been marked as 'seen'.
#     """
#     pages = {
#         "http://test.com": b'<html><body><a href="http://test.com">self</a><a href="http://test.com/target">target</a></body></html>',
#         "http://test.com/target": b'<html><body>target page</body></html>',
#     }

#     crawler = MockCrawler(pages=pages)
#     crawler.submit = MagicMock(wraps=crawler.submit)
    
#     crawled_pages_queue = asyncio.Queue()

#     original_anext = crawler.__anext__
#     async def controlled_anext():
#         resp = await crawled_pages_queue.get()
#         if resp is None:
#             raise StopAsyncIteration
#         await asyncio.sleep(0) 
#         return resp
    
#     crawler.__anext__ = controlled_anext

#     engine = WXPathEngine()
#     engine.crawler = crawler
#     path_expr = "url('http://test.com')//url(@href)"

#     async def run_crawler():
#         results = []
#         async for result in engine.run(path_expr, max_depth=2):
#             results.append(result)
#         return results

#     crawler_task = asyncio.create_task(run_crawler())

#     await asyncio.sleep(0.01)
    
#     resp1 = await original_anext()
#     await crawled_pages_queue.put(resp1)
#     await asyncio.sleep(0.01)

#     resp2 = await original_anext()
#     await crawled_pages_queue.put(resp2)
    
#     await asyncio.sleep(0.01)
#     await crawled_pages_queue.put(None) 
    
#     results = await crawler_task
    
#     assert crawler.submit.call_count == 2
#     urls_submitted = {call.args[0].url for call in crawler.submit.call_args_list}
#     assert urls_submitted == {"http://test.com", "http://test.com/target"}
    
#     # We should get two results (the elements for the two pages)
#     result_urls = {r.base_url for r in results}
#     assert result_urls == {"http://test.com", "http://test.com/target"}
#     assert len(results) == 2

# @pytest.mark.asyncio
# async def test_engine_hangs_on_url_mismatch_with_non_terminating_crawler():
#     """
#     If a response URL does not match the inflight key AND
#     the crawler iterator never terminates, the engine hangs.
#     """

#     from wxpath.core.runtime.engine import WXPathEngine
#     from wxpath.http.client.response import Response
#     from wxpath.http.client.request import Request

#     class HangingMismatchCrawler:
#         async def __aenter__(self): return self
#         async def __aexit__(self, *_): pass

#         def submit(self, req):
#             self._submitted = True

#         def __aiter__(self):
#             async def gen():
#                 # First response: URL mismatch
#                 yield Response(
#                     Request("http://example.com/"),
#                     200,
#                     b"<html></html>"
#                 )
#                 # Then hang forever (like real crawler does)
#                 # while True:
#                 await asyncio.sleep(1)
#             return gen()

#     eng = WXPathEngine()
#     eng.crawler = HangingMismatchCrawler()

#     async def run():
#         async for _ in eng.run("url('http://example.com')", max_depth=0):
#             pass
    
#     with pytest.raises(asyncio.TimeoutError):
#         await asyncio.wait_for(run(), timeout=0.3)