# `wxpath`: The Programmable Frontier — Roadmap & Design

> **Status:** Design proposal (not yet implemented). This document is the master
> plan. Each **Move** below is meant to be lifted out one-by-one, spec'd into its
> own implementation ticket, and built/tested individually. Nothing here changes
> `src/wxpath/*` yet.
>
> **Companion docs:** [DESIGN.md](DESIGN.md) (the DSL & engine semantics),
> [COMPARISONS.md](COMPARISONS.md) (wxpath vs Exa.ai / Parallel Web Systems).

## Contents

- [Thesis: stop calling it "BFS"](#thesis-stop-calling-it-bfs)
- [Current architecture (verified understanding)](#current-architecture-verified-understanding)
- [The gaps, mapped to moves](#the-gaps-mapped-to-moves)
- [The roadmap (sequence & dependency graph)](#the-roadmap-sequence--dependency-graph)
- [Design specs, move-by-move](#design-specs-move-by-move)
  - [M0 — Reliability prerequisites](#m0--reliability-prerequisites)
  - [M1 — Pluggable, persistent frontier backend](#m1--pluggable-persistent-frontier-backend)
  - [M2 — Priority scheduling (activate the dormant field)](#m2--priority-scheduling-activate-the-dormant-field)
  - [M3 — Declarative scoring (DSL surface)](#m3--declarative-scoring-dsl-surface)
  - [M4 — Link-provenance scoring ("structural foresight")](#m4--link-provenance-scoring-structural-foresight)
  - [M5 — The hybrid wedge (pluggable semantic scorer)](#m5--the-hybrid-wedge-pluggable-semantic-scorer)
  - [M6 — Canonicalization & content-fingerprint dedup](#m6--canonicalization--content-fingerprint-dedup)
  - [M7 — Host-aware frontier scheduling](#m7--host-aware-frontier-scheduling)
  - [M8 — Pluggable fetcher (the reach play)](#m8--pluggable-fetcher-the-reach-play)
- [New invariants this roadmap introduces](#new-invariants-this-roadmap-introduces)
- [What we deliberately will not do](#what-we-deliberately-will-not-do)

---

## Thesis: stop calling it "BFS"

Today wxpath's core principle is described as *"a FIFO queue expanded in BFS-ish
order"* ([DESIGN.md](DESIGN.md) "Mental Model"; [engine.py](src/wxpath/core/runtime/engine.py)
`WXPathEngine` docstring). "Naive in-process BFS" is a **liability label** in a
competitive setting. The same machinery, reframed, is a **product**:

> **A programmable, persistent, priority-driven frontier — i.e. focused crawling
> as a first-class primitive — where BFS is merely the *default* policy.**

The competitive wedge (see [COMPARISONS.md](COMPARISONS.md)) is **not** to out-*search*
Exa/Parallel. It is to own the lane they don't: **deterministic, resumable,
relevance-steered, exhaustive *mapping* of the sites you point it at.** The
frontier *is* the product. This roadmap turns the frontier from a hardcoded
in-memory `asyncio.Queue` into a programmable scheduler — without sacrificing the
determinism, sovereignty, and unit-economics advantages that are the whole moat.

Positioning, one line:

> *Exa finds the data, Parallel researches it — **wxpath is the programmable,
> deterministic, resumable harvester that maps the sites you target,
> best-data-first, and never repeats work.***

---

## Current architecture (verified understanding)

Everything below is read directly from source, so the design specs sit on solid
ground.

### The frontier lives entirely inside one function call

The whole scheduler is local state inside `WXPathEngine.run()`
([engine.py](src/wxpath/core/runtime/engine.py)):

```python
queue: asyncio.Queue[CrawlTask] = asyncio.Queue()   # the frontier — FIFO, in-memory
inflight: dict[str, CrawlTask]  = {}                 # URLs submitted, awaiting response
pending_tasks = 0                                    # for the terminal condition
# on the engine instance:
self.seen_urls: set[str] = set()                     # dedup set — raw URL strings
```

- A background `submitter()` task pulls `CrawlTask`s off `queue`, skips any
  `task.url in self.seen_urls or task.url in inflight`, marks them seen, and hands
  them to the crawler.
- Responses come back via `async for resp in crawler`; each parsed page is run
  through `_process_pipeline`, which emits `Intent`s.
- New links become `CrawlIntent`s; the engine enqueues them in
  `_process_pipeline`:

```python
elif isinstance(intent, CrawlIntent):
    next_depth = task.depth + 1
    if next_depth <= max_depth and intent.url not in self.seen_urls:
        queue.put_nowait(CrawlTask(url=intent.url, segments=intent.next_segments,
                                   depth=next_depth, backlink=task.url, elem=None))
```

This `put_nowait(...)` call site is **the single chokepoint** where ordering,
priority, scoring, provenance, and canonicalization all have to plug in. Almost
every move below touches exactly this line.

### The data model is already half-built for priority

[models.py](src/wxpath/core/models.py) `CrawlTask` **already has a priority field —
and it is dead code:**

```python
@dataclass(slots=True)
class CrawlTask:
    ...
    priority: int = field(default=0)   # "lower number = higher priority"
    def __post_init__(self):
        self.priority = self.depth     # auto-sync → pure BFS
    def __lt__(self, other):
        return self.priority < other.priority   # ready for a heap...
```

`__lt__` is defined precisely so `CrawlTask`s can live in a priority structure —
but `run()` uses a plain `asyncio.Queue` (FIFO), so **`priority` and `__lt__` are
never consulted.** The author left a comment: *"Useful if you want Depth-First
behavior in a shared queue."* **M2 simply makes the engine honor a field the model
already exposes.**

`CrawlIntent` ([models.py](src/wxpath/core/models.py)) is minimal — it is the thing
that needs new fields for scoring/provenance:

```python
@dataclass(slots=True)
class CrawlIntent(Intent):
    url: str             # "I found this link"
    next_segments: list  # "what to do if you go there"
    # ← M3 adds: score/priority   ← M4 adds: provenance
```

### Link discovery already holds the provenance we want — then throws it away

In [ops.py](src/wxpath/core/ops.py), the `url()` operators discover links via:

```python
urls = get_absolute_links_from_elem_and_xpath(curr_elem, _path_exp)
urls = dict.fromkeys(urls)          # dedup, order-preserving
for url in urls:
    yield CrawlIntent(url=url, next_segments=next_segments)
```

`curr_elem` is the **parent DOM element**. At this exact moment we have the anchor
node, its text, its ancestry, its siblings — full provenance — but we collapse it
to a bare URL string and discard the context. **M4 captures it instead of
discarding it.**

### Politeness is already host-aware (at the throttle layer)

[throttler.py](src/wxpath/http/policy/throttler.py) is an ABC (`AbstractThrottler`)
with `AutoThrottler` / `SimpleThrottler` / `ImpoliteThrottle`, all keyed
`defaultdict(host)`. So per-host pacing exists — but the **frontier ordering** is
host-blind. **M7** makes the scheduler cooperate with the throttler instead of
fighting it.

### There is a backend-factory pattern to copy verbatim

[cache.py](src/wxpath/http/client/cache.py) `get_cache_backend()` is a clean factory
switched on a settings key:

```python
if CACHE_SETTINGS.backend == "redis":  return RedisBackend(...)
elif CACHE_SETTINGS.backend == "sqlite": return SQLiteBackend(...)
```

**M1's `get_frontier_backend()` mirrors this one-to-one** — same shape, same
settings ergonomics, `in_memory` / `sqlite` / `redis`. This is the strongest
signal that a pluggable frontier is *idiomatic* for this codebase, not a foreign
graft.

### Lifecycle owns its own event loop

`wxpath_async_blocking_iter` ([engine.py](src/wxpath/core/runtime/engine.py)) creates
and tears down its own loop, and the `run()` `finally` cancels the `submitter()`
task on early close. Persistence + resume (M1) and any checkpoint semantics must
respect this lifecycle — **M0** hardens it first.

---

## The gaps, mapped to moves

| # | Gap in today's engine | Evidence | Move |
|---|---|---|---|
| 1 | Frontier is in-memory only → no resume, lost on crash | `queue = asyncio.Queue()` local to `run()` | **M1** |
| 2 | `seen_urls` grows unbounded | `# Will grow unbounded...` TODO on `self.seen_urls` | **M1 / M6** |
| 3 | `priority` / `__lt__` exist but are ignored | FIFO `asyncio.Queue`, not `PriorityQueue` | **M2** |
| 4 | No way to *express* "crawl X before Y" | grammar has `url('...', follow=...)` but no score | **M3** |
| 5 | Link context discarded at discovery | `dict.fromkeys(urls)` drops the anchor node | **M4** |
| 6 | No relevance steering / no optional "intelligence" | deterministic-only; no scorer hook | **M5** |
| 7 | Dedup is raw-string; UTM/session/pagination re-fetched | `task.url in self.seen_urls` | **M6** |
| 8 | Frontier ordering is host-blind | throttler is per-host, queue is not | **M7** |
| 9 | Dead-on-arrival at JS / anti-bot walls | single `aiohttp` fetch path in crawler | **M8** |

---

## The roadmap (sequence & dependency graph)

Build order is leverage-first **and** dependency-correct. M1+M2+M3 are the
commercial unlock; everything else is enhancement that hangs off the M1 interface.

```
            ┌─────────────────────────── PHASE 1: FOUNDATION ──────────────────────────┐
M0 ───────▶ M1  Pluggable / persistent frontier backend  (the interface everything uses)
(reliability)│
             ├─▶ M2  Priority scheduling (activate CrawlTask.priority)
             │     └─▶ M3  Declarative scoring DSL  (priority=… in the grammar)
             │            ├─▶ M4  Link-provenance scoring  (structural foresight)
             │            └─▶ M5  Hybrid wedge  (pluggable semantic scorer)
             ├─▶ M6  Canonicalization + content-fingerprint dedup   (independent after M1)
             └─▶ M7  Host-aware frontier scheduling   (needs M1 + M2)

M8  Pluggable fetcher  ───────────────────────── (independent of the frontier entirely)
```

| Phase | Move | Title | Leverage | Depends on |
|---|---|---|---|---|
| 0 | **M0** | Reliability prerequisites | unblocks persistence | — |
| 1 | **M1** | Pluggable / persistent frontier backend | ★★★ unlock | M0 |
| 2 | **M2** | Priority scheduling | ★★★ unlock | M1 |
| 2 | **M3** | Declarative scoring (DSL) | ★★★ differentiator | M2 |
| 2 | **M4** | Link-provenance scoring | ★★ unique IP | M3 |
| 2 | **M5** | Hybrid wedge (semantic scorer) | ★★ category bridge | M2/M3 |
| 3 | **M6** | Canonicalization + fingerprint dedup | ★★ cost/efficiency | M1 |
| 4 | **M7** | Host-aware scheduling | ★ scale/politeness | M1, M2 |
| 4 | **M8** | Pluggable fetcher | ★ reach | — |

**The two-move minimum viable story** is M1 + M2: a frontier you can persist,
resume, and order by priority. M3 makes that ordering *programmable*; M4 makes it
*smart*; M5 makes it optionally *semantic*. If we only ever ship M1–M3, wxpath is
already a "deterministic focused crawler" no competitor offers self-hosted.

---

## Design specs, move-by-move

Each spec is written so it can be detached into its own ticket. Common template:
**Problem → Design → Touchpoints → Determinism → Acceptance → Risks.**

---

### M0 — Reliability prerequisites

**Problem.** Persistence and resume (M1) are meaningless on top of an engine that
can crash on teardown. Two known issues were flagged during benchmarking:
1. `RuntimeError: Event loop is closed` on an **early-aborted** crawl (consumer
   stops iterating because a `max_urls` budget tripped, while `submitter()` is
   still awaiting `queue.get()`). The `run()` `finally` and
   `wxpath_async_blocking_iter` `finally` already contain mitigations — **M0
   verifies the abort path end-to-end and adds a regression test.**
2. Per-URL latency pairing in the benchmark `BenchHook` (fetch→parse paired via
   `ctx.user_data`, but the engine builds a fresh `FetchContext` per hook phase →
   p50/p95/p99 stuck at `<pending>`).

**Design.** No new architecture — make the existing teardown deterministic and
covered. Add a `tests/` case that seeds a frontier larger than `max_urls`, aborts
mid-crawl, and asserts clean shutdown (no warning, no loop error). Fix the
`FetchContext` identity so a single logical fetch carries one context across
phases (or thread a correlation id).

**Touchpoints.** [engine.py](src/wxpath/core/runtime/engine.py) `run()` `finally`
+ `wxpath_async_blocking_iter` `finally`; `hooks/registry.py` `FetchContext`;
`benchmarks/collectors.py` `BenchHook`.

**Acceptance.** Abort-mid-crawl test is green and deterministic; benchmark card
shows real latency quantiles instead of `<pending>`.

**Risks.** Low. This is hardening, not redesign.

---

### M1 — Pluggable, persistent frontier backend

> 📋 **Detailed implementation plan:** [design/M1-pluggable-frontier.md](design/M1-pluggable-frontier.md)
> (branch `feat/m1-pluggable-frontier`) — finalized interface, SQLite schema, exact
> engine edits, tests, and rollout checklist.

**Problem.** The frontier (`queue`), the seen-set (`self.seen_urls`), and
`inflight` are all in-memory and local. Consequences: (a) a crash loses the entire
crawl, (b) `seen_urls` grows unbounded (the code's own TODO), (c) no horizontal
scale — one process owns the whole graph. Managed competitors checkpoint and
resume as table stakes; wxpath cannot pause/resume at all.

**Design.** Extract the frontier behind a small interface and provide three
implementations, selected by a settings key — **mirroring
[`get_cache_backend()`](src/wxpath/http/client/cache.py) exactly.**

```python
# src/wxpath/http/frontier/base.py
from typing import Protocol, Optional
from wxpath.core.models import CrawlTask

class Frontier(Protocol):
    async def push(self, task: CrawlTask) -> None: ...
    async def pop(self) -> Optional[CrawlTask]: ...   # highest priority; None when drained
    async def seen(self, url: str) -> bool: ...        # dedup membership (absorbs seen_urls)
    async def mark_seen(self, url: str) -> None: ...
    async def size(self) -> int: ...                   # frontier depth (for terminal check)
    async def checkpoint(self) -> None: ...            # durability barrier (no-op in memory)
    async def close(self) -> None: ...

# src/wxpath/http/frontier/__init__.py — the factory (copy of cache.py shape)
def get_frontier_backend() -> Frontier:
    backend = SETTINGS.http.client.frontier.backend
    if backend == "memory": return InMemoryFrontier()          # default — today's behavior
    if backend == "sqlite": return SQLiteFrontier(...)         # single-node, resumable
    if backend == "redis":  return RedisFrontier(...)          # distributed, shared frontier
    raise ValueError(f"Unknown frontier backend: {backend}")
```

- **`InMemoryFrontier`** wraps the current `asyncio.Queue` + `set` → **zero
  behavior change**, proves the abstraction is faithful (M1 ships dark).
- **`SQLiteFrontier`** (WAL mode): one table, indexed for the pop query. Enables
  resume-from-stop and bounds memory.

```sql
-- SQLiteFrontier schema (WAL)
CREATE TABLE frontier (
    url            TEXT PRIMARY KEY,        -- canonical URL (also serves dedup)
    state          TEXT NOT NULL,           -- 'queued' | 'inflight' | 'done'
    priority       REAL NOT NULL DEFAULT 0, -- M2 consumes this
    depth          INTEGER NOT NULL,
    discovery_time INTEGER NOT NULL,        -- tie-break (monotonic counter, NOT wall clock)
    backlink       TEXT,
    segments       BLOB,                    -- pickled next_segments
    provenance     BLOB                     -- M4 (nullable)
);
CREATE INDEX ix_pop ON frontier (state, priority DESC, discovery_time ASC);
```

`pop()` = `SELECT ... WHERE state='queued' ORDER BY priority DESC, discovery_time
ASC LIMIT 1` then flip to `inflight` (single transaction). `seen()`/`mark_seen()`
collapse into the `url` primary key + `state`. The `inflight` dict and
`pending_tasks` counter become queries over `state`, preserving `is_terminal()`.

- **`RedisFrontier`** uses a sorted set (`ZADD`/`ZPOPMAX` keyed by priority) + a
  `seen` set; the **same interface** makes single-node→distributed a config flip.

**Settings.** New block, same nesting as `http.client.cache`:

```yaml
http.client.frontier.backend: memory   # memory | sqlite | redis
http.client.frontier.sqlite.path: ./.wxpath_frontier.db
http.client.frontier.resume: false     # if true, reattach to an existing frontier table
```

**Touchpoints.** [engine.py](src/wxpath/core/runtime/engine.py): replace the three
local structures with `frontier = get_frontier_backend()`; `submitter()` calls
`await frontier.pop()` / `seen()` / `mark_seen()`; the `put_nowait` chokepoint
becomes `await frontier.push(...)`; `is_terminal()` becomes
`await frontier.size() == 0 and pending == 0`. **No DSL change. No ops change.**

**Determinism.** `discovery_time` is a **monotonic counter**, never wall-clock
(the engine already bans `Date.now()`-style nondeterminism). With `backend:
memory` and equal priorities, pop order is identical to today's FIFO →
bit-stable, so the existing `benchmarks/` reproducibility gate still passes
unchanged. This is the regression contract for M1.

**Acceptance.** (1) `local_fixture` benchmark yields byte-identical
coverage/extraction with `backend: memory`. (2) With `backend: sqlite`, kill the
process at N pages, restart with `resume: true`, and the crawl completes the
*same* total page set with no re-fetches. (3) `seen_urls` no longer lives on the
engine instance.

**Risks.** SQLite write throughput on a hot loopback crawl (mitigate: WAL +
batched `push`); pickled `segments` versioning (store a schema tag).

---

### M2 — Priority scheduling (activate the dormant field)

**Problem.** `CrawlTask.priority` and `CrawlTask.__lt__` exist
([models.py](src/wxpath/core/models.py)) but the FIFO queue ignores them. There is
no way to crawl high-value pages before low-value ones — every crawl is pure
breadth-first, which under a `max_urls`/wall-clock budget means **budget is spent
on whatever was discovered first, not on what matters.**

**Design.** Make the frontier a **priority** frontier. `InMemoryFrontier` swaps
`asyncio.Queue` → `asyncio.PriorityQueue` (or a heap) keyed by
`(-priority, discovery_time)`; `SQLiteFrontier`/`RedisFrontier` already order by
priority in M1. Default priority stays `= depth` ⇒ **ties reproduce BFS exactly**,
so M2 is a no-op until a score is supplied (by M3+). This is the bridge: M2 makes
priority *respected*; M3 makes it *settable*.

**Touchpoints.** [models.py](src/wxpath/core/models.py): keep `priority` but stop
hard-overwriting it in `__post_init__` (default to `depth` only when unset).
[engine.py](src/wxpath/core/runtime/engine.py): the `CrawlTask(...)` built at the
enqueue chokepoint passes a computed priority. Frontier `pop()` returns
highest-priority.

**Determinism.** With no scores, `priority == depth` and `discovery_time` breaks
ties → identical to BFS. Output data is **unaffected** regardless of order (see
[Invariant I4](#new-invariants-this-roadmap-introduces)); only *coverage order
under a budget* changes.

**Acceptance.** Unit test: hand-seed three tasks with priorities (5, 1, 9) at the
same depth; assert pop order 9→5→1. Benchmark with no scoring unchanged.

**Risks.** Low — the model and `__lt__` were built for this.

---

### M3 — Declarative scoring (DSL surface)

**Problem.** M2 gives a priority *channel* but nothing fills it. We need to
**express** relevance in the same declarative language as everything else —
otherwise "focused crawling" requires Python, breaking the DSL promise.

**Design.** Extend the `url()` argument grammar with an optional **`priority=`**
keyword, parallel to the existing `follow=`/`depth` arguments already parsed in
[ops.py](src/wxpath/core/ops.py) (`@register('url', (String, Xpath, Depth))` etc.).
The value is a constant or an **XPath expression evaluated per discovered link, in
the context of the anchor node**:

```
(: static weights — footer links are cheap, product links are gold :)
url('https://shop.example/')
    /// url( //a[contains(@class,'product')]/@href, priority=10 )

(: dynamic, per-anchor score from the DOM :)
url('https://shop.example/')
    /// url( //a/@href, priority=number(./@data-rank) )

(: combine with extraction as usual — scoring only steers the frontier :)
url('https://news.example/')
    /// url( //a/@href, priority=count(./ancestor::article) )
        / map { 'url': wx:current-url(), 'title': (//h1/text())[1] }
```

Grammar addition (extends [DESIGN.md](DESIGN.md) ruleset):

```
UrlEvalSegment    := /url(XPathExprToURLs ScoreArg?) | //url(XPathExprToURLs ScoreArg?)
UrlRecurseSegment := ///url(XPathExprToURLs ScoreArg?)
ScoreArg          := ',' 'priority' '=' (Number | XPathExpr)
```

This requires a new AST node `Score` (sibling of `Depth` in
[parser.py](src/wxpath/core/parser.py)) and new `@register('url', (..., Score))`
overloads in [ops.py](src/wxpath/core/ops.py). Critically, **per-anchor dynamic
scoring needs the node, not just the URL** — so
`get_absolute_links_from_elem_and_xpath` (in [dom.py](src/wxpath/core/dom.py)) must
return `(url, anchor_node)` pairs (or a parallel call), and the op evaluates the
`priority` XPath against each `anchor_node` to populate `CrawlIntent.score`.

```python
@dataclass(slots=True)
class CrawlIntent(Intent):
    url: str
    next_segments: list
    score: float = 0.0          # ← M3
    provenance: "Provenance | None" = None   # ← M4
```

The engine maps `intent.score → CrawlTask.priority` at the enqueue chokepoint.

**Touchpoints.** [parser.py](src/wxpath/core/parser.py) (new `Score` node + grammar),
[ops.py](src/wxpath/core/ops.py) (`Score` overloads, score evaluation per anchor),
[dom.py](src/wxpath/core/dom.py) (return anchor nodes), [models.py](src/wxpath/core/models.py)
(`CrawlIntent.score`), [engine.py](src/wxpath/core/runtime/engine.py) (score→priority).

**Determinism.** A given page + given expression yields identical scores ⇒
identical priorities ⇒ identical order. Fully reproducible. Document this as a
new invariant.

**Acceptance.** `///url(//a/@href, priority=...)` parses; on a fixture where
anchors carry `@data-rank`, assert the high-rank pages are popped (and thus
fetched) before low-rank ones under a tight `max_urls`.

**Risks.** Grammar ambiguity with the existing `follow=`/positional-xpath rewrite
rules (DESIGN.md "Open for discussion") — spec the parser tests carefully.

---

### M4 — Link-provenance scoring ("structural foresight")

**Problem.** The single most-missed idea: today wxpath scores a link by *its own
URL/anchor* only. But the **provenance** of a link — where it sat in the parent
DOM — is a strong, *cheap, deterministic* predictor of its value, and wxpath is
uniquely positioned to use it because at discovery time it **has the parent tree**
(`curr_elem` in [ops.py](src/wxpath/core/ops.py)). We currently throw that context
away with `dict.fromkeys(urls)`.

**Design.** At link discovery, capture a `Provenance` record per link instead of
discarding the node:

```python
@dataclass(slots=True)
class Provenance:
    anchor_text:     str           # visible text of the <a>
    parent_tag:      str           # 'nav' | 'main' | 'article' | 'footer' | 'aside' ...
    ancestor_path:   tuple[str]    # ('html','body','main','div','a')
    link_density:    float         # links / text-length in the containing block
    dom_depth:       int           # node depth in the page tree
    discovered_at:   str           # backlink URL
    discovered_depth: int          # crawl depth where found
```

Two payoffs, both deterministic:

1. **Provenance-aware scoring.** Expose provenance to the M3 scoring expression via
   new `wx:` functions (siblings of the existing `wx:depth()`, `wx:backlink()` in
   [patches.py](src/wxpath/patches.py)): `wx:link-density()`, `wx:parent-tag()`,
   `wx:anchor-text()`, `wx:ancestor-path()`. Then "promote links inside `<main>`,
   demote boilerplate" is a one-line DSL predicate:

   ```
   /// url( //a/@href,
            priority = (if (wx:parent-tag()='main') then 10 else 1)
                       - wx:link-density() )
   ```

2. **Deterministic trap/loop detection.** A frontier-level rule drops links whose
   `ancestor_path` pattern (or URL path) repeats beyond a threshold — the classic
   link-farm / infinite-calendar / `/archive/.../archive/...` recursion. This is a
   pure structural rule (no ML), implemented as a frontier admission filter:

   ```yaml
   http.client.frontier.trap.max_path_repeat: 3   # drop /a/b/a/b/a/... after 3 repeats
   http.client.frontier.trap.max_link_density: 0.95
   ```

**Touchpoints.** [dom.py](src/wxpath/core/dom.py) (build `Provenance` while
resolving links), [models.py](src/wxpath/core/models.py) (`CrawlIntent.provenance`,
carried onto `CrawlTask`), [patches.py](src/wxpath/patches.py) (new `wx:` functions),
[ops.py](src/wxpath/core/ops.py) (thread provenance through), frontier admission
filter (M1 backend).

**Determinism.** All signals are computed from the served HTML — identical input ⇒
identical provenance ⇒ identical scores/drops. No network, no model, no clock.

**Acceptance.** On a fixture containing a `<main>` product grid + a `<footer>` link
block + a synthetic `/loop/1→/loop/2→…` trap: assert (a) main-grid links outrank
footer links, (b) the trap is pruned at the configured repeat threshold while a
legitimate deep chain of equal length is **not**.

**Risks.** `link_density` cost on large pages (compute lazily, once per block);
provenance blob size in SQLite (store compactly / make nullable).

---

### M5 — The hybrid wedge (pluggable semantic scorer)

**Problem.** wxpath's moat is *determinism* (see [COMPARISONS.md](COMPARISONS.md)).
But "relevance-guided discovery" is exactly where Exa/Parallel claim superiority.
We want to compete on relevance **without** surrendering deterministic output.

**The wedge — the key insight:** a scorer changes **traversal order only, never
extracted data.** So scoring is the *one* place we can inject optional
"intelligence" with zero cost to the determinism guarantee. Order affects *which
pages get crawled first under a budget*; it never affects *what is extracted from a
page*. That asymmetry is the whole trick.

**Design.** A pluggable `FrontierScorer` strategy; the M3/M4 deterministic path is
the default implementation, a semantic scorer is an opt-in plugin:

```python
# src/wxpath/http/frontier/scoring.py
class FrontierScorer(Protocol):
    def score(self, intent: CrawlIntent) -> float: ...   # higher = crawl sooner

class DeterministicScorer:                 # DEFAULT — M3 priority + M4 provenance
    def score(self, intent): return intent.score

class SemanticScorer:                      # OPT-IN — relevance to a target objective
    def __init__(self, objective: str, embedder): ...
    def score(self, intent):
        # cosine(embed(anchor_text + url + provenance), embed(objective))
        ...
```

Selection via settings + an optional DSL objective:

```yaml
http.client.frontier.scorer: deterministic   # deterministic | semantic
http.client.frontier.objective: "lithium battery recycling suppliers"
```

```
(: "focused crawl toward an objective" — semantic ordering, deterministic output :)
url('https://directory.example/')
    /// url( //a/@href )
        / map { 'url': wx:current-url(), 'text': wx:main-article-text() }
```

The extracted `map{...}` is **byte-identical** regardless of scorer; only the
*order pages are visited* (and thus which survive a `max_urls` budget) differs.

**Touchpoints.** New `src/wxpath/http/frontier/scoring.py`; frontier `push()` calls
`scorer.score(intent)` to set priority; settings block; embedder is an injected
dependency (no hard ML dep in core — ships as an extra, e.g.
`pip install wxpath[semantic]`).

**Determinism.** **Output determinism is preserved by construction** (Invariant
I4). *Coverage* under a budget becomes scorer-dependent — so when the semantic
scorer is active, runs are deterministic given a pinned embedder but not
guaranteed across embedder versions. Document this sharply: the benchmark's
reproducibility gate runs `scorer: deterministic`.

**Acceptance.** With `scorer: semantic` + an objective, assert pages whose anchor
text matches the objective are crawled before off-topic pages under a tight
budget, **and** that the extracted records for any commonly-visited page are
identical to a deterministic run (proving order ≠ data).

**Risks.** Embedding latency per link (batch at the block level; cache by
anchor-text hash); scope creep — keep core ML-free, semantic scorer is strictly an
optional extra. This is a *bridge*, not the moat; resist making it mandatory.

---

### M6 — Canonicalization & content-fingerprint dedup

**Problem.** Dedup is `task.url in self.seen_urls` — **raw string** equality.
`?utm_source=…`, session ids, trailing slashes, and pagination variants all read
as distinct URLs ⇒ wasted fetches and duplicate records. (We saw the hint live: a
crawl reporting more pages than unique pages.)

**Design.** Two layers, cheap-first:

1. **URL canonicalization (high ROI, do first).** Normalize before the dedup
   check: lowercase host, strip default ports, sort/strip tracking params
   (`utm_*`, `gclid`, `fbclid`, configurable), drop fragments, normalize trailing
   slash. The canonical form is what M1's frontier stores as the `url` primary key.

   ```yaml
   http.client.frontier.canonical.strip_params: [utm_*, gclid, fbclid, sessionid]
   http.client.frontier.canonical.drop_fragment: true
   http.client.frontier.canonical.normalize_trailing_slash: true
   ```

2. **Content fingerprint (advanced).** After parse, compute a SimHash/shingle
   fingerprint of the main content; maintain a fingerprint registry in the frontier
   backend. Near-duplicate pages (mirrored content under different URLs, print
   views, pagination chrome) are recorded once. Threshold-configurable; off by
   default.

   ```yaml
   http.client.frontier.fingerprint.enabled: false
   http.client.frontier.fingerprint.hamming_threshold: 3
   ```

**Touchpoints.** New `src/wxpath/http/frontier/canonical.py`; frontier
`seen()`/`mark_seen()`/`push()` operate on canonical URLs; fingerprint hook after
`parse_html` in [engine.py](src/wxpath/core/runtime/engine.py) (or as a
`post_parse` hook). The existing benchmark `wx:main-article-text()`
([patches.py](src/wxpath/patches.py)) is the content source for the fingerprint.

**Determinism.** Canonicalization is a pure function of the URL; SimHash is a pure
function of content. Both reproducible. Expected-count assertions in
`benchmarks/server.py` corpus may need updating if canonicalization collapses any
fixture URLs (the fixture currently uses clean root-relative hrefs, so likely no
change — verify).

**Acceptance.** Seed a fixture page linking to `/p/1`, `/p/1?utm_source=x`,
`/p/1#section`, `/p/1/`: assert exactly **one** fetch. With fingerprinting on, two
distinct URLs serving identical bodies record once.

**Risks.** Over-aggressive param stripping breaking sites where a query param is
semantic (make the strip-list explicit, never strip unknown params by default).

---

### M7 — Host-aware frontier scheduling

**Problem.** The throttler is per-host
([throttler.py](src/wxpath/http/policy/throttler.py)) but the frontier is host-blind.
On a multi-host crawl, BFS can stack thousands of same-host URLs back-to-back,
forcing the throttler to serialize them while other hosts sit idle — poor
politeness *and* poor throughput.

**Design.** Make the frontier **host-partitioned**: per-FQDN sub-queues with a
round-robin/weighted pop across hosts, each host carrying a budget and inheriting
the throttler's pace. `pop()` returns the highest-priority task from a host that
is *not currently rate-limited*, spreading concurrency across the host set.

```yaml
http.client.frontier.per_host_budget: 5000     # max queued per FQDN
http.client.frontier.host_fairness: round_robin  # round_robin | weighted | strict_priority
```

In SQL terms this is an extension of M1's pop query: partition by host, pick the
best eligible task whose host is below its in-flight cap (the per-host cap already
exists at the crawler level — M7 makes the *frontier* aware of it so it stops
handing the crawler work it will only have to serialize).

**Touchpoints.** Frontier backend `pop()` (host-partitioned selection); read the
existing per-host concurrency from the crawler/throttler config; settings block.

**Determinism.** Round-robin over a sorted host set with monotonic tie-breaks is
deterministic. Document the host-iteration order.

**Acceptance.** Crawl a 3-host fixture; assert in-flight requests are spread across
hosts (not 32 to one host, 0 to the others) and total wall-clock drops versus the
host-blind baseline.

**Risks.** Interaction with M2 priority (a global high-priority task on a
throttled host) — define the policy explicitly via `host_fairness`.

---

### M8 — Pluggable fetcher (the reach play)

**Problem.** The crawler has a single `aiohttp` fetch path. Against JS-rendered or
anti-bot-walled sites, wxpath returns empty/blocked — **dead on arrival**, the one
honest gap vs managed services that run headless fleets
([COMPARISONS.md](COMPARISONS.md)).

**Design.** **Do not build anti-bot infrastructure** (we cannot win that arms race,
and owning it contradicts the self-hosted/deterministic positioning). Instead make
the *fetch step* an interface so users drop in their own renderer/proxy:

```python
class Fetcher(Protocol):
    async def fetch(self, request: Request) -> Response: ...

# default: AiohttpFetcher (today's behavior, extracted from Crawler)
# opt-in:  PlaywrightFetcher, ProxyFetcher, ScrapingServiceFetcher — user-supplied extras
```

Selected by settings, same factory pattern as M1. The BFS/scoring/extraction core
is untouched — only how a single page's bytes are obtained changes.

**Touchpoints.** Refactor [crawler.py](src/wxpath/http/client/crawler.py) to delegate
the actual GET to a `Fetcher`; factory `get_fetcher()`; settings
`http.client.fetcher.backend`.

**Determinism.** The default `AiohttpFetcher` keeps loopback determinism. Headless
fetchers are inherently nondeterministic (JS, time) — out of scope for the
reproducibility gate; clearly labeled as a field-only capability.

**Acceptance.** Default fetcher passes all existing tests unchanged; a stub
`Fetcher` injected in a test proves the seam (e.g. returns canned bytes for a URL
the aiohttp path could never reach).

**Risks.** Interface leakage (keep `Request`/`Response` as the only contract);
scope — ship *one* reference alt-fetcher at most, the rest are community extras.

---

## New invariants this roadmap introduces

To be appended to [DESIGN.md](DESIGN.md)'s invariant list once implemented:

```
I4 (THE WEDGE): A frontier scorer affects traversal ORDER only, never extracted
    DATA. For any page that is visited, its extracted output is identical
    regardless of priority/scoring/scorer. Scoring changes which pages survive a
    budget, not what any page yields.

I5: With backend=memory and no scores supplied, the frontier reproduces today's
    FIFO/BFS order exactly (regression contract — the benchmark gate depends on it).

I6: All default (non-semantic) scoring/dedup/provenance signals are pure functions
    of the served HTML and the URL — no wall-clock, no RNG, no network — so crawls
    remain bit-reproducible. discovery_time is a monotonic counter, never a clock.

I7: The frontier backend is the single source of truth for {queued, inflight, done,
    seen}. The engine holds no separate dedup set (seen_urls is retired).
```

## What we deliberately will not do

| Temptation | Why not |
|---|---|
| Build a neural search index | That is Exa/Parallel's capital-intensive moat; we'd lose. We index *the sites you point at*, not the web. |
| Own anti-bot / CAPTCHA evasion | Unwinnable arms race; contradicts deterministic/self-hosted positioning. Make it **pluggable** (M8) instead. |
| Make the semantic scorer mandatory | It would compromise the determinism moat. It is strictly an opt-in extra (M5). |
| Abandon BFS as the default | BFS is the *safe, reproducible* default. We add policies *on top*; we don't replace the floor. |
| Add ML to core dependencies | Core stays deterministic & dependency-light. Intelligence ships as `wxpath[semantic]`. |

---

*This roadmap is the plan of record for evolving wxpath's frontier. Pick a Move,
detach it into a ticket using the per-move template above, implement, and check
its Acceptance criteria before moving on. M1 → M2 → M3 is the critical path.*
