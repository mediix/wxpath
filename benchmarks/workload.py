"""Workload YAML schema, loader, and canonical hash."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

KINDS = {'single_host', 'thousand_host', 'cc_sample', 'anti_bot', 'freshness'}
V0_RUNNABLE_KINDS = {'single_host'}


def _seeds_dict(s: 'Seeds') -> dict[str, Any]:
    """Canonical seeds payload for hashing and snapshot serialization.

    Fixture corpus params are part of the hash (the corpus shape is part of
    reproducibility) but the runtime ephemeral port is never stored.
    """
    return {
        'type': s.type,
        'urls': s.urls,
        'path': s.path,
        'page_count': s.page_count,
        'fan_out': s.fan_out,
        'filler_bytes': s.filler_bytes,
    }


@dataclass
class Seeds:
    type: str                                # 'inline' | 'file' | 'manifest' | 'fixture'
    urls: list[str] = field(default_factory=list)
    path: str | None = None
    # Synthetic-corpus parameters (type == 'fixture'); see benchmarks/server.py.
    page_count: int = 0
    fan_out: int = 0
    filler_bytes: int = 0

    def resolve(self, workload_dir: Path) -> list[str]:
        if self.type == 'inline':
            return list(self.urls)
        if self.type == 'file':
            if not self.path:
                raise ValueError('seeds.type=file requires seeds.path')
            p = (workload_dir / self.path).resolve()
            return [
                line.strip()
                for line in p.read_text(encoding='utf-8').splitlines()
                if line.strip() and not line.lstrip().startswith('#')
            ]
        if self.type == 'manifest':
            raise NotImplementedError('manifest seeds not supported in v0')
        if self.type == 'fixture':
            raise RuntimeError(
                'fixture seeds resolve only after the in-process server binds a '
                'port; the driver calls resolve_fixture(base_url) instead'
            )
        raise ValueError(f'unknown seeds.type: {self.type!r}')

    def resolve_fixture(self, base_url: str) -> list[str]:
        """Resolve fixture seeds to the (runtime) loopback server base URL."""
        if self.type != 'fixture':
            raise ValueError('resolve_fixture is only valid for seeds.type=fixture')
        return [base_url]


@dataclass
class Limits:
    max_urls: int = 1000
    wall_clock_seconds: int = 600


@dataclass
class Workload:
    name: str
    kind: str
    runnable: bool
    description: str
    seeds: Seeds
    path_expr: str
    max_depth: int
    limits: Limits
    settings_overrides: dict[str, Any]
    tags: list[str]
    notes: str | None = None
    source_path: Path | None = None

    @property
    def workload_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()

    def is_runnable_in_v0(self) -> bool:
        return self.runnable and self.kind in V0_RUNNABLE_KINDS

    def canonical_hash(self) -> str:
        payload = {
            'name': self.name,
            'kind': self.kind,
            'runnable': self.runnable,
            'description': self.description,
            'seeds': _seeds_dict(self.seeds),
            'path_expr': self.path_expr,
            'max_depth': self.max_depth,
            'limits': {
                'max_urls': self.limits.max_urls,
                'wall_clock_seconds': self.limits.wall_clock_seconds,
            },
            'settings_overrides': self.settings_overrides,
            'tags': self.tags,
        }
        blob = yaml.safe_dump(payload, sort_keys=True, default_flow_style=False).encode('utf-8')
        return hashlib.sha256(blob).hexdigest()


def load(path: str | Path) -> Workload:
    p = Path(path).resolve()
    with p.open('r', encoding='utf-8') as f:
        raw = yaml.safe_load(f) or {}

    kind = raw.get('kind')
    if kind not in KINDS:
        raise ValueError(f'workload {p}: kind must be one of {sorted(KINDS)}, got {kind!r}')

    seeds_raw = raw.get('seeds') or {}
    seeds = Seeds(
        type=seeds_raw.get('type', 'inline'),
        urls=list(seeds_raw.get('urls') or []),
        path=seeds_raw.get('path'),
        page_count=int(seeds_raw.get('page_count', 0)),
        fan_out=int(seeds_raw.get('fan_out', 0)),
        filler_bytes=int(seeds_raw.get('filler_bytes', 0)),
    )

    limits_raw = raw.get('limits') or {}
    limits = Limits(
        max_urls=int(limits_raw.get('max_urls', 1000)),
        wall_clock_seconds=int(limits_raw.get('wall_clock_seconds', 600)),
    )

    return Workload(
        name=raw['name'],
        kind=kind,
        runnable=bool(raw.get('runnable', False)),
        description=raw.get('description', ''),
        seeds=seeds,
        path_expr=raw.get('path_expr', ''),
        max_depth=int(raw.get('max_depth', 1)),
        limits=limits,
        settings_overrides=dict(raw.get('settings_overrides') or {}),
        tags=list(raw.get('tags') or []),
        notes=raw.get('notes'),
        source_path=p,
    )


def to_dict(wl: Workload) -> dict[str, Any]:
    """Re-serialize to dict (for workload.snapshot.yaml)."""
    return {
        'name': wl.name,
        'kind': wl.kind,
        'runnable': wl.runnable,
        'description': wl.description,
        'seeds': _seeds_dict(wl.seeds),
        'path_expr': wl.path_expr,
        'max_depth': wl.max_depth,
        'limits': {
            'max_urls': wl.limits.max_urls,
            'wall_clock_seconds': wl.limits.wall_clock_seconds,
        },
        'settings_overrides': wl.settings_overrides,
        'tags': wl.tags,
        'notes': wl.notes,
    }


def resolve_alias(name_or_path: str, bundled_dir: Path) -> Path:
    """Accept a bundled workload name OR a path to a YAML file."""
    p = Path(name_or_path)
    if p.suffix in ('.yaml', '.yml') and p.exists():
        return p.resolve()
    candidate = bundled_dir / f'{name_or_path}.yaml'
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError(
        f'workload {name_or_path!r} not found as bundled name or path'
    )


def bundled_dir() -> Path:
    return Path(__file__).resolve().parent / 'workloads'


def list_bundled() -> list[Workload]:
    out = []
    for path in sorted(bundled_dir().glob('*.yaml')):
        try:
            out.append(load(path))
        except Exception as e:  # noqa: BLE001
            # Surface as a stub-failed entry without crashing `list`
            out.append(
                Workload(
                    name=path.stem,
                    kind='single_host',
                    runnable=False,
                    description=f'<failed to parse: {e}>',
                    seeds=Seeds(type='inline'),
                    path_expr='',
                    max_depth=0,
                    limits=Limits(),
                    settings_overrides={},
                    tags=[],
                    source_path=path,
                )
            )
    return out
