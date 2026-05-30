"""Runner smoke: hook lifecycle, output files, defensive double-register.

Uses a fake driver to avoid the real network. Verifies the runner's
contract: register hook, drive, snapshot, render, write 5 files, deregister.
"""

import json
from pathlib import Path

import pytest

from benchmarks.collectors import BenchHook, StatsSnapshot
from benchmarks.driver import DriverResult, Event
from benchmarks.runner import HookAlreadyRegisteredError, run_workload
from benchmarks.workload import bundled_dir, load
from wxpath.hooks import registry
from wxpath.hooks.registry import FetchContext
from wxpath.http.stats import CrawlerStats


class FakeDriver:
    name = 'fake'
    version = '0.0.1'

    def __init__(self, emit_n: int = 5):
        self._emit_n = emit_n

    def run(self, workload, on_event):
        # Stand in for the hook firing: call the registered BenchHook directly
        # so the Recorder is exercised end-to-end.
        bench_hook = registry._global_hooks.get('BenchHook')
        assert bench_hook is not None, 'runner did not register BenchHook before driver.run'

        for i in range(self._emit_n):
            ctx = FetchContext(url=f'https://example.com/{i}', backlink=None,
                               depth=1, segments=[])
            bench_hook.post_fetch(ctx, b'<html><body><a href="x">x</a></body></html>')
            bench_hook.post_parse(ctx, _DummyElem())
            bench_hook.post_extract(f'val-{i}')
            on_event(Event(t_monotonic=float(i), kind='value', payload=f'val-{i}'))

        snap = StatsSnapshot.from_crawler_stats(CrawlerStats(
            requests_completed=self._emit_n,
        ))
        return DriverResult(
            snapshot=snap,
            n_events_value=self._emit_n,
            n_events_error=0,
            n_seeds_submitted=1,
            wall_clock_s=0.5,
        )


class _DummyElem:
    pass


def test_run_writes_five_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr('benchmarks.runner.WxPathDriver', FakeDriver)
    wl = load(bundled_dir() / 'single_host.yaml')
    # Skip settings application — FakeDriver doesn't use them, but the workload's
    # overrides will still be applied harmlessly. Force settings_overrides empty
    # to avoid any side-effects on the wxpath global SETTINGS for other tests.
    wl.settings_overrides = {}

    card, run_dir = run_workload(wl, out_root=tmp_path, quiet=True)

    assert run_dir.exists()
    for name in ('report.txt', 'report.json', 'raw.jsonl', 'meta.json',
                 'workload.snapshot.yaml'):
        assert (run_dir / name).exists(), f'missing {name}'

    assert card.pages_completed == 5
    # raw.jsonl has 5 events + 1 stats_snapshot tail line.
    lines = (run_dir / 'raw.jsonl').read_text().strip().splitlines()
    assert len(lines) == 6
    assert json.loads(lines[-1])['kind'] == 'stats_snapshot'

    meta = json.loads((run_dir / 'meta.json').read_text())
    # The pending list is populated by build_card; runner forwards it.
    assert isinstance(meta['pending_metrics'], list)
    assert 'robots compliance rate' in meta['pending_metrics']


def test_hook_is_deregistered_after_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr('benchmarks.runner.WxPathDriver', FakeDriver)
    wl = load(bundled_dir() / 'single_host.yaml')
    wl.settings_overrides = {}
    run_workload(wl, out_root=tmp_path, quiet=True)
    assert 'BenchHook' not in registry._global_hooks


def test_double_register_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.setattr('benchmarks.runner.WxPathDriver', FakeDriver)
    wl = load(bundled_dir() / 'single_host.yaml')
    wl.settings_overrides = {}

    # Pre-register a sentinel BenchHook to simulate stale state.
    from benchmarks.collectors import Recorder
    registry._global_hooks['BenchHook'] = BenchHook(Recorder())
    try:
        with pytest.raises(HookAlreadyRegisteredError):
            run_workload(wl, out_root=tmp_path, quiet=True)
    finally:
        registry._global_hooks.pop('BenchHook', None)


def test_strict_mode_fails_when_pending(tmp_path: Path, monkeypatch):
    monkeypatch.setattr('benchmarks.runner.WxPathDriver', FakeDriver)
    wl = load(bundled_dir() / 'single_host.yaml')
    wl.settings_overrides = {}
    with pytest.raises(RuntimeError):
        run_workload(wl, out_root=tmp_path, quiet=True, strict=True)


def test_non_runnable_workload_rejected(tmp_path: Path):
    wl = load(bundled_dir() / 'thousand_host.yaml')
    with pytest.raises(ValueError):
        run_workload(wl, out_root=tmp_path, quiet=True)
