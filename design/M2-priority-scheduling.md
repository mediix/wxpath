# M2 — Priority Scheduling · Implementation Plan

> **Branch:** `feat/m2-priority-scheduling` (off `feat/m1-pluggable-frontier`)
> **Parent:** [FRONTIER_ROADMAP.md §M2](../FRONTIER_ROADMAP.md#m2--priority-scheduling-activate-the-dormant-field)
> **Depends on:** M1 (the pluggable frontier). M2 makes the frontier *honor*
> `CrawlTask.priority`; **M3** is what *sets* it (the `priority=` DSL). With no
> scoring source yet, M2 is a **dark-ship**: every task still gets `priority=depth`,
> so the crawl order is byte-identical to M1/BFS (Invariant I5).

---

## 1. Scope & non-goals

**In scope:** make every frontier backend order by `priority`, and let a
`CrawlTask` carry an explicit priority instead of always being forced to `depth`.

**Not in scope:** the DSL surface that produces priorities (**M3**), link-provenance
inputs (**M4**), semantic scoring (**M5**). M2 ships the *consumer* of priority; the
*producer* is M3. Until then nothing sets a non-default priority, so behavior is
unchanged.

---

## 2. The convention decision (load-bearing)

wxpath's data model already encodes a convention, and M2 commits to it rather than
inventing a new one:

> **Lower priority value = higher precedence (popped sooner).**

Evidence in [models.py](../src/wxpath/core/models.py): the comment *"lower number =
higher priority"* and `__lt__` returning `self.priority < other.priority` (min-first).
This is the **inverse** of the roadmap sketch's "higher = more important" (which came
from Gemini's external framing). We choose the existing convention because:

1. It is already coded in `__lt__` — no model rewrite.
2. **BFS falls out for free:** the default `priority = depth` means depth 0 (priority
   0) is popped before depth 1, etc. — exactly today's behavior. No `-depth` hacks.
3. The SQLite/heap ordering is a plain `ASC`, the natural default.

**Ergonomics note for M3:** "priority 0 is most important" can read awkwardly to end
users. M3's DSL will paper over this with friendlier surface syntax (e.g. a `boost`
that *subtracts* from priority, or documenting "lower = sooner"). The *engine*
convention stays `lower = sooner`; the *DSL* is free to present sugar. M2 does not
touch the DSL.

---

## 3. Changes (4 small edits)

### 3.1 `CrawlTask` — stop force-overwriting priority

[models.py](../src/wxpath/core/models.py): the field defaults to `None`; `__post_init__`
falls back to `depth` **only when unset**, so an explicit priority survives.

```python
priority: Optional[int] = field(default=None)

def __post_init__(self):
    # Default to depth (preserves BFS); honor an explicit priority if given.
    # Convention: lower value = popped sooner.
    if self.priority is None:
        self.priority = self.depth
```

`__lt__` and `__iter__` are unchanged. Every existing construction
(`CrawlTask(elem=None, url=..., depth=d, ...)`) omits `priority` → `None` → `depth`,
identical to before.

### 3.2 `InMemoryFrontier` — FIFO queue → priority queue

[memory.py](../src/wxpath/http/frontier/memory.py): swap `asyncio.Queue` for
`asyncio.PriorityQueue`, ordering on `(priority, seq)` where `seq` is a monotonic
insertion counter (FIFO tiebreak among equal priorities, and it keeps `CrawlTask`
out of the tuple comparison). `seq` is a plain incrementing int — deterministic, no
clock/RNG.

```python
self._queue = asyncio.PriorityQueue()   # items: (priority, seq, task)
self._seen: set[str] = set()
self._seq = 0
...
async def push(self, task):
    if task.url in self._seen: return False
    self._seen.add(task.url)
    self._queue.put_nowait((task.priority, self._seq, task))
    self._seq += 1
    return True

async def pop(self):
    if self._closed and self._queue.empty(): return None
    _prio, _seq, task = await self._queue.get()
    return task
```

