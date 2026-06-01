# M4b — Deterministic Trap / Loop Detection · Implementation Plan

> **Branch:** `feat/m4b-trap-detection` (off `feat/m4-link-provenance-scoring`)
> **Parent:** [FRONTIER_ROADMAP.md §M4](../FRONTIER_ROADMAP.md#m4--link-provenance-scoring-structural-foresight)
> **Sibling:** [M4-link-provenance.md §3](./M4-link-provenance.md) specced M4b at design
> level; this is the build plan. M4a (provenance scoring) shipped in `31fbc44`.

---

## 0. Scope decision (confirmed with user 2026-05-31)

M4's §3 bundled **two** independent admission filters. They have very different
blast radii, so this pass ships only the first:

- **URL-path-repeat trap filter (THIS PASS).** A pure function of the URL string
  drops links whose path contains a short segment-cycle repeated beyond a
  threshold (`/a/b/a/b/a/b/…`, `/next/next/next/…`, calendar/pagination loops).
  Lives entirely in a **composable frontier wrapper** — **zero edits to the
  memory/sqlite/redis backends**, settings-gated, **off by default** so the
  default crawl stays byte-identical (I5). Deterministic, no DOM, no carry.

- **Link-density admission (DEFERRED).** Dropping links from near-pure-link blocks
  needs a per-link density float from the anchor's DOM. The anchor exists cleanly
  only in ops.py's *dynamic-scored* discovery path; the unscored BFS path uses a
  different discovery function (`xpath3` + empty-filter) whose URL **sequence** is
  not provably identical to the anchored one (`lxml` native + `.getparent()`).
  Threading density into the unscored path risks reordering the I5 hot path for
  marginal benefit — and **M4a already covers the boilerplate case via scoring**
  (`priority = … - ./wx:link-density()` *demotes* dense blocks). Density-*drop* can
  be its own milestone if a need emerges. See §6.

---

## 1. Why a wrapper, not a `push()` edit per backend

M4's §3 sketch said "an admission filter invoked in `push()`". Implementing that
literally means editing **three** backends (`memory`/`sqlite`/`redis`), duplicating
the filter call and plumbing settings into each. Instead we exploit that `Frontier`
is a `Protocol`: a **decorator frontier** implements the same interface, applies the
filter in its own `push()`, and delegates every other method to the wrapped backend.

```
get_frontier_backend()
  └─ InMemoryFrontier()          ← unchanged
     wrapped by ──────────────►  TrapFilterFrontier(inner)   ← only when trap.enabled
```

Benefits: backends stay untouched; one filter call-site; composes with **any**
present or future backend; and when `trap.enabled is False` the factory returns the
bare backend, so the default path is *literally the same object graph as today* →
I5 byte-identical with zero overhead.

Returning `False` from `push()` for a trap reuses the existing dedup contract: the
engine already treats a `False` push as "not enqueued" (no progress-bar increment),
so no engine change is needed.

---

## 2. Trap signal — consecutive segment-cycle repeats (`frontier/trap.py`)

**Definition.** Split the URL *path* into non-empty segments. A URL is a trap when
some cycle of length `p` (`1 ≤ p ≤ max_period`) repeats **consecutively** more than
`max_path_repeat` times anywhere in the segment list.

- `/a/b/a/b/a/b/a/b` → block `a/b` (p=2) repeats 4× → trap when `max_path_repeat=3`.
- `/next/next/next/next` → block `next` (p=1) repeats 4× → trap.
- `/docs/guide/intro/setup/install` → all segments distinct → max run 1 → **kept**
  (a legitimate deep chain of equal length is never pruned — the §5 acceptance).

Pure function, deterministic, no clock/RNG/network → fits the I6 family.

```python
def _segments(url: str) -> list[str]:
    # path only; query/fragment ignored (cycle traps live in the path)
    path = urllib.parse.urlsplit(url).path
    return [s for s in path.split('/') if s]

def max_consecutive_cycle(segments: list[str], max_period: int) -> tuple[int, int]:
    """Return (period, repeats) of the longest consecutive block-repeat.

    repeats == 1 means "no block repeats" (every path is at least 1×).
    O(n²) over short paths — segment counts are tiny.
    """
    best = (1, 1)
    n = len(segments)
    for p in range(1, min(max_period, n // 2) + 1):
        i = 0
        while i + p <= n:
            block = segments[i:i + p]
            reps = 1
            j = i + p
            while j + p <= n and segments[j:j + p] == block:
                reps += 1
                j += p
            if reps > best[1]:
                best = (p, reps)
            # advance past this run start; +1 is enough for correctness
            i += 1
    return best

def is_path_trap(url: str, max_path_repeat: int = 3, max_period: int = 4) -> bool:
    if max_path_repeat < 1:        # 0/neg disables (defensive)
        return False
    _, reps = max_consecutive_cycle(_segments(url), max_period)
    return reps > max_path_repeat
```

**Why consecutive-only.** Non-consecutive segment recurrence (`/a/x/a/y/a/z`) is a
weaker, false-positive-prone signal (legit faceted nav repeats container segments).
Consecutive cycles are the precise, low-false-positive shape that infinite traps
take. Query-string traps (`?page=∞`, `?date=…`) are a separate signal, noted in §6.

---

## 3. The wrapper (`frontier/trap.py`, same module)

```python
class TrapFilterFrontier(Frontier):
    """Composable admission filter: drop URL-path-repeat traps at push().

    Wraps any Frontier backend, applies is_path_trap() in push(), and delegates
    pop/seen/size/mark_done/checkpoint/close unchanged. Constructed by
    get_frontier_backend() only when http.client.frontier.trap.enabled.
    """
    def __init__(self, inner, *, max_path_repeat=3, max_period=4):
        self._inner = inner
        self._max_path_repeat = max_path_repeat
        self._max_period = max_period
        self.dropped = 0          # observability: traps pruned this run

    async def push(self, task):
        if is_path_trap(task.url, self._max_path_repeat, self._max_period):
            self.dropped += 1
            log.debug("trap pruned", extra={"url": task.url})
            return False          # same contract as a dedup miss
        return await self._inner.push(task)

    async def pop(self):            return await self._inner.pop()
    async def mark_done(self, url): return await self._inner.mark_done(url)
    async def seen(self, url):      return await self._inner.seen(url)
    async def size(self):           return await self._inner.size()
    async def checkpoint(self):     return await self._inner.checkpoint()
    async def close(self):          return await self._inner.close()
```

No silent caps: `dropped` is incremented and each prune is `log.debug`'d so a steered
crawl can report how many links the filter removed.

---

## 4. Settings + factory

`settings.py` — add under `http.client.frontier`:

```python
'trap': {
    'enabled': False,        # off by default → I5 byte-identical default crawl
    'max_path_repeat': 3,    # consecutive cycle repeats allowed before dropping
    'max_period': 4,         # largest cycle length scanned for
},
```

`frontier/__init__.py` — wrap after constructing the backend:

```python
backend = <existing memory|sqlite|redis construction>
trap = FRONTIER_SETTINGS.trap
if trap.enabled:
    from wxpath.http.frontier.trap import TrapFilterFrontier
    backend = TrapFilterFrontier(
        backend, max_path_repeat=trap.max_path_repeat, max_period=trap.max_period)
return backend
```

Lazy import keeps `trap.py` out of the default-path import graph (mirrors the
backend lazy-imports already there).

---

## 5. Tests

`tests/core/frontier/test_trap.py` (pure logic):
- block cycles p=1..max_period detected at the threshold boundary
  (`reps == max_path_repeat` kept; `reps == max_path_repeat + 1` dropped);
- **legit deep chain of distinct segments (equal length) is NOT pruned**;
- query/fragment ignored; trailing slashes / empty segments handled;
- `max_path_repeat < 1` disables.

`tests/core/frontier/test_trap_frontier.py` (wrapper):
- a trap URL → `push` returns `False`, `dropped == 1`, inner never received it;
- a normal URL → delegated, `push` returns inner's result; dedup parity (second
  push of same normal URL → `False` from inner);
