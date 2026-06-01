"""Reference embedder for the semantic frontier scorer (M5, ``wxpath[semantic]``).

This module is imported **lazily** by
:func:`~wxpath.http.frontier.scoring.get_frontier_scorer` only when
``http.client.frontier.scorer == 'semantic'`` — so the default (deterministic) crawl
never pulls in an ML dependency. ``sentence_transformers`` is itself imported inside
:class:`SentenceTransformerEmbedder` so even importing this module stays cheap; the
heavy import happens only when the embedder is constructed.

The embedder is an injected dependency (the :class:`~wxpath.http.frontier.scoring.Embedder`
protocol), so tests pass a stub returning canned vectors and never need the extra.
"""

from typing import Sequence

from wxpath.http.frontier.scoring import Embedder
from wxpath.util.logging import get_logger

log = get_logger(__name__)


class SentenceTransformerEmbedder:
    """:class:`Embedder` backed by a ``sentence-transformers`` model.

    Requires the ``wxpath[semantic]`` extra. Vectors are L2-normalized so the
    scorer's cosine reduces to a dot product.
    """

    def __init__(self, model: str) -> None:
        from sentence_transformers import SentenceTransformer  # [semantic] extra
        log.info("loading semantic embedder", extra={"model": model})
        self._model = SentenceTransformer(model)

    def embed(self, texts: list[str]) -> list[Sequence[float]]:
        return self._model.encode(texts, normalize_embeddings=True)


def get_embedder(semantic_cfg) -> Embedder:
    """Construct the reference embedder from the ``frontier.semantic`` settings block."""
    return SentenceTransformerEmbedder(semantic_cfg.model)
