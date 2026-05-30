"""Run metadata capture: git, host, versions, rusage."""

from __future__ import annotations

import platform
import resource
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ['git', *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ''


def git_sha() -> str:
    sha = _git('rev-parse', 'HEAD')
    return sha or 'unknown'


def git_dirty() -> bool:
    out = _git('status', '--porcelain')
    return bool(out)


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return 'unknown'


def _rusage_delta(before: resource.struct_rusage, after: resource.struct_rusage) -> dict[str, float]:
    return {
        'utime': max(0.0, after.ru_utime - before.ru_utime),
        'stime': max(0.0, after.ru_stime - before.ru_stime),
        'maxrss_kb': float(after.ru_maxrss),  # already max over process lifetime
    }


class RunMeta:
    """Capture and finalize run metadata."""

    def __init__(self, workload_name: str, workload_hash: str, workload_path: str,
                 cli_argv: list[str], user_tags: dict[str, str]):
        self._workload_name = workload_name
        self._workload_hash = workload_hash
        self._workload_path = workload_path
        self._cli_argv = cli_argv
        self._user_tags = user_tags
        self._t_start_wall: float = 0.0
        self._t_end_wall: float = 0.0
        self._rusage_before: resource.struct_rusage | None = None
        self._rusage_after: resource.struct_rusage | None = None
        self._started_iso: str = ''
        self._ended_iso: str = ''
        self._pending_metrics: list[str] = []
        self._limit_overrun_s: float = 0.0

    def start(self) -> None:
        self._t_start_wall = time.monotonic()
        self._started_iso = datetime.now(timezone.utc).isoformat()
        self._rusage_before = resource.getrusage(resource.RUSAGE_SELF)

    def stop(self) -> None:
        self._t_end_wall = time.monotonic()
        self._ended_iso = datetime.now(timezone.utc).isoformat()
        self._rusage_after = resource.getrusage(resource.RUSAGE_SELF)

    @property
    def duration_s(self) -> float:
        return max(0.0, self._t_end_wall - self._t_start_wall)

    def note_pending(self, metric: str) -> None:
        if metric not in self._pending_metrics:
            self._pending_metrics.append(metric)

    def set_limit_overrun(self, overrun_s: float) -> None:
        self._limit_overrun_s = max(0.0, overrun_s)

    def as_dict(self) -> dict[str, Any]:
        if self._rusage_before is None or self._rusage_after is None:
            ru = {'utime': 0.0, 'stime': 0.0, 'maxrss_kb': 0.0}
        else:
            ru = _rusage_delta(self._rusage_before, self._rusage_after)
        return {
            'wxpath_version': _pkg_version('wxpath'),
            'benchmarks_version': _pkg_version('wxpath') if False else _read_self_version(),
            'git_sha': git_sha(),
            'git_dirty': git_dirty(),
            'hostname': socket.gethostname(),
            'python_version': platform.python_version(),
            'platform': platform.platform(),
            'started_at': self._started_iso,
            'ended_at': self._ended_iso,
            'duration_s': self.duration_s,
            'workload_name': self._workload_name,
            'workload_hash': self._workload_hash,
            'workload_path': self._workload_path,
            'cli_argv': self._cli_argv,
            'user_tags': self._user_tags,
            'rusage': ru,
            'pending_metrics': list(self._pending_metrics),
            'limit_overrun_s': self._limit_overrun_s,
        }


def _read_self_version() -> str:
    # benchmarks/__init__.py owns __version__; avoid circular import at module load
    try:
        from benchmarks import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return 'unknown'


def python_version() -> str:
    return platform.python_version()


def py_executable() -> str:
    return sys.executable


def repo_root() -> Path:
    out = _git('rev-parse', '--show-toplevel')
    return Path(out) if out else Path.cwd()
