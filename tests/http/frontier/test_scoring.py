"""M5: pluggable frontier scoring — DeterministicScorer, SemanticScorer, factory."""

import pytest

from wxpath.core.models import CrawlIntent
from wxpath.http.frontier import scoring
from wxpath.http.frontier.scoring import (
    DeterministicScorer,
    SemanticScorer,
    get_frontier_scorer,
    needs_anchor_text,
)
from wxpath.settings import SETTINGS

FRONTIER = SETTINGS.http.client.frontier


class StubEmbedder:
    """Deterministic bag-of-words embedder over a fixed vocabulary (no ML dep).

    embed(text) is a 0/1 presence vector over ``vocab``; cosine of two such vectors
    grows with shared vocabulary, so an on-objective text outscores an off-topic one.
    """

    def __init__(self, vocab):
        self._vocab = list(vocab)
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            words = set(t.lower().split())
            out.append([1.0 if v in words else 0.0 for v in self._vocab])
        return out


def _intent(url="http://h/p", score=None, anchor_text=None):
    return CrawlIntent(url=url, next_segments=[], score=score, anchor_text=anchor_text)


# --- DeterministicScorer (default) ----------------------------------------

def test_deterministic_passes_through_score():
    s = DeterministicScorer()
    assert s.score(_intent(score=4.0)) == 4.0


def test_deterministic_none_stays_none():
    """Unscored intent → None so the engine falls back to BFS (I5)."""
    assert DeterministicScorer().score(_intent(score=None)) is None


# --- SemanticScorer (opt-in) ----------------------------------------------

VOCAB = ["lithium", "battery", "recycling", "celebrity", "gossip", "weather"]


def test_semantic_ranks_on_topic_above_off_topic():
    emb = StubEmbedder(VOCAB)
    s = SemanticScorer("lithium battery recycling", emb)
    on = s.score(_intent(url="http://h/a", anchor_text="battery recycling plant"))
    off = s.score(_intent(url="http://h/b", anchor_text="celebrity gossip weather"))
    assert on > off
    assert off == 0.0  # no shared vocabulary with the objective


def test_semantic_empty_anchor_text_is_none():
    """No textual signal → None → BFS fallback for that link."""
    s = SemanticScorer("lithium battery", StubEmbedder(VOCAB))
    assert s.score(_intent(anchor_text=None)) is None
    assert s.score(_intent(anchor_text="   ")) is None


def test_semantic_caches_by_anchor_text():
    emb = StubEmbedder(VOCAB)
    s = SemanticScorer("battery", emb)            # 1 embed call for the objective
    calls_after_init = emb.calls
    a = s.score(_intent(url="http://h/1", anchor_text="battery news"))
    b = s.score(_intent(url="http://h/2", anchor_text="battery news"))  # same text
    assert a == b
    assert emb.calls == calls_after_init + 1      # second score served from cache


def test_semantic_cache_off_re_embeds(monkeypatch):
    emb = StubEmbedder(VOCAB)
    s = SemanticScorer("battery", emb, cache=False)
    base = emb.calls
    s.score(_intent(anchor_text="battery news"))
    s.score(_intent(anchor_text="battery news"))
    assert emb.calls == base + 2                  # no cache → embed each time


# --- needs_anchor_text() + factory ----------------------------------------

def test_needs_anchor_text_tracks_setting(monkeypatch):
    monkeypatch.setitem(FRONTIER, "scorer", "deterministic")
    assert needs_anchor_text() is False
    monkeypatch.setitem(FRONTIER, "scorer", "semantic")
    assert needs_anchor_text() is True


def test_factory_default_is_deterministic(monkeypatch):
    monkeypatch.setitem(FRONTIER, "scorer", "deterministic")
    assert isinstance(get_frontier_scorer(), DeterministicScorer)


def test_factory_semantic_uses_injected_embedder(monkeypatch):
    monkeypatch.setitem(FRONTIER, "scorer", "semantic")
    monkeypatch.setitem(FRONTIER, "objective", "battery")
    monkeypatch.setattr(
        "wxpath.http.frontier.embedders.get_embedder",
        lambda cfg: StubEmbedder(VOCAB),
    )
    scorer = get_frontier_scorer()
    assert isinstance(scorer, SemanticScorer)
    assert scorer.score(_intent(anchor_text="battery")) > 0


def test_factory_unknown_scorer_raises(monkeypatch):
    monkeypatch.setitem(FRONTIER, "scorer", "bogus")
    with pytest.raises(ValueError, match="Unknown frontier scorer"):
        get_frontier_scorer()
