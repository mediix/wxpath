# M5 — The Hybrid Wedge (Pluggable Semantic Scorer) · Implementation Plan

> **Branch:** `feat/m5-hybrid-scorer` (off `feat/m4b-trap-detection`)
> **Parent:** [FRONTIER_ROADMAP.md §M5](../FRONTIER_ROADMAP.md#m5--the-hybrid-wedge-pluggable-semantic-scorer)
> **Builds on:** M3 declarative scoring (`8e17b53`), M4a link-provenance (`31fbc44`),
> M4b trap detection (`66f4527`). M5 is the last Phase-2 move.

---

## 0. Scope decision (confirmed with user 2026-05-31)

wxpath's moat is **determinism**; "relevance-guided discovery" is exactly where
Exa/Parallel claim superiority ([COMPARISONS.md](../COMPARISONS.md)). M5 lets a crawl
be *steered toward an objective* **without** surrendering deterministic output, by
exploiting the one asymmetry that makes it free:

> A scorer changes traversal **order** only, never extracted **data**. Order decides
> which pages survive a `max_urls`/wall-clock budget; it never changes what a visited
> page yields. (Roadmap **Invariant I4**.)

So M5 makes scoring a **pluggable strategy**: the M3/M4 deterministic path is the
default; a semantic scorer is a strictly opt-in plugin shipped as `wxpath[semantic]`.

Two scope decisions for this pass:

- **Integration point: the engine enqueue chokepoint, NOT a `push()` wrapper.** The
  roadmap §M5 sketch said "frontier `push()` calls `scorer.score(intent)`". Taken
  literally that fails: `push()` receives a `CrawlTask`, which carries **no** anchor
  text or provenance — a semantic scorer's inputs. A `push()`-level scorer would force
  persisting those inputs as sqlite/redis blobs on every task. Instead the scorer runs
  at [engine.py](../src/wxpath/core/runtime/engine.py) where `intent.score → priority`
  is **already** computed and the live `CrawlIntent` + anchor context still exist. The
  semantic inputs stay **ephemeral**; only the resulting priority float is persisted.
  This is the M4b lesson re-applied — pick the seam that needs zero backend edits.

- **Objective comes from settings, not the DSL (this pass).** A `priority=` expression
  already exists for deterministic steering. A semantic *objective* is a crawl-wide
  knob, so it lives in `http.client.frontier.objective`. A DSL `objective=` argument is
  deferred (§6) — it would touch the parser, which M5 deliberately does not.

---

## 1. Why a strategy object, not branches in the engine

M3 hard-coded one scoring rule at the chokepoint: `priority = -intent.score`. M5
needs **two** rules (deterministic passthrough, semantic relevance) selectable at
runtime, with the semantic one carrying an optional ML dependency that must never
touch the default import graph.

A `FrontierScorer` strategy — selected by a factory that mirrors
[`get_frontier_backend()`](../src/wxpath/http/frontier/__init__.py) and
`get_cache_backend()` — gives exactly that: the engine calls `scorer.score(intent)`
and stays oblivious to which rule is active. The default `DeterministicScorer` is a
pure passthrough of `intent.score`, so the chokepoint computes the **identical**
priority it does today → I5 byte-identical with zero behavior change. The semantic
scorer and its embedder are lazy-imported only when `scorer: semantic`.

```
get_frontier_scorer()
  ├─ DeterministicScorer()     ← default; score(intent) == intent.score; no ML import
  └─ SemanticScorer(objective, embedder)   ← opt-in; cosine(anchor_text+url, objective)
```

---

## 2. The scorer module (`src/wxpath/http/frontier/scoring.py`)

```python
"""Pluggable frontier scoring (M5).

A FrontierScorer maps a discovered CrawlIntent to a priority (higher = crawl
sooner). The default DeterministicScorer passes through the M3/M4 DSL score, so
the engine's enqueue path is byte-identical to pre-M5 (Invariant I5). An opt-in
SemanticScorer orders links by relevance to a crawl-wide objective — changing
traversal ORDER only, never extracted DATA (Invariant I4).
"""

from typing import Optional, Protocol, Sequence, runtime_checkable

from wxpath.core.models import CrawlIntent


@runtime_checkable
class FrontierScorer(Protocol):
    def score(self, intent: CrawlIntent) -> Optional[float]:
        """Higher = crawl sooner. None = unscored → engine falls back to depth (BFS)."""
        ...


class DeterministicScorer:
    """DEFAULT. Pure passthrough of the M3/M4 `priority=` DSL score. ML-free."""
    def score(self, intent: CrawlIntent) -> Optional[float]:
        return intent.score


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[Sequence[float]]:
        """Map texts to vectors. Injected dependency — no hard ML dep in core."""
        ...


class SemanticScorer:
    """OPT-IN. Orders links by cosine relevance of (anchor_text + url) to an
    objective. Caches by anchor-text hash; never mutates extracted data (I4)."""

    def __init__(self, objective: str, embedder: Embedder, *, cache: bool = True):
        self._embedder = embedder
        self._objective_vec = embedder.embed([objective])[0]
        self._cache: dict[str, float] | None = {} if cache else None

    def score(self, intent: CrawlIntent) -> Optional[float]:
        text = (intent.anchor_text or "").strip()
        if not text:
            return None                      # no signal → BFS fallback for this link
        key = text                            # cache by anchor text (hash in impl)
        if self._cache is not None and key in self._cache:
            return self._cache[key]
        vec = self._embedder.embed([f"{text} {intent.url}"])[0]
        s = _cosine(vec, self._objective_vec)
        if self._cache is not None:
            self._cache[key] = s
        return s
```

`_cosine` is a small pure helper (numpy if present, else a stdlib dot/norm). The
real `SemanticScorer.score` wraps the embed call in `asyncio.to_thread` *if* called
from the loop — but see §3: the deterministic default is sync and per-intent, and the
semantic batching strategy is spec'd in §6 to keep the I5 hot path untouched.

### Factory (same module)

```python
def get_frontier_scorer() -> FrontierScorer:
    cfg = SETTINGS.http.client.frontier
    if cfg.scorer == "deterministic":
        return DeterministicScorer()                       # cheap, no ML import
    if cfg.scorer == "semantic":
        from wxpath.http.frontier.embedders import get_embedder   # lazy → [semantic] extra
        return SemanticScorer(cfg.objective, get_embedder(cfg.semantic), cache=cfg.semantic.cache)
    raise ValueError(f"Unknown frontier scorer: {cfg.scorer}")
```

Lazy import mirrors the backend lazy-imports in
[frontier/__init__.py](../src/wxpath/http/frontier/__init__.py) — selecting
`deterministic` never imports the ML stack.

### Reference embedder (`src/wxpath/http/frontier/embedders.py`, behind the extra)

```python
class SentenceTransformerEmbedder:
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer   # [semantic] extra only
        self._model = SentenceTransformer(model)
    def embed(self, texts):
        return self._model.encode(texts, normalize_embeddings=True)

def get_embedder(semantic_cfg) -> Embedder:
    return SentenceTransformerEmbedder(semantic_cfg.model)
```

Tests inject a **stub** `Embedder` returning canned vectors, so the seam is exercised
with no ML dependency in CI (§5).

---

## 3. CrawlIntent gains an ephemeral semantic input (`core/models.py`)

`SemanticScorer` needs the link's anchor text. It is **already available** at
discovery: `_discover_scored` ([ops.py:130](../src/wxpath/core/ops.py)) iterates
`(url, anchor)` pairs from `get_links_with_anchors_from_elem_and_xpath`
([dom.py:25](../src/wxpath/core/dom.py)). We capture it onto the intent — and **only**
the intent:

```python
@dataclass(slots=True)
class CrawlIntent(Intent):
    url: str
    next_segments: list
    score: float | None = None
    anchor_text: str | None = None     # ← M5: ephemeral semantic input (intent-only)
    # provenance: "Provenance | None" = None   # ← M4 (still deferred)
```

`anchor_text` is **not** carried onto `CrawlTask` and **never persisted** — it exists
only between discovery (ops) and scoring (engine chokepoint). Default `None` leaves
every existing path unchanged; a missing anchor yields `None` → BFS fallback.

**Resolving the unscored-path gap (implementation decision).** A semantic crawl
normally uses an *unscored* `url(//a/@href)` (no `priority=`), and today the unscored
discovery path (`get_absolute_links_from_elem_and_xpath`) resolves **no anchors** — so
`anchor_text` would always be `None` and the semantic scorer would no-op. The fix is to
gate anchor resolution on **"a dynamic score is present OR a text-based scorer is
active"**, the latter read live via `scoring.needs_anchor_text()` (`scorer ==
"semantic"`). `_discover_scored` then yields `(url, score, anchor_text)`:

```python
dynamic = score_node is not None and score_node.const is None
if dynamic or needs_anchor_text():
    pairs = get_links_with_anchors_from_elem_and_xpath(curr_elem, path_exp)  # anchors
else:
    pairs = ((u, None) for u in get_absolute_links_from_elem_and_xpath(curr_elem, path_exp))
...
yield url, _resolve_score(score_node, anchor), _anchor_text(anchor)
```

This keeps the deterministic default's discovery path **byte-identical** (I5):
`needs_anchor_text()` is `False`, so the unscored path is untouched. The
anchor-resolving discovery only engages under `scorer: semantic`, where byte-repro
across runs is not promised anyway — and `dom.py` documents that the anchor path
yields the **same URL set** as `xpath3`, so coverage is unaffected. Consumers (the
`ContextItem` branch + the two `_discover_scored` loops in
[ops.py](../src/wxpath/core/ops.py)) pass `anchor_text` into `CrawlIntent`.

`_anchor_text(anchor)` uses `itertext()` (works for both `HtmlElement` and
elementpath's `XPath3Element` — the M4a lesson) and collapses whitespace; it is a pure
read of served HTML (no network/clock → I6 family).

---

## 4. Engine wiring (the chokepoint) + settings

### Engine (`core/runtime/engine.py`)

Construct the scorer once in `run()`, beside the frontier:

```python
frontier = get_frontier_backend()
scorer   = get_frontier_scorer()          # ← M5
```

At the enqueue chokepoint (today's [engine.py:450](../src/wxpath/core/runtime/engine.py)):

```python
# M5: the active scorer maps the discovered intent to a priority. The default
# DeterministicScorer returns intent.score, so this is byte-identical to M3/M4.
score = scorer.score(intent)
priority = -score if score is not None else None
```

The negation and `None → BFS` fallback are unchanged from M3 — only the *source* of
`score` moves from `intent.score` to `scorer.score(intent)`. With
`scorer: deterministic` these are the same value, so the loop shape and output are
identical (the I5 regression contract).

### Settings (`settings.py`, under `http.client.frontier`)

Append, in the same style as the existing `trap` block:

```python
# M5: pluggable frontier scoring. 'deterministic' (default) passes through the
# M3/M4 priority= score → byte-identical to pre-M5 (Invariant I5). 'semantic'
# orders links by relevance to `objective` using an injected embedder (the
# wxpath[semantic] extra) — traversal ORDER only, never extracted DATA (I4).
'scorer': "deterministic",      # deterministic | semantic
'objective': "",                # semantic target objective (semantic scorer only)
'semantic': {
    'model': "all-MiniLM-L6-v2",  # reference embedder model id
    'cache': True,                # cache scores by anchor-text hash
},
```

### Packaging (`pyproject.toml`, `[project.optional-dependencies]`)

Add, alongside `tui` / `frontier-redis`:

```toml
semantic = ["sentence-transformers>=2.2", "numpy>=1.24"]
```

Core imports nothing from this extra unless `scorer: semantic` (the factory's lazy
import). `numpy` already appears in the `bench` extra, so it is a known-good pin.

---

## 5. Tests

`tests/http/frontier/test_scoring.py` (pure + seam, no ML dep):
- `DeterministicScorer.score(intent)` returns `intent.score` for scored / `None` for
  unscored intents (passthrough contract).
- `SemanticScorer` with a **stub embedder** (canned orthogonal vectors): an
  on-objective anchor scores higher than an off-objective anchor; empty/`None`
  `anchor_text` → `None`; cache returns the same value on repeat without re-embedding
  (assert embedder call count).
- `get_frontier_scorer()`: `scorer="deterministic"` → `DeterministicScorer`;
  `scorer="semantic"` → `SemanticScorer` (stub embedder injected); unknown → `ValueError`.

`tests/core/runtime/test_engine.py` (e2e, the I4 proof):
- Fixture with on-topic + off-topic link blocks. Run once `deterministic`, once
  `semantic` (stub embedder) under a tight `max_urls`. Assert (a) the semantic run
  fetches on-topic pages before off-topic ones, **and** (b) for any page visited by
  both runs the extracted record is **identical** — order changed, data did not (I4).

`anchor_text` capture: extend an ops/dom test to assert a discovered `CrawlIntent`
carries the anchor's `text_content()` and that an anchorless discovery leaves it `None`.

**I5 gate:** `scorer` defaults `deterministic`; full suite green; `wxpath-bench run
local_fixture` byte-identical to the M4b baseline (`d0:1 d1:4 d2:16 d3:64 d4:256
d5:171`, 511 extractions). The reproducibility gate always runs the deterministic
scorer.

---

## 6. Deferred / future

- **DSL `objective=` argument** — a per-`url()` objective in the grammar (touches the
  parser, like M3's `priority=`). v1 keeps the objective in settings.
- **Per-page batch scoring** — gather a page's `CrawlIntent`s, embed all anchor texts
  in one `embedder.embed([...])` call (via `asyncio.to_thread`), then assign + push.
  This is the roadmap's "batch at the block level" mitigation. Kept optional so the
  deterministic I5 hot path stays per-intent and the engine loop shape is unchanged;
  promote it if semantic-crawl latency proves it necessary.
- **Provenance-enriched embedding input** — fold an M4a `Provenance` summary
  (parent-tag, section) into the embedded text once `Provenance` is persisted on the
  intent. Currently M4a exposes provenance only via `wx:` DSL functions.

---

## 7. File manifest

**Add**
- `src/wxpath/http/frontier/scoring.py` — `FrontierScorer`, `DeterministicScorer`,
  `SemanticScorer`, `Embedder`, `get_frontier_scorer`, `_cosine`.
- `src/wxpath/http/frontier/embedders.py` — `SentenceTransformerEmbedder`,
  `get_embedder` (behind the `[semantic]` extra).
- `tests/http/frontier/test_scoring.py` (+ e2e case in `tests/core/runtime/test_engine.py`).

**Modify**
- `src/wxpath/core/models.py` — `CrawlIntent.anchor_text`.
- `src/wxpath/core/ops.py` — capture `anchor_text` at the three `CrawlIntent` sites.
- `src/wxpath/core/runtime/engine.py` — construct `scorer`; chokepoint uses
  `scorer.score(intent)`.
- `src/wxpath/settings.py` — `http.client.frontier` scorer block.
- `pyproject.toml` — `semantic` extra.

**No** change to frontier backends (`memory`/`sqlite`/`redis`), `CrawlTask`, the trap
wrapper, the parser, or the DSL grammar.

---

## 8. New invariant (append to DESIGN.md with M5)

```
I10 (semantic order, M5): A semantic FrontierScorer affects traversal ORDER only,
    never extracted DATA (a concrete realization of I4). With scorer=deterministic
    (the default) the scorer is a pure passthrough of the M3/M4 score, so the engine
    enqueue path is byte-identical to pre-M5 (extends I5). A semantic crawl is
    reproducible given a pinned embedder, but coverage-under-budget is not guaranteed
    across embedder versions; the reproducibility gate runs scorer=deterministic.
```

---

## 9. Build order — IMPLEMENTED

1. ✅ `scoring.py`: `FrontierScorer` + `DeterministicScorer` + `_cosine` + factory;
   settings block; engine `scorer` construct + chokepoint edit. Suite green +
   `local_fixture` I5 byte-identical (deterministic default, no ML touched).
2. ✅ `CrawlIntent.anchor_text` + ops capture via `needs_anchor_text()` gating
   (additive, default `None`; I5 path unchanged).
3. ✅ `SemanticScorer` + `Embedder` protocol + stub-embedder unit tests (10) + engine
   e2e order-vs-data / I4-I10 test (2).
4. ✅ `embedders.py` reference impl + `[semantic]` extra; I10 appended to
   FRONTIER_ROADMAP.md invariant block (where I8/I9 live, per M4b precedent).

**Result:** suite **312 passed** (was 300; +12). `wxpath-bench run local_fixture`
byte-identical to the M4b baseline (`d0:1 d1:4 d2:16 d3:64 d4:256 d5:171`, 511
extractions, content 510). I10 holds.