### 3.3 `SQLiteFrontier` — order by priority, then seq

[sqlite.py](../src/wxpath/http/frontier/sqlite.py): pop's `ORDER BY` gains the priority
term; the covering index matches; bump `SCHEMA_VERSION` (the ordering/index changed,
so a stale M1 db is correctly rejected by the version gate).

```sql
-- index
CREATE INDEX IF NOT EXISTS ix_pop ON frontier (state, priority, discovery_seq);
-- pop
SELECT ... WHERE state='queued' ORDER BY priority ASC, discovery_seq ASC LIMIT 1
```

`SCHEMA_VERSION = 2`. (Resume across the M1→M2 upgrade is intentionally unsupported:
the order changed; the version gate raises on a v1 db so the user resets it.)

### 3.4 `RedisFrontier` — priority-major score

[redis.py](../src/wxpath/http/frontier/redis.py): encode priority into the sorted-set
score so `ZPOPMIN` yields lowest-priority-then-FIFO:

```python
score = float(task.priority) * 1e12 + seq   # priority-major, seq tiebreak
await self._r.zadd(self._k("queued"), {task.url: score})
```

(`seq` < 1e12 for any realistic crawl; negative priorities still order correctly.)

---

## 4. Determinism & I5

With `priority == depth` (the only value produced until M3), `(priority, seq)`
ordering ≡ `(depth, seq)` ≡ pure `seq` ordering — because `seq` is monotonic with
depth (every depth-*d* task is discovered before any depth-*(d+1)* task; M1 §7). So:

- **Memory:** the priority queue pops in the same order the FIFO queue did → identical.
- **SQLite:** `ORDER BY priority, discovery_seq` ≡ M1's `ORDER BY discovery_seq` →
  identical.

⇒ The `local_fixture` reproducibility gate (512 pages, fixed depth histogram, 511
extractions) stays green unchanged. M2 only becomes *observable* once M3 supplies a
non-depth priority.

---

## 5. Tests

`tests/http/frontier/test_frontier_contract.py` (parametrized memory + sqlite): add
- **`test_pop_orders_by_priority`** — push 3 same-depth tasks with explicit
  priorities (5, 1, 9); assert pop order **1 → 5 → 9** (lower first). The existing
  `test_fifo_order_at_equal_priority` already covers the equal-priority FIFO tiebreak.

`tests/core/runtime/test_engine_frontier_parity.py`: unchanged — still proves
memory ≡ sqlite on the fixture crawl (now via priority=depth).

Full suite + a `wxpath-bench run local_fixture` spot-check must reproduce the exact
pre-M2 baseline (dark-ship).

---

## 6. File manifest

**Modify**
- `src/wxpath/core/models.py` — `priority` default `None` + conditional `__post_init__`.
- `src/wxpath/http/frontier/memory.py` — `PriorityQueue` + seq.
- `src/wxpath/http/frontier/sqlite.py` — ORDER BY priority, index, `SCHEMA_VERSION=2`.
- `src/wxpath/http/frontier/redis.py` — priority-major score.
- `tests/http/frontier/test_frontier_contract.py` — `_task(priority=…)` + priority test.

**No** engine/operator/parser changes (those are M3).

---

## 7. Acceptance — ✅ IMPLEMENTED

- [x] `test_pop_orders_by_priority` green on memory **and** sqlite (pop order 1→5→9).
- [x] Full suite green (241); `local_fixture` byte-identical to pre-M2 — full 512-page
      run reproduces `d0:1 d1:4 d2:16 d3:64 d4:256 d5:171`, 511 extractions (priority=depth ⇒ BFS).
- [x] An explicit `CrawlTask(..., priority=p)` is honored (no longer overwritten by `__post_init__`).
- [x] Convention recorded: lower value = popped sooner (engine); DSL sugar deferred to M3.
