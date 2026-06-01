"""Per-URL collector (hook) + post-run CrawlerStats snapshot reader.

Strict ownership: the BenchHook is the only source for latency quantiles,
unique-URL/content HLLs, depth distribution, and selector pairing. The
StatsSnapshot is the only source for throughput, bytes, status histogram,
errors-by-host, and retry counters. Report builder never sums across them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from benchmarks.metrics import Quantiles, StatusHistogram, UniqueSet


@dataclass
class Recorder:
    """Per-run counters owned by the runner. NOT module-global."""

    fetch_latency_ms: Quantiles = field(default_factory=Quantiles)
    unique_urls: UniqueSet | None = None
    unique_content: UniqueSet | None = None
    depths: Counter = field(default_factory=Counter)
    bytes_per_url: list[int] = field(default_factory=list)
    extractions_per_url: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    parse_failures: int = 0
    urls_with_at_least_one_extraction: set[str] = field(default_factory=set)
    urls_fetched: set[str] = field(default_factory=set)
    urls_parsed: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        # Lazy HLL init so unit tests that don't touch dedup avoid datasketch import.
        self.unique_urls = UniqueSet()
        self.unique_content = UniqueSet()


class BenchHook:
    """wxpath Hook implementation that funnels per-URL events into a Recorder.

    The Recorder is injected at construction time so multiple benchmark runs
    in the same process can use independent recorders (assuming the runner
    deregisters this hook between runs — see runner.py).
    """

    def __init__(self, recorder: Recorder):
        self._rec = recorder

    def post_fetch(self, ctx, html_bytes: bytes):
        # Timestamp the fetch on the context; post_parse will derive latency.
        ctx.user_data['_bench_t_fetch'] = time.monotonic()
        self._rec.urls_fetched.add(ctx.url)
        self._rec.depths[int(ctx.depth)] += 1
        n = len(html_bytes) if html_bytes is not None else 0
        self._rec.bytes_per_url.append(n)
        if self._rec.unique_urls is not None:
            self._rec.unique_urls.add(ctx.url)
        if html_bytes and self._rec.unique_content is not None:
            digest = hashlib.sha1(html_bytes, usedforsecurity=False).digest()
            self._rec.unique_content.add(digest)
        return html_bytes

    def post_parse(self, ctx, elem):
        t0 = ctx.user_data.get('_bench_t_fetch')
        if t0 is not None:
            dt_ms = (time.monotonic() - t0) * 1000.0
            self._rec.fetch_latency_ms.add(dt_ms)
        if elem is None:
            self._rec.parse_failures += 1
            return elem
        self._rec.urls_parsed.add(ctx.url)
        # Stamp the URL for post_extract pairing via a thread-local-ish slot.
        ctx.user_data['_bench_url'] = ctx.url
        _LAST_URL_STACK.append(ctx.url)
        return elem

    def post_extract(self, value):
        url = _LAST_URL_STACK[-1] if _LAST_URL_STACK else None
        if url is not None and value is not None:
            self._rec.extractions_per_url[url] += 1
            self._rec.urls_with_at_least_one_extraction.add(url)
        return value


# Best-effort stack used by post_extract since the engine pipes values through
# the post_extract hook without a context. The wxpath engine processes one URL
# at a time per worker; this stack mirrors that lifetime. Cleared by the runner
# before/after each run.
_LAST_URL_STACK: list[str] = []


def reset_url_stack() -> None:
    _LAST_URL_STACK.clear()


@dataclass(frozen=True)
class StatsSnapshot:
    """Frozen copy of the wxpath Crawler's CrawlerStats fields.

    Captured strictly AFTER the iterator returns. Reads `status_counts` and
    `bytes_received` defensively because those are added dynamically rather
    than declared on the dataclass.
    """

    requests_enqueued: int
    requests_started: int
    requests_completed: int
    requests_cache_hit: int
    in_flight_global: int
    in_flight_per_host: dict[str, int]
    queue_size: int
    queue_wait_time_total: float
    throttle_waits: int
    throttle_wait_time: float
    throttle_waits_by_host: dict[str, int]
    latency_samples: int
    latency_ewma: float
    min_latency: float | None
    max_latency: float | None
    retries_scheduled: int
    retries_executed: int
    errors_by_host: dict[str, int]
    status_counts: dict[int, int]
    bytes_received: int

    @classmethod
    def from_crawler_stats(cls, s: Any) -> 'StatsSnapshot':
        f = {field.name: getattr(s, field.name) for field in dataclasses.fields(s)}
        # Coerce defaultdicts to plain dicts for serialization.
        f['in_flight_per_host'] = dict(f['in_flight_per_host'])
        f['throttle_waits_by_host'] = dict(f['throttle_waits_by_host'])
        f['errors_by_host'] = dict(f['errors_by_host'])
        # Dynamically-added attrs:
        f['status_counts'] = dict(getattr(s, 'status_counts', {}) or {})
        f['bytes_received'] = int(getattr(s, 'bytes_received', 0) or 0)
        return cls(**f)


def status_histogram_from_snapshot(snap: StatsSnapshot) -> StatusHistogram:
    h = StatusHistogram()
    for code, n in snap.status_counts.items():
        h._counts[int(code)] += int(n)  # bulk add; Counter underneath
    return h
