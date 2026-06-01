"""argparse CLI for the benchmark harness."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from benchmarks import __version__
from benchmarks.runner import run_workload
from benchmarks.workload import (
    bundled_dir,
    list_bundled,
    load,
    resolve_alias,
    to_dict,
)


def _cmd_list(_args: argparse.Namespace) -> int:
    rows = list_bundled()
    if not rows:
        print('no bundled workloads found')
        return 1
    name_w = max(len(r.name) for r in rows)
    print(f'{"name".ljust(name_w)}  kind            runnable  description')
    print(f'{"-" * name_w}  --------------- --------  -----------')
    for r in rows:
        runnable = 'yes' if r.is_runnable_in_v0() else 'no'
        print(f'{r.name.ljust(name_w)}  {r.kind.ljust(15)} {runnable.ljust(8)}  {r.description}')
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    path = resolve_alias(args.workload, bundled_dir())
    wl = load(path)
    payload = to_dict(wl)
    payload['_canonical_hash'] = wl.canonical_hash()
    payload['_source_path'] = str(path)
    print(yaml.safe_dump(payload, sort_keys=False))
    return 0


def _parse_tag(spec: str) -> tuple[str, str]:
    if '=' not in spec:
        raise argparse.ArgumentTypeError(f'--tag expects KEY=VAL, got {spec!r}')
    k, _, v = spec.partition('=')
    return k.strip(), v.strip()


def _cmd_run(args: argparse.Namespace) -> int:
    path = resolve_alias(args.workload, bundled_dir())
    wl = load(path)

    if args.max_urls is not None:
        wl.limits.max_urls = args.max_urls
    if args.timeout is not None:
        wl.limits.wall_clock_seconds = args.timeout

    user_tags = dict(args.tag or [])

    try:
        card, run_dir = run_workload(
            workload=wl,
            out_root=Path(args.out) if args.out else None,
            cli_argv=sys.argv,
            user_tags=user_tags,
            quiet=args.quiet,
            strict=args.strict,
        )
    except Exception as exc:  # noqa: BLE001
        print(f'run failed: {exc}', file=sys.stderr)
        return 2

    if not args.quiet:
        from benchmarks.report import render_text
        print(render_text(card))
    print(f'wrote: {run_dir}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='wxpath-bench',
        description='wxpath benchmark harness — measure crawl performance against standardized workloads.',
    )
    p.add_argument('--version', action='version', version=f'wxpath-bench {__version__}')
    sub = p.add_subparsers(dest='cmd', required=True)

    pl = sub.add_parser('list', help='list bundled workloads')
    pl.set_defaults(func=_cmd_list)

    ps = sub.add_parser('show', help='show resolved workload + canonical hash')
    ps.add_argument('workload', help='bundled name (e.g. single_host) or path to YAML')
    ps.set_defaults(func=_cmd_show)

    pr = sub.add_parser('run', help='execute a workload')
    pr.add_argument('workload', help='bundled name or path to YAML')
    pr.add_argument('--out', default=None, help='output root (default: benchmarks/results)')
    pr.add_argument('--max-urls', type=int, default=None, help='override limits.max_urls')
    pr.add_argument('--timeout', type=int, default=None,
                    help='override limits.wall_clock_seconds')
    pr.add_argument('--strict', action='store_true',
                    help='fail run if any pending metric is present')
    pr.add_argument('--quiet', action='store_true', help='suppress info logging')
    pr.add_argument('--tag', action='append', type=_parse_tag,
                    help='annotate meta.json with KEY=VAL (repeatable)')
    pr.set_defaults(func=_cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)
