"""Collectors: Recorder accumulation, BenchHook pairing, StatsSnapshot."""

from collections import defaultdict

import pytest

from benchmarks.collectors import (
    BenchHook,
    Recorder,
    StatsSnapshot,
    reset_url_stack,
    status_histogram_from_snapshot,
)
from wxpath.hooks.registry import FetchContext
from wxpath.http.stats import CrawlerStats


def _ctx(url: str, depth: int = 0) -> FetchContext:
    return FetchContext(url=url, backlink=None, depth=depth, segments=[])


def test_recorder_counts_fetch_parse_extract():
    reset_url_stack()
    rec = Recorder()
    hook = BenchHook(rec)

    ctx = _ctx('https://example.com/a')
    hook.post_fetch(ctx, b'<html><body><a href="x">x</a></body></html>')
    hook.post_parse(ctx, _DummyElem())
    hook.post_extract('x')
    hook.post_extract('y')

    assert 'https://example.com/a' in rec.urls_fetched
    assert 'https://example.com/a' in rec.urls_parsed
    assert rec.depths[0] == 1
    assert rec.extractions_per_url['https://example.com/a'] == 2
    assert 'https://example.com/a' in rec.urls_with_at_least_one_extraction


def test_recorder_records_latency_via_pairing():
    reset_url_stack()
    rec = Recorder()
    hook = BenchHook(rec)

    ctx = _ctx('https://example.com/a')
    hook.post_fetch(ctx, b'<html></html>')
    # Latency sample is recorded the moment post_parse fires.
    hook.post_parse(ctx, _DummyElem())

    assert rec.fetch_latency_ms.count == 1
    assert rec.fetch_latency_ms.min() is not None
    assert rec.fetch_latency_ms.min() >= 0.0


def test_recorder_parse_failure_increments():
    reset_url_stack()
    rec = Recorder()
    hook = BenchHook(rec)

    ctx = _ctx('https://example.com/bad')
    hook.post_fetch(ctx, b'')
    hook.post_parse(ctx, None)  # parse rejected this branch

    assert rec.parse_failures == 1
    assert 'https://example.com/bad' not in rec.urls_parsed


def test_unique_url_and_content_hll():
    reset_url_stack()
    rec = Recorder()
    hook = BenchHook(rec)

    payload = b'<html></html>'
    for i in range(10):
        ctx = _ctx(f'https://example.com/{i}')
        hook.post_fetch(ctx, payload)

    assert rec.unique_urls.added == 10
    assert rec.unique_urls.estimate() >= 7  # HLL has small relative error
    # All URLs returned identical bytes, so unique_content should be ~1.
    assert rec.unique_content.estimate() in (1, 2)


def test_stats_snapshot_from_crawler_stats_handles_dynamic_attrs():
    s = CrawlerStats()
    s.requests_completed = 42
    s.errors_by_host = defaultdict(int)
    s.errors_by_host['example.com'] = 3
    # Dynamic attrs added at runtime in build_trace_config; emulate that:
    s.status_counts = defaultdict(int)
    s.status_counts[200] = 40
    s.status_counts[404] = 2
    s.bytes_received = 1_000_000

    snap = StatsSnapshot.from_crawler_stats(s)
    assert snap.requests_completed == 42
    assert snap.errors_by_host == {'example.com': 3}
    assert snap.status_counts == {200: 40, 404: 2}
    assert snap.bytes_received == 1_000_000


def test_stats_snapshot_handles_missing_dynamic_attrs():
    s = CrawlerStats()  # never had status_counts or bytes_received set
    snap = StatsSnapshot.from_crawler_stats(s)
    assert snap.status_counts == {}
    assert snap.bytes_received == 0


def test_status_histogram_buckets_from_snapshot():
    s = CrawlerStats()
    s.status_counts = defaultdict(int, {200: 10, 301: 1, 404: 2, 503: 1, 200_000: 1})
    snap = StatsSnapshot.from_crawler_stats(s)
    hist = status_histogram_from_snapshot(snap)
    buckets = hist.buckets()
    assert buckets['2xx'] == 10
    assert buckets['3xx'] == 1
    assert buckets['4xx'] == 2
    assert buckets['5xx'] == 1
    assert buckets['other'] == 1
    assert hist.total == 15


# Minimal stand-in for lxml.html.HtmlElement — BenchHook only checks for None.
class _DummyElem:
    pass


@pytest.fixture(autouse=True)
def _clean_url_stack():
    reset_url_stack()
    yield
    reset_url_stack()