- `pop/seen/size/mark_done/close` delegate to inner.

`tests/core/frontier/test_factory.py` (or extend existing):
- `trap.enabled=False` ⇒ `get_frontier_backend()` returns the bare backend type
  (not wrapped);
- `trap.enabled=True` ⇒ returns a `TrapFilterFrontier` wrapping the configured
  backend.

`tests/core/runtime/test_engine.py` (e2e, inject `TrapFilterFrontier(InMemoryFrontier())`):
- a fixture whose root links into a `/loop/a/b/a/b/…` self-deepening trap **and** a
  parallel legitimate deep chain of the same length ⇒ the trap is pruned at the
  threshold while the legit chain is fully crawled.

I5: `trap.enabled` defaults `False`; full suite green; `wxpath-bench run
local_fixture` byte-identical to the M4a baseline
(`d0:1 d1:4 d2:16 d3:64 d4:256 d5:171`, 511 extractions).

---

## 6. Deferred / future

- **Link-density admission** — needs the ops→intent→task density carry + unscored
  hot-path change (§0). M4a scoring already demotes dense blocks; revisit only if
  *dropping* (vs demoting) proves necessary.
- **Query-string traps** (`?page=`, session-id explosions, calendar `?date=`) — a
  separate signal class (param-key recurrence / unbounded numeric params); the
  wrapper is the natural home when built.
- **`ancestor-path` trap input** — `wx:ancestor-path()` (shipped in M4a) could feed a
  DOM-structural trap signal; out of scope here (DOM, not URL).

---

## 7. File manifest

**Add**
- `src/wxpath/http/frontier/trap.py` — `is_path_trap`, `max_consecutive_cycle`,
  `TrapFilterFrontier`.
- `tests/core/frontier/test_trap.py`, `test_trap_frontier.py` (+ factory test).
- e2e case in `tests/core/runtime/test_engine.py`.

**Modify**
- `src/wxpath/settings.py` — `http.client.frontier.trap` block.
- `src/wxpath/http/frontier/__init__.py` — wrap when `trap.enabled`.

**No** change to backends, `CrawlTask`/`CrawlIntent`, ops, engine, or parser.

---

## 8. New invariant (append to DESIGN.md with M4b)

```
I (trap admission): URL-path-repeat trap detection is a pure function of the URL
   string and is applied as a composable frontier wrapper that is absent unless
   http.client.frontier.trap.enabled. With it disabled the frontier object graph
   is identical to pre-M4b, so default crawls remain byte-reproducible (extends I5).
```

---

## 9. Build order

1. `trap.py` pure functions + `test_trap.py` green.
2. `TrapFilterFrontier` + wrapper/factory tests; settings block.
3. e2e engine trap-vs-legit-chain test; full suite + `local_fixture` I5.
4. Append invariant to DESIGN.md; offer to commit.
