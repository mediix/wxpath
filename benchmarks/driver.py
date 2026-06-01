"""Driver protocol + WxPathDriver: adapt a Workload to wxpath's engine."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from wxpath.core.runtime.engine import WXPathEngine, wxpath_async_blocking_iter
from wxpath.http.client.crawler import Crawler
from wxpath.settings import SETTINGS
from wxpath.util.logging import get_logger

from benchmarks.collectors import StatsSnapshot
from benchmarks.workload import Workload

log = get_logger(__name__)


@dataclass
class Event:
    t_monotonic: float
    kind: str               # 'value' | 'error' | 'lifecycle'
    url: str | None = None
    depth: int | None = None
    payload: Any = None


@dataclass
class DriverResult:
    snapshot: StatsSnapshot
    n_events_value: int = 0
    n_events_error: int = 0
    n_seeds_submitted: int = 0
    wall_clock_s: float = 0.0
    limit_overrun_s: float = 0.0
    aborted_reason: str | None = None
    notes: list[str] = field(default_factory=list)


class Driver(Protocol):
    name: str
    version: str

    def run(self, workload: Workload, on_event: Callable[[Event], None]) -> DriverResult: ...


def _apply_setting(dotted_key: str, value: Any) -> None:
    """Walk dotted path into SETTINGS AttrDict and assign value."""
    parts = dotted_key.split('.')
    node = SETTINGS
    for p in parts[:-1]:
        if not hasattr(node, p):
            raise KeyError(f'unknown setting path: {dotted_key!r} (missing {p!r})')
        node = getattr(node, p)
    leaf = parts[-1]
    if not hasattr(node, leaf):
        raise KeyError(f'unknown setting leaf: {dotted_key!r}')
    node[leaf] = value


def _seed_expression(seed: str, inner_expr: str, depth: int) -> str:
    """Compose a wxpath expression that starts at `seed` with the given inner xpath.

    For v0 we use a literal url(...) prefix matching the README pattern.
    """
    safe_inner = inner_expr.lstrip()
    if safe_inner.startswith('//'):
        return f"url('{seed}'){safe_inner}"
    return f"url('{seed}'){safe_inner}"


class WxPathDriver:
    """Drives wxpath through `wxpath_async_blocking_iter` and captures stats."""

    name = 'wxpath'
    version = '0.1.0'

    def run(self, workload: Workload, on_event: Callable[[Event], None]) -> DriverResult:
        if workload.path_expr is None:
            raise ValueError('workload.path_expr is required')

        # Apply settings overrides
        for key, val in (workload.settings_overrides or {}).items():
            _apply_setting(key, val)
            log.info('settings override applied', extra={'key': key, 'value': val})

        # Crawler()'s respect_robots arg defaults to True and shadows the
        # setting, so pass the (possibly overridden) value explicitly to honor
        # the workload's settings_overrides.
        crawler = Crawler(respect_robots=bool(SETTINGS.http.client.crawler.respect_robots))
        engine = WXPathEngine(crawler=crawler)

        # Fixture workloads stand up a local server whose base URL is only known
        # after it binds an ephemeral port; resolve seeds against it.
        fixture, base_url = self._start_seeds_server(workload)
        if base_url is not None:
            seeds = workload.seeds.resolve_fixture(base_url)
        else:
            seeds = workload.seeds.resolve(workload.workload_dir)
        if not seeds:
            if fixture is not None:
                fixture.stop()
            raise ValueError(f'workload {workload.name!r} resolved to zero seeds')

        deadline = time.monotonic() + max(1, workload.limits.wall_clock_seconds)
        max_urls = max(1, workload.limits.max_urls)

        t0 = time.monotonic()
        result = DriverResult(snapshot=None, n_seeds_submitted=len(seeds))  # type: ignore[arg-type]
        if base_url is not None:
            result.notes.append(f'fixture base_url={base_url}')
        n_value = 0
        n_error = 0
        urls_seen_value = 0

        try:
            for seed in seeds:
                if self._is_over_budget(deadline, urls_seen_value, max_urls):
                    result.aborted_reason = 'limit'
                    break
                expr = _seed_expression(seed, workload.path_expr, workload.max_depth)
                log.info('seed start', extra={'seed': seed, 'expr': expr})
                try:
                    for item in wxpath_async_blocking_iter(
                        expr,
                        max_depth=workload.max_depth,
                        engine=engine,
                        yield_errors=True,
                    ):
                        now = time.monotonic()
                        is_error = isinstance(item, dict) and item.get('__type__') == 'error'
                        if is_error:
                            n_error += 1
                            on_event(Event(
                                t_monotonic=now,
                                kind='error',
                                url=item.get('url'),
                                depth=item.get('depth'),
                                payload=item,
                            ))
                        else:
                            n_value += 1
                            urls_seen_value += 1
                            on_event(Event(
                                t_monotonic=now,
                                kind='value',
                                url=None,
                                depth=None,
                                payload=_safe_payload(item),
                            ))
                        if self._is_over_budget(deadline, urls_seen_value, max_urls):
                            result.aborted_reason = 'limit'
                            break
                except Exception as exc:  # noqa: BLE001
                    log.warning('seed crawl raised', extra={'seed': seed, 'error': str(exc)})
                    result.notes.append(f'seed {seed!r} raised: {exc}')
                if result.aborted_reason:
                    break
        finally:
            t_end = time.monotonic()
            result.wall_clock_s = t_end - t0
            if t_end > deadline:
                result.limit_overrun_s = t_end - deadline
            result.n_events_value = n_value
            result.n_events_error = n_error
            result.snapshot = StatsSnapshot.from_crawler_stats(crawler._stats)
            if fixture is not None:
                fixture.stop()

        return result

    @staticmethod
    def _start_seeds_server(workload: Workload):
        """Start the in-process fixture server for fixture workloads.

        Returns ``(server, base_url)`` for fixture seeds, else ``(None, None)``.
        """
        if workload.seeds.type != 'fixture':
            return None, None
        from benchmarks.server import FixtureServer, build_corpus
        corpus = build_corpus(
            page_count=workload.seeds.page_count,
            fan_out=workload.seeds.fan_out,
            filler_bytes=workload.seeds.filler_bytes,
        )
        server = FixtureServer(corpus.pages)
        base_url = server.start()
        log.info('fixture server started',
                 extra={'base_url': base_url, 'pages': len(corpus.pages)})
        return server, base_url

    @staticmethod
    def _is_over_budget(deadline: float, urls_seen: int, max_urls: int) -> bool:
        return time.monotonic() >= deadline or urls_seen >= max_urls


def _safe_payload(item: Any) -> Any:
    """Stringify lxml elements / unusual objects so the JSONL writer doesn't choke."""
    try:
        from lxml.html import HtmlElement
        if isinstance(item, HtmlElement):
            tag = item.tag
            text = (item.text or '').strip()[:80]
            return {'__type__': 'HtmlElement', 'tag': tag, 'text': text}
    except Exception:  # noqa: BLE001
        pass
    if isinstance(item, (str, int, float, bool, type(None))):
        return item
    if isinstance(item, (list, tuple, dict)):
        return item
    return repr(item)[:200]
