"""Pluggable frontier scoring (M5 — the hybrid wedge).

A :class:`FrontierScorer` maps a discovered :class:`~wxpath.core.models.CrawlIntent`
to a priority (higher = crawl sooner). The engine consults the active scorer at the
enqueue chokepoint to set ``CrawlTask.priority``.

Two scorers ship:

- :class:`DeterministicScorer` (**default**) — a pure passthrough of the M3/M4
  ``priority=`` DSL score. With it active the engine computes the *identical*
  priority it did pre-M5, so the default crawl is byte-identical (Invariant I5) and
  no ML dependency is imported.
- :class:`SemanticScorer` (**opt-in**) — orders links by cosine relevance of their
  ``anchor_text + url`` to a crawl-wide objective, using an injected
  :class:`Embedder` (the ``wxpath[semantic]`` extra). It changes traversal **order**
  only, never extracted **data** (Invariant I4/I10): which pages survive a budget
  shifts, what any visited page yields does not.

Selection is by settings via :func:`get_frontier_scorer`, mirroring
:func:`~wxpath.http.frontier.get_frontier_backend`. See
``design/M5-hybrid-scorer.md``.
"""

import math
from typing import Optional, Protocol, Sequence, runtime_checkable

from wxpath.core.models import CrawlIntent
from wxpath.settings import SETTINGS
from wxpath.util.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "FrontierScorer",
    "DeterministicScorer",
    "SemanticScorer",
    "Embedder",
    "get_frontier_scorer",
    "needs_anchor_text",
]


@runtime_checkable
class FrontierScorer(Protocol):
    """Maps a discovered intent to a priority. Higher = crawl sooner."""

    def score(self, intent: CrawlIntent) -> Optional[float]:
        """Return a priority for ``intent`` (higher = sooner).

        ``None`` means "no opinion" — the engine then falls back to depth so the
        link is scheduled in BFS order (Invariant I5).
        """
        ...


class DeterministicScorer:
    """DEFAULT scorer: pure passthrough of the M3/M4 ``priority=`` DSL score.

    Returns ``intent.score`` unchanged, so the engine enqueue path is identical to
    pre-M5. ML-free — importing this module never pulls in an embedder.
    """

    def score(self, intent: CrawlIntent) -> Optional[float]:
        return intent.score


@runtime_checkable
class Embedder(Protocol):
    """Maps texts to vectors. Injected so core carries no hard ML dependency."""

    def embed(self, texts: list[str]) -> list[Sequence[float]]:
        ...


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors (stdlib; works for lists or ndarrays)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SemanticScorer:
    """OPT-IN scorer: order links by relevance of ``anchor_text + url`` to an objective.

    Scores are ``cosine(embed(anchor_text + " " + url), embed(objective))``. A link
    with no anchor text yields ``None`` (BFS fallback for that link). Results are
    cached by anchor text so a repeated link text is embedded once.

    Affects traversal ORDER only — the extraction path never sees the scorer, so
    extracted data is identical to a deterministic run (Invariant I4/I10). A semantic
    crawl is reproducible given a *pinned* embedder, not across embedder versions.

    Note: :meth:`score` embeds synchronously; for large crawls a real embedder should
    be batched/off-loaded (see design §6). The cache bounds repeat cost.
    """

    def __init__(self, objective: str, embedder: Embedder, *, cache: bool = True) -> None:
        if not objective:
            log.warning("semantic scorer constructed with empty objective → all links score 0")
        self._embedder = embedder
        self._objective_vec = embedder.embed([objective])[0]
        self._cache: Optional[dict[str, float]] = {} if cache else None

    def score(self, intent: CrawlIntent) -> Optional[float]:
        text = (intent.anchor_text or "").strip()
        if not text:
            return None  # no textual signal → let the engine fall back to BFS
        if self._cache is not None and text in self._cache:
            return self._cache[text]
        vec = self._embedder.embed([f"{text} {intent.url}"])[0]
        sim = _cosine(vec, self._objective_vec)
        if self._cache is not None:
            self._cache[text] = sim
        return sim


def needs_anchor_text() -> bool:
    """Whether the active scorer needs per-link anchor text.

    Read live from settings so the discovery layer (``ops._discover_scored``) can
    resolve anchors on the *unscored* path only when a text-based scorer is active —
    keeping the deterministic default's discovery path byte-identical (I5).
    """
    return SETTINGS.http.client.frontier.scorer == "semantic"


def get_frontier_scorer() -> FrontierScorer:
    """Construct the configured frontier scorer (mirrors ``get_frontier_backend``).

    ``deterministic`` (the default) is cheap and ML-free. ``semantic`` lazily imports
    the embedder so selecting it is the only path that touches the ``[semantic]`` extra.
    """
    cfg = SETTINGS.http.client.frontier
    name = cfg.scorer
    if name == "deterministic":
        return DeterministicScorer()
    if name == "semantic":
        from wxpath.http.frontier.embedders import get_embedder
        return SemanticScorer(cfg.objective, get_embedder(cfg.semantic), cache=cfg.semantic.cache)
    raise ValueError(f"Unknown frontier scorer: {name}")
