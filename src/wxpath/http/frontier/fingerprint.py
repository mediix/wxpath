"""Content-fingerprint near-duplicate dedup (M6 — layer 2).

URL canonicalization (layer 1) collapses URL variants of the *same address*. It
cannot catch the same *content* served under genuinely different URLs — mirror
domains, print views, ``?page=`` chrome wrapping identical bodies, CMS alias paths.
This layer fingerprints the main-content text of each fetched page and skips pages
whose fingerprint is within a Hamming threshold of one already recorded — so a
near-duplicate page is **recorded once**.

Design — mirrors the M5 scorer (:mod:`wxpath.http.frontier.scoring`), NOT a frontier
wrapper:

* A :class:`ContentDeduper` is a strategy object the engine constructs in ``run()``
  (via :func:`get_content_deduper`) and consults *after parse*, where the parsed DOM
  is available — content dedup is an engine post-parse concern, not URL scheduling,
  so it does not belong on the :class:`~wxpath.http.frontier.base.Frontier` Protocol.
  Its registry is **ephemeral per run** (like the semantic scorer's embedding cache);
  cross-resume fingerprint persistence is a further deferral (see
  ``design/M6-canonicalization.md`` §6).
* :class:`NullDeduper` (**default**) never deduplicates → the engine processing path
  is byte-identical to pre-layer-2 (Invariant I5).
* :class:`SimHashDeduper` (**opt-in**, ``http.client.frontier.fingerprint.enabled``)
  computes a 64-bit SimHash over word-shingles of the main content and matches within
  ``hamming_threshold`` bits.

Determinism: SimHash uses a **stable** hash (BLAKE2b), never Python's salted
``hash()``, so a fingerprinted crawl is bit-reproducible (extends I6/I11 → I12). With
a deterministic crawl order, which of two near-duplicates is recorded-vs-skipped is
itself deterministic. The deduper affects which pages' data is *recorded*, never the
data extracted from the page that is kept (a realization of I4).
"""

from __future__ import annotations

import hashlib
from typing import List, Optional, Protocol, runtime_checkable

from wxpath.settings import SETTINGS
from wxpath.util.cleaners import main_text_extractor
from wxpath.util.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "ContentDeduper",
    "NullDeduper",
    "SimHashDeduper",
    "get_content_deduper",
    "simhash",
    "hamming",
]


# --------------------------------------------------------------------------- #
# Pure SimHash primitives (deterministic — stable hash, no clock/RNG/network)
# --------------------------------------------------------------------------- #
def _stable_hash(token: str, bits: int) -> int:
    """Deterministic ``bits``-wide hash of ``token`` (BLAKE2b, not salted hash())."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=bits // 8).digest()
    return int.from_bytes(digest, "big")


def _shingles(tokens: List[str], k: int) -> List[str]:
    """Overlapping ``k``-word shingles; the whole token list if shorter than ``k``."""
    if len(tokens) < k:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)]


def simhash(text: str, *, bits: int = 64, shingle_size: int = 4) -> int:
    """Charikar SimHash of ``text`` over word-shingles. Pure and deterministic.

    Returns ``0`` for empty text (the caller treats a zero/empty fingerprint as
    "no signal" and never deduplicates on it).
    """
    tokens = text.split()
    features = _shingles(tokens, shingle_size)
    if not features:
        return 0
    counts = [0] * bits
    for feat in features:
        h = _stable_hash(feat, bits)
        for i in range(bits):
            counts[i] += 1 if (h >> i) & 1 else -1
    fp = 0
    for i in range(bits):
        if counts[i] > 0:
            fp |= 1 << i
    return fp


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two fingerprints."""
    return (a ^ b).bit_count()


def _content_text(elem) -> str:
    """Main-article text of a parsed page, robust to extractor failure.

    Prefers the boilerplate-stripping :func:`main_text_extractor` (so nav/footer
    chrome doesn't make distinct pages look alike, nor identical chrome make them
    look duplicate); falls back to all descendant text, then to ``""``.
    """
    if elem is None:
        return ""
    try:
        text = main_text_extractor(elem)
        if text and text.strip():
            return text.strip()
    except Exception:
        log.debug("main_text_extractor failed; falling back to itertext")
    try:
        return "".join(elem.itertext()).strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Strategy objects
# --------------------------------------------------------------------------- #
@runtime_checkable
class ContentDeduper(Protocol):
    """Decides whether a freshly-parsed page is a near-duplicate of a prior one."""

    def is_duplicate(self, elem) -> bool:
        """Return ``True`` if ``elem``'s content duplicates an already-recorded page.

        A ``False`` result records ``elem``'s fingerprint as newly seen (so the
        first occurrence of any content is always kept). Implementations must be
        side-effect-free beyond their own registry.
        """
        ...


class NullDeduper:
    """DEFAULT deduper: never deduplicates → engine path byte-identical (I5)."""

    def is_duplicate(self, elem) -> bool:
        return False


class SimHashDeduper:
    """OPT-IN deduper: skip pages whose main-content SimHash is within a threshold.

    Maintains an in-run registry of 64-bit fingerprints and matches a new page
    against it by Hamming distance. The first page of any content is recorded and
    kept; later near-duplicates return ``True`` so the engine skips them (no yield,
    no link expansion). Empty/zero fingerprints (no extractable text) are never
    treated as duplicates.
    """

    def __init__(self, *, hamming_threshold: int = 3, shingle_size: int = 4, bits: int = 64) -> None:
        self._threshold = hamming_threshold
        self._shingle_size = shingle_size
        self._bits = bits
        self._seen_fps: List[int] = []  # ephemeral per-run registry
        self.duplicates = 0             # observability: pages skipped as near-dups

    def is_duplicate(self, elem) -> bool:
        text = _content_text(elem)
        if not text:
            return False  # no textual signal → cannot fingerprint → never a dup
        fp = simhash(text, bits=self._bits, shingle_size=self._shingle_size)
        if fp == 0:
            return False
        for prior in self._seen_fps:
            if hamming(fp, prior) <= self._threshold:
                self.duplicates += 1
                log.debug("content near-duplicate skipped", extra={"hamming_threshold": self._threshold})
                return True
        self._seen_fps.append(fp)
        return False


def get_content_deduper() -> ContentDeduper:
    """Construct the configured content deduper (mirrors ``get_frontier_scorer``).

    ``fingerprint.enabled`` is off by default → :class:`NullDeduper` → the engine
    processing path is identical to pre-layer-2 (Invariant I5).
    """
    cfg = SETTINGS.http.client.frontier.fingerprint
    if not cfg.enabled:
        return NullDeduper()
    return SimHashDeduper(
        hamming_threshold=cfg.hamming_threshold,
        shingle_size=cfg.shingle_size,
    )
