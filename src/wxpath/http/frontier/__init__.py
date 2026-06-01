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
    """Construct the configured frontier backend, optionally wrapped.

    Two composable wrappers may be layered on, each gated by its own settings flag
    and absent (the default) so the object graph stays identical to the bare backend
    (Invariant I5):

    * **M4b** ``http.client.frontier.trap.enabled`` →
      :class:`~wxpath.http.frontier.trap.TrapFilterFrontier` drops URL-path-repeat
      traps at ``push()``.
    * **M6** ``http.client.frontier.canonical.enabled`` →
      :class:`~wxpath.http.frontier.canonical.CanonicalizingFrontier` normalizes each
      URL before dedup/admission.

    The canonicalizer is the **outermost** wrapper so URLs are normalized *before*
    the trap filter and the backend's dedup ever see them (trap detection and dedup
    both operate on the canonical form).
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

    canonical = FRONTIER_SETTINGS.canonical
    if canonical.enabled:
        from wxpath.http.frontier.canonical import CanonicalizingFrontier
        backend = CanonicalizingFrontier(
            backend,
            strip_params=canonical.strip_params,
            drop_fragment=canonical.drop_fragment,
            normalize_trailing_slash=canonical.normalize_trailing_slash,
        )
    return backend
