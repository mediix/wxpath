"""End-to-end parity: the memory and sqlite frontiers must yield the same crawl.

Runs the same deep-crawl expression against the deterministic fixture server with
each backend and asserts identical coverage (same set of fetched pages, same
count). This is the engine-level proof that the persistent backend is faithful,
not just the in-memory wrapper (Invariant I5 / acceptance §12).
"""

import pytest

from benchmarks.server import FixtureServer, build_corpus
from wxpath.core.runtime.engine import WXPathEngine, wxpath_async_blocking
from wxpath.http.client.crawler import Crawler
from wxpath.http.frontier.memory import InMemoryFrontier
from wxpath.http.frontier.sqlite import SQLiteFrontier


def _crawl(expr: str, max_depth: int, frontier) -> list[str]:
    """Run a crawl with a specific frontier; return the sorted fetched-page URLs."""
    engine = WXPathEngine(
        crawler=Crawler(
            respect_robots=False,
            auto_throttle_start_delay=0.0,   # neutralize politeness sleeps (per-instance,
            auto_throttle_max_delay=0.0,     # no global SETTINGS mutation)
        ),
        frontier=frontier,
    )
    results = wxpath_async_blocking(expr, max_depth=max_depth, engine=engine)
    # Each ///url(//@href) result is the fetched page element; base_url is its URL.
    return sorted((getattr(r, "base_url", None) or str(r)) for r in results)


def test_memory_and_sqlite_frontiers_agree(tmp_path):
    corpus = build_corpus(page_count=7, fan_out=2, filler_bytes=0)
    with FixtureServer(corpus.pages) as srv:
        expr = f"url('{srv.base_url}')///url(//@href)"
        mem = _crawl(expr, corpus.tree_height, InMemoryFrontier())
        sql = _crawl(expr, corpus.tree_height, SQLiteFrontier(path=str(tmp_path / "f.db")))

    assert len(mem) == corpus.expected_extractions     # every node but the root
    assert mem == sql                                  # identical coverage, both backends


def test_memory_frontier_is_reproducible(tmp_path):
    corpus = build_corpus(page_count=7, fan_out=2, filler_bytes=0)
    with FixtureServer(corpus.pages) as srv:
        expr = f"url('{srv.base_url}')///url(//@href)"
        first = _crawl(expr, corpus.tree_height, InMemoryFrontier())
        second = _crawl(expr, corpus.tree_height, InMemoryFrontier())
    assert first == second
