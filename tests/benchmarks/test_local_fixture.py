"""End-to-end smoke test for the reproducible engine-baseline workload.

Unlike test_runner_smoke (which fakes the driver), this drives the REAL
WxPathDriver and engine against the REAL in-process fixture server over
loopback — just with a tiny corpus. It asserts the deterministic coverage and
extraction counts predicted by benchmarks/server.build_corpus, and runs twice
to guard reproducibility. Fully offline: no live network.
"""

from pathlib import Path

import pytest

from benchmarks.server import build_corpus
from benchmarks.runner import run_workload
from benchmarks.workload import bundled_dir, load

# Tiny corpus: BFS binary tree of 7 nodes -> depths {0:1, 1:2, 2:4}.
TINY = dict(page_count=7, fan_out=2, filler_bytes=64)


@pytest.fixture
def isolate_settings():
    """Snapshot/restore global SETTINGS the workload mutates, so the fixture
    run's overrides (high concurrency, robots off, zero throttle) don't leak
    into other tests in the same process."""
    from wxpath.settings import SETTINGS
    crawler = dict(SETTINGS.http.client.crawler)
    cache = dict(SETTINGS.http.client.cache)
    try:
        yield
    finally:
        for k, v in crawler.items():
            SETTINGS.http.client.crawler[k] = v
        for k, v in cache.items():
            SETTINGS.http.client.cache[k] = v


def _tiny_workload():
    wl = load(bundled_dir() / 'local_fixture.yaml')
    wl.seeds.page_count = TINY['page_count']
    wl.seeds.fan_out = TINY['fan_out']
    wl.seeds.filler_bytes = TINY['filler_bytes']
    wl.max_depth = 8                 # >= tree height (2); whole tree fetched
    wl.limits.max_urls = 100         # well above expected yielded values
    return wl


def test_local_fixture_workload_is_runnable():
    wl = load(bundled_dir() / 'local_fixture.yaml')
    assert wl.is_runnable_in_v0() is True
    assert wl.seeds.type == 'fixture'


def test_engine_baseline_counts_are_deterministic(tmp_path: Path, isolate_settings):
    corpus = build_corpus(**TINY)

    card, run_dir = run_workload(
        _tiny_workload(), out_root=tmp_path / 'run1', quiet=True
    )

    # Every node fetched exactly once; one value per node except the seed/root.
    assert card.pages_completed == corpus.expected_pages_completed       # 7
    assert card.extracted_total == corpus.expected_extractions           # 6
    assert card.unique_urls_observed == corpus.expected_unique_urls      # 7
    assert card.depth_histogram == corpus.expected_depth_histogram       # {0:1,1:2,2:4}
    assert card.selector_hit_rate == pytest.approx(corpus.expected_selector_hit_rate)

    # All loopback fetches succeed; nothing throttled out or aborted.
    assert card.status_buckets.get('2xx') == corpus.expected_pages_completed
    assert not card.errors_by_host
    assert card.parse_failures == 0
    assert card.aborted_reason is None

    # The workload adds no NEW pending-instrumentation rows beyond the 6 known.
    assert len(card.pending) == 6

    # Standard 5-file output bundle is written.
    for name in ('report.txt', 'report.json', 'raw.jsonl', 'meta.json',
                 'workload.snapshot.yaml'):
        assert (run_dir / name).exists(), f'missing {name}'


def test_engine_baseline_is_reproducible(tmp_path: Path, isolate_settings):
    """Two independent runs produce identical coverage/extraction numbers."""
    wl = _tiny_workload()
    card1, _ = run_workload(wl, out_root=tmp_path / 'a', quiet=True)
    card2, _ = run_workload(wl, out_root=tmp_path / 'b', quiet=True)

    assert card1.pages_completed == card2.pages_completed
    assert card1.extracted_total == card2.extracted_total
    assert card1.unique_urls_observed == card2.unique_urls_observed
    assert card1.depth_histogram == card2.depth_histogram
    assert card1.status_buckets == card2.status_buckets
