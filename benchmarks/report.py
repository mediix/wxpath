"""ReportCard: build from recorder + stats snapshot; render text and JSON."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

from benchmarks.collectors import Recorder, StatsSnapshot, status_histogram_from_snapshot

PENDING = '<pending>'


def _fmt_int(n: int | None) -> str:
    return f'{n:,}' if isinstance(n, int) else PENDING


def _fmt_float(x: float | None, suffix: str = '', ndigits: int = 2) -> str:
    if x is None:
        return PENDING
    return f'{x:,.{ndigits}f}{suffix}'


def _fmt_pct(num: float | None, denom: float | None) -> str:
    if not denom or num is None:
        return PENDING
    return f'{(100.0 * num / denom):.1f}%'


@dataclass
class PendingRow:
    metric: str
    reason: str


@dataclass
class ReportCard:
    workload_name: str
    wall_clock_s: float
    duration_human: str
    seeds_submitted: int
    pages_completed: int

    # Throughput
    effective_pages_per_s: float | None
    bytes_per_s: float | None
    bytes_total: int

    # Latency (ms)
    fetch_p50_ms: float | None
    fetch_p95_ms: float | None
    fetch_p99_ms: float | None
    fetch_min_ms: float | None
    fetch_max_ms: float | None
    latency_ewma_ms: float | None
    tail_ratio: float | None

    # Reliability
    success_count: int
    status_buckets: dict[str, int]
    status_raw: dict[int, int]
    errors_by_host: dict[str, int]
    retries_scheduled: int
    retries_executed: int

    # Coverage
    unique_urls_hll: int
    unique_urls_observed: int
    unique_content_hll: int
    depth_histogram: dict[int, int]
    hosts_reached: int

    # Extraction
    selector_hit_rate: float | None
    extracted_total: int
    extracted_per_url_avg: float | None
    parse_failures: int

    # Politeness / content quality (pending)
    pending: list[PendingRow]

    # Driver
    driver_name: str
    driver_version: str
    aborted_reason: str | None
    notes: list[str] = field(default_factory=list)


def build_card(
    workload_name: str,
    driver_name: str,
    driver_version: str,
    rec: Recorder,
    snap: StatsSnapshot,
    wall_clock_s: float,
    seeds_submitted: int,
    aborted_reason: str | None,
    notes: list[str],
) -> ReportCard:
    pages_completed = int(snap.requests_completed)
    eff_pps = pages_completed / wall_clock_s if wall_clock_s > 0 else None
    bytes_total = int(snap.bytes_received)
    bps = bytes_total / wall_clock_s if wall_clock_s > 0 else None

    p50 = rec.fetch_latency_ms.percentile(50)
    p95 = rec.fetch_latency_ms.percentile(95)
    p99 = rec.fetch_latency_ms.percentile(99)
    ewma_ms = (snap.latency_ewma * 1000.0) if snap.latency_ewma else None
    tail = (p99 / p50) if (p50 and p99 and p50 > 0) else None

    status_hist = status_histogram_from_snapshot(snap)
    buckets = status_hist.buckets()
    success = buckets.get('2xx', 0)

    selector_hits = len(rec.urls_with_at_least_one_extraction)
    parsed = len(rec.urls_parsed)
    hit_rate = (selector_hits / parsed) if parsed > 0 else None
    extracted_total = sum(rec.extractions_per_url.values())
    extracted_avg = (extracted_total / parsed) if parsed > 0 else None

    pending = [
        PendingRow('robots compliance rate', 'needs robots-policy decision hook in wxpath'),
        PendingRow('crawl-delay adherence',  'needs per-host inter-request gap stream'),
        PendingRow('per-phase latency (dns/tls/ttfb)', 'needs trace hooks beyond on_request_end'),
        PendingRow('soft-404 detection',     'needs content classifier'),
        PendingRow('boilerplate-strip ratio', 'needs main-content extractor'),
        PendingRow('429/host rate', 'needs per-host status timeseries (stats has totals only)'),
    ]

    return ReportCard(
        workload_name=workload_name,
        wall_clock_s=wall_clock_s,
        duration_human=_human_duration(wall_clock_s),
        seeds_submitted=seeds_submitted,
        pages_completed=pages_completed,
        effective_pages_per_s=eff_pps,
        bytes_per_s=bps,
        bytes_total=bytes_total,
        fetch_p50_ms=p50,
        fetch_p95_ms=p95,
        fetch_p99_ms=p99,
        fetch_min_ms=rec.fetch_latency_ms.min(),
        fetch_max_ms=rec.fetch_latency_ms.max(),
        latency_ewma_ms=ewma_ms,
        tail_ratio=tail,
        success_count=success,
        status_buckets=buckets,
        status_raw=dict(snap.status_counts),
        errors_by_host=dict(snap.errors_by_host),
        retries_scheduled=int(snap.retries_scheduled),
        retries_executed=int(snap.retries_executed),
        unique_urls_hll=rec.unique_urls.estimate() if rec.unique_urls else 0,
        unique_urls_observed=rec.unique_urls.added if rec.unique_urls else 0,
        unique_content_hll=rec.unique_content.estimate() if rec.unique_content else 0,
        depth_histogram=dict(rec.depths),
        hosts_reached=len({h for h in snap.in_flight_per_host} | set(snap.errors_by_host)),
        selector_hit_rate=hit_rate,
        extracted_total=extracted_total,
        extracted_per_url_avg=extracted_avg,
        parse_failures=int(rec.parse_failures),
        pending=pending,
        driver_name=driver_name,
        driver_version=driver_version,
        aborted_reason=aborted_reason,
        notes=notes,
    )


def _human_duration(s: float) -> str:
    if s < 60:
        return f'{s:0.1f}s'
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f'{h:02d}:{m:02d}:{sec:02d}'


def render_text(card: ReportCard) -> str:
    width = 64
    bar = '─' * width
    lines: list[str] = []
    lines.append(f'┌─ wxpath benchmark report {bar[27:]}┐')
    lines.append(f'│ workload   {card.workload_name}')
    lines.append(f'│ driver     {card.driver_name} v{card.driver_version}')
    lines.append(f'│ wall       {card.duration_human}  '
                 f'seeds {card.seeds_submitted}  pages {card.pages_completed}')
    if card.aborted_reason:
        lines.append(f'│ aborted    {card.aborted_reason}')
    lines.append('├─ Throughput ' + '─' * (width - 14) + '┤')
    lines.append(f'│  effective    {_fmt_float(card.effective_pages_per_s, " pages/s")}')
    lines.append(f'│  bytes        {_fmt_float((card.bytes_per_s or 0)/1024.0, " KiB/s")}  '
                 f'(total {_fmt_int(card.bytes_total)} B)')
    lines.append('├─ Latency (ms) ' + '─' * (width - 16) + '┤')
    lines.append(f'│  p50  {_fmt_float(card.fetch_p50_ms)}   '
                 f'p95  {_fmt_float(card.fetch_p95_ms)}   '
                 f'p99  {_fmt_float(card.fetch_p99_ms)}')
    lines.append(f'│  min  {_fmt_float(card.fetch_min_ms)}   '
                 f'max  {_fmt_float(card.fetch_max_ms)}   '
                 f'ewma  {_fmt_float(card.latency_ewma_ms)}')
    lines.append(f'│  tail-ratio (p99/p50)  {_fmt_float(card.tail_ratio, "x")}')
    lines.append('├─ Reliability ' + '─' * (width - 14) + '┤')
    total = sum(card.status_buckets.values())
    lines.append(f'│  success     {card.status_buckets.get("2xx",0)} / {total}  '
                 f'({_fmt_pct(card.status_buckets.get("2xx",0), total)})')
    lines.append(f'│  status      ' +
                 '  '.join(f'{k}:{v}' for k, v in card.status_buckets.items() if v > 0))
    lines.append(f'│  retries     scheduled {card.retries_scheduled}  '
                 f'executed {card.retries_executed}')
    if card.errors_by_host:
        top = sorted(card.errors_by_host.items(), key=lambda kv: -kv[1])[:3]
        lines.append('│  err-hosts   ' + ', '.join(f'{h}:{n}' for h, n in top))
    lines.append('├─ Coverage ' + '─' * (width - 11) + '┤')
    lines.append(f'│  unique URLs (HLL)        '
                 f'{_fmt_int(card.unique_urls_hll)}  '
                 f'(observed {_fmt_int(card.unique_urls_observed)})')
    lines.append(f'│  unique content (HLL)     '
                 f'{_fmt_int(card.unique_content_hll)}')
    lines.append(f'│  hosts reached            {_fmt_int(card.hosts_reached)}')
    if card.depth_histogram:
        dh = ', '.join(f'd{k}:{v}' for k, v in sorted(card.depth_histogram.items()))
        lines.append(f'│  depth distribution       {dh}')
    lines.append('├─ Extraction ' + '─' * (width - 13) + '┤')
    lines.append(f'│  selector hit rate        '
                 f'{_fmt_pct((card.selector_hit_rate or 0)*100 if card.selector_hit_rate else None, 100)}')
    lines.append(f'│  extracted (total)        {_fmt_int(card.extracted_total)}  '
                 f'avg/url {_fmt_float(card.extracted_per_url_avg)}')
    lines.append(f'│  parse failures           {_fmt_int(card.parse_failures)}')
    lines.append('├─ Pending instrumentation ' + '─' * (width - 26) + '┤')
    for row in card.pending:
        lines.append(f'│  {row.metric:34s} {PENDING:11s}  {row.reason}')
    lines.append('└' + bar + '┘')
    if card.notes:
        lines.append('')
        lines.append('Notes:')
        for n in card.notes:
            lines.append(f'  - {n}')
    return '\n'.join(lines) + '\n'


def render_json(card: ReportCard) -> str:
    def _convert(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj):
            return {k: _convert(v) for k, v in dataclasses.asdict(obj).items()}
        if isinstance(obj, dict):
            return {str(k): _convert(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_convert(v) for v in obj]
        return obj
    return json.dumps(_convert(card), indent=2, default=str)


def pending_metric_names(card: ReportCard) -> list[str]:
    return [row.metric for row in card.pending]
