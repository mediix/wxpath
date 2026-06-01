"""URL canonicalization + canonicalizing dedup wrapper (M6, layer 1).

Dedup in the frontier is membership by URL string. Without normalization,
``/p/1``, ``/p/1?utm_source=x``, ``/p/1#section`` and ``/p/1/`` all read as
distinct URLs ⇒ the same page is fetched (and recorded) several times. This module
collapses such variants to one canonical form *before* the dedup check.

Two pieces:

* :func:`canonicalize` — a **pure function of the URL string** (no DOM, clock, RNG,
  or network), so a canonicalized crawl stays bit-reproducible. Structural
  normalization (lowercase scheme + host, strip the default port) is always applied
  because those forms are equivalent per RFC 3986; three further reductions are
  configurable because they *can* be semantic on some sites: stripping tracking
  params, dropping the fragment, and normalizing the trailing slash.
* :class:`CanonicalizingFrontier` — a composable wrapper (same shape as
  :class:`~wxpath.http.frontier.trap.TrapFilterFrontier`) that rewrites ``task.url``
  to its canonical form at :meth:`push` and canonicalizes the argument of
  :meth:`seen`/:meth:`mark_done`, delegating everything else unchanged.

``get_frontier_backend()`` wraps the configured backend with it only when
``http.client.frontier.canonical.enabled`` — so with the feature off (the default)
the frontier object graph is identical to pre-M6 and default crawls remain
byte-identical (Invariant I5/I11). See ``design/M6-canonicalization.md``.
"""

from __future__ import annotations

import fnmatch
import urllib.parse
from typing import Iterable, Optional

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.base import Frontier
from wxpath.util.logging import get_logger

log = get_logger(__name__)

# Conservative default strip-list: well-known tracking params that never change a
# page's content. Never strip unknown params by default (a param like ``?id=42`` is
# usually semantic) — that risk is called out in FRONTIER_ROADMAP M6.
DEFAULT_STRIP_PARAMS = ("utm_*", "gclid", "fbclid", "sessionid", "msclkid")

# Scheme → default port. A port equal to the scheme default is dropped (``:80`` on
# http is equivalent to no port).
_DEFAULT_PORTS = {"http": "80", "https": "443", "ftp": "21", "ws": "80", "wss": "443"}


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive glob match of a query-param name against the strip-list."""
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, p.lower()) for p in patterns)


def _normalize_netloc(parsed: urllib.parse.SplitResult, scheme: str) -> str:
    """Lowercase host, preserve any userinfo, drop a default port.

    ``parsed.hostname`` is already lowercased by urllib and is the correct canonical
    host (hostnames are case-insensitive per RFC 3986 §3.2.2).
    """
    host = parsed.hostname or ""
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += ":" + parsed.password
        userinfo += "@"
    netloc = userinfo + host
    port = parsed.port
    if port is not None and _DEFAULT_PORTS.get(scheme) != str(port):
        netloc += f":{port}"
    return netloc


def canonicalize(
    url: Optional[str],
    *,
    strip_params: Iterable[str] = (),
    drop_fragment: bool = True,
    normalize_trailing_slash: bool = True,
) -> Optional[str]:
    """Return the canonical form of ``url`` (idempotent, pure).

    Always applied (structural, RFC-equivalent): lowercase scheme + host, drop the
    default port. Configurable reductions:

    * ``strip_params`` — remove query params whose name matches any glob in the list
      (e.g. ``utm_*``), then sort the survivors for a stable form. An empty list
      leaves the query string untouched (and unsorted).
    * ``drop_fragment`` — discard ``#fragment`` (fragments are client-side only).
    * ``normalize_trailing_slash`` — strip trailing ``/`` from non-root paths.

    Falsy input (``None``/``""``) is returned unchanged — the engine never pushes a
    URL-less task, but the wrapper stays defensive.
    """
    if not url:
        return url

    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    netloc = _normalize_netloc(parsed, scheme)

    path = parsed.path
    if normalize_trailing_slash and len(path) > 1:
        path = path.rstrip("/") or "/"

    query = parsed.query
    strip_list = tuple(strip_params or ())
    if strip_list and query:
        kept = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)
            if not _matches_any(k, strip_list)
        ]
        kept.sort()  # stable order so ?b=2&a=1 and ?a=1&b=2 canonicalize alike
        query = urllib.parse.urlencode(kept)

    fragment = "" if drop_fragment else parsed.fragment

    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))


class CanonicalizingFrontier(Frontier):
    """Composable wrapper: canonicalize URLs before dedup/admission/storage.

    Wraps any :class:`Frontier` backend. At :meth:`push` it rewrites ``task.url`` to
    its canonical form (in place — the task is freshly built at the engine's enqueue
    chokepoint and unshared) so the inner backend dedups on the canonical key and the
    engine fetches the canonical URL. :meth:`seen` / :meth:`mark_done` canonicalize
    their argument so membership checks line up. ``pop`` (already-canonical tasks),
    ``size``, ``checkpoint`` and ``close`` delegate unchanged.

    Constructed by ``get_frontier_backend()`` only when
    ``http.client.frontier.canonical.enabled``.
    """

    def __init__(
        self,
        inner: Frontier,
        *,
        strip_params: Iterable[str] = DEFAULT_STRIP_PARAMS,
        drop_fragment: bool = True,
        normalize_trailing_slash: bool = True,
    ) -> None:
        self._inner = inner
        self._strip_params = tuple(strip_params or ())
        self._drop_fragment = drop_fragment
        self._normalize_trailing_slash = normalize_trailing_slash
        self.canonicalized = 0  # observability: pushes whose URL was rewritten

    def _canon(self, url: Optional[str]) -> Optional[str]:
        return canonicalize(
            url,
            strip_params=self._strip_params,
            drop_fragment=self._drop_fragment,
            normalize_trailing_slash=self._normalize_trailing_slash,
        )

    async def push(self, task: CrawlTask) -> bool:
        canon = self._canon(task.url)
        if canon != task.url:
            self.canonicalized += 1
            log.debug("canonicalized", extra={"raw": task.url, "canonical": canon})
            task.url = canon
        return await self._inner.push(task)

    async def pop(self) -> Optional[CrawlTask]:
        return await self._inner.pop()

    async def mark_done(self, url: str) -> None:
        return await self._inner.mark_done(self._canon(url))

    async def seen(self, url: str) -> bool:
        return await self._inner.seen(self._canon(url))

    async def size(self) -> int:
        return await self._inner.size()

    async def checkpoint(self) -> None:
        return await self._inner.checkpoint()

    async def close(self) -> None:
        return await self._inner.close()
