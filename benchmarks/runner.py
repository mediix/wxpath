"""Workload orchestrator: hook lifecycle, driver invocation, output writing."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from wxpath.hooks import registry
from wxpath.util.logging import configure_logging, get_logger

from benchmarks.collectors import BenchHook, Recorder, reset_url_stack
from benchmarks.driver import Driver, DriverResult, Event, WxPathDriver
from benchmarks.meta import RunMeta
from benchmarks.report import (
    ReportCard,
    build_card,
    pending_metric_names,
    render_json,
    render_text,
)
from benchmarks.workload import Workload, to_dict

log = get_logger(__name__)


class HookAlreadyRegisteredError(RuntimeError):
    """Defensive: a BenchHook was registered before run_workload was called."""


def _bench_hook_name() -> str:
    # BenchHook is registered by class name in wxpath.hooks.registry._global_hooks.
    return BenchHook.__name__


def _register_bench_hook(hook: BenchHook) -> None:
    name = _bench_hook_name()
    if name in registry._global_hooks:
        raise HookAlreadyRegisteredError(
            f'a {name} is already registered; run_workload refuses to start'
        )
    registry._global_hooks[name] = hook


def _deregister_bench_hook() -> None:
    # Deliberate poke at a private attr — wxpath has no public deregister.
    # Tracked as a v0 limitation; subprocess-per-run is the v1 path.
    registry._global_hooks.pop(_bench_hook_name(), None)


def _make_run_dir(out_root: Path, workload_name: str) -> Path:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')
    run_dir = out_root / f'{ts}__{workload_name}'
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def run_workload(
    workload: Workload,
    out_root: Path | str | None = None,
    cli_argv: list[str] | None = None,
    user_tags: dict[str, str] | None = None,
    quiet: bool = False,
    strict: bool = False,
) -> tuple[ReportCard, Path]:
    """Run a workload end-to-end and write the standard 5-file output set.

    Returns (ReportCard, run_dir_path).
    """
    if not workload.is_runnable_in_v0():
        raise ValueError(
            f'workload {workload.name!r} is not runnable in v0 '
            f'(kind={workload.kind}, runnable={workload.runnable})'
        )

    configure_logging(level='WARN' if quiet else 'INFO')

    out_root = Path(out_root) if out_root else (
        Path(__file__).resolve().parent / 'results'
    )
    out_root.mkdir(parents=True, exist_ok=True)
    run_dir = _make_run_dir(out_root, workload.name)

    meta = RunMeta(
        workload_name=workload.name,
        workload_hash=workload.canonical_hash(),
        workload_path=str(workload.source_path or ''),
        cli_argv=list(cli_argv or []),
        user_tags=dict(user_tags or {}),
    )

    recorder = Recorder()
    bench_hook = BenchHook(recorder)
    reset_url_stack()

    raw_path = run_dir / 'raw.jsonl'
    events_written = 0

    def _on_event(ev: Event) -> None:
        nonlocal events_written
        record = {
            't': ev.t_monotonic,
            'kind': ev.kind,
            'url': ev.url,
            'depth': ev.depth,
            'payload': ev.payload,
            'schema': 1,
        }
        raw_fh.write(json.dumps(record, default=str) + '\n')
        events_written += 1

    driver: Driver = WxPathDriver()
    result: DriverResult

    meta.start()
    _register_bench_hook(bench_hook)
    try:
        with raw_path.open('w', encoding='utf-8') as raw_fh:
            result = driver.run(workload, _on_event)
    finally:
        _deregister_bench_hook()
        reset_url_stack()
        meta.stop()

    meta.set_limit_overrun(result.limit_overrun_s)

    card = build_card(
        workload_name=workload.name,
        driver_name=driver.name,
        driver_version=driver.version,
        rec=recorder,
        snap=result.snapshot,
        wall_clock_s=result.wall_clock_s,
        seeds_submitted=result.n_seeds_submitted,
        aborted_reason=result.aborted_reason,
        notes=result.notes,
    )
    for name in pending_metric_names(card):
        meta.note_pending(name)

    # Write outputs
    (run_dir / 'report.txt').write_text(render_text(card), encoding='utf-8')
    (run_dir / 'report.json').write_text(render_json(card), encoding='utf-8')
    (run_dir / 'meta.json').write_text(
        json.dumps(meta.as_dict(), indent=2, default=str), encoding='utf-8'
    )
    (run_dir / 'workload.snapshot.yaml').write_text(
        yaml.safe_dump(to_dict(workload), sort_keys=False), encoding='utf-8'
    )

    # Append a final stats_snapshot line for raw.jsonl auditability.
    with raw_path.open('a', encoding='utf-8') as raw_fh:
        snap_dict: dict[str, Any] = {
            'schema': 1, 'kind': 'stats_snapshot',
            'snapshot': _snapshot_to_jsonable(result.snapshot),
        }
        raw_fh.write(json.dumps(snap_dict, default=str) + '\n')

    if strict and meta.as_dict().get('pending_metrics'):
        raise RuntimeError(
            f'strict mode: pending metrics present: {meta.as_dict()["pending_metrics"]!r}'
        )

    return card, run_dir


def _snapshot_to_jsonable(snap: Any) -> dict[str, Any]:
    import dataclasses
    d = dataclasses.asdict(snap)
    # status_counts keys are ints — make sure JSON dumps cleanly
    d['status_counts'] = {str(k): v for k, v in d.get('status_counts', {}).items()}
    return d
