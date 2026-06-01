"""Pluggable frontier backends.

Mirrors the cache-backend factory pattern in
``wxpath.http.client.cache.get_cache_backend``: a single factory switched on a
settings key. Default ``memory`` preserves historical engine behavior; ``sqlite``
adds single-node resume; ``redis`` adds a shared/distributed frontier.
"""

from wxpath.http.frontier.base import Frontier
from wxpath.settings import SETTINGS
from wxpath.util.logging import get_logger

log = get_logger(__name__)

FRONTIER_SETTINGS = SETTINGS.http.client.frontier

__all__ = ["Frontier", "get_frontier_backend"]


def _build_backend() -> Frontier:
    """Construct the configured frontier backend (pre-wrapping).

    Backends are imported lazily so that selecting ``memory`` never imports the
    optional ``sqlite``/``redis`` modules (and their dependencies).
    """
    backend = FRONTIER_SETTINGS.backend
    log.info("frontier backend", extra={"backend": backend})

    if backend == "memory":
        from wxpath.http.frontier.memory import InMemoryFrontier
        return InMemoryFrontier()

    if backend == "sqlite":
        from wxpath.http.frontier.sqlite import SQLiteFrontier
        return SQLiteFrontier(
            path=FRONTIER_SETTINGS.sqlite.path,
            resume=FRONTIER_SETTINGS.resume,
            poll_interval=FRONTIER_SETTINGS.poll_interval,
        )

    if backend == "redis":
        from wxpath.http.frontier.redis import RedisFrontier
        return RedisFrontier(
            address=FRONTIER_SETTINGS.redis.address,
            namespace=FRONTIER_SETTINGS.redis.namespace,
            resume=FRONTIER_SETTINGS.resume,
            poll_interval=FRONTIER_SETTINGS.poll_interval,
        )

    raise ValueError(f"Unknown frontier backend: {backend}")


def get_frontier_backend() -> Frontier:
    """Construct the configured frontier backend, optionally trap-filtered.

    When ``http.client.frontier.trap.enabled`` (M4b), the backend is wrapped in a
    :class:`~wxpath.http.frontier.trap.TrapFilterFrontier` that drops URL-path-repeat
    traps at ``push()``. With it disabled (the default) the bare backend is returned
    unchanged — the frontier object graph is identical to pre-M4b (Invariant I5).
    """
    backend = _build_backend()

    trap = FRONTIER_SETTINGS.trap
    if trap.enabled:
        from wxpath.http.frontier.trap import TrapFilterFrontier
        backend = TrapFilterFrontier(
            backend,
            max_path_repeat=trap.max_path_repeat,
            max_period=trap.max_period,
        )
    return backend
