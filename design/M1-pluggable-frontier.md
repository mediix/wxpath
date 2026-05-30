# M1 — Pluggable, Persistent Frontier Backend · Implementation Plan

> **Branch:** `feat/m1-pluggable-frontier`
> **Parent:** [FRONTIER_ROADMAP.md §M1](../FRONTIER_ROADMAP.md#m1--pluggable-persistent-frontier-backend)
> **Status:** Ready to implement. Self-contained; depends only on M0 (already landed:
> engine teardown fix in `33890c5`).
> **Prime directive:** ship the abstraction *dark* — with `backend: memory` the
> engine must behave **byte-identically** to today (Invariant I5). All later moves
> (M2 priority, M3 scoring, M6 dedup, M7 host-fairness) plug into this interface.

---

## 1. Scope & non-goals

**In scope (M1):**
- Extract the engine's frontier state — `asyncio.Queue` + `self.seen_urls` — behind
  a `Frontier` interface.
- Three backends: `InMemoryFrontier` (default, behavior-preserving), `SQLiteFrontier`
  (WAL, resumable), `RedisFrontier` (distributed).
- A `get_frontier_backend()` factory mirroring
  [`get_cache_backend()`](../src/wxpath/http/client/cache.py).
- Settings block `http.client.frontier`.
- Resume-from-stop for the SQLite/Redis backends (kill → restart → finish the same
  page-set with **no re-fetch**).
- Tests: per-backend unit, resume integration, reproducibility regression.

**Explicitly NOT in scope (later moves):**
- Priority *ordering logic* — M1 carries `priority` through the schema/queries but
  leaves it at the default (`= depth`), so ordering stays pure BFS. Activating it is **M2**.
- DSL `priority=` syntax — **M3**.
- URL canonicalization / SimHash dedup — **M6** (M1 keeps raw-URL dedup; canonical
  form becomes the PK *later* without interface change).
- Host partitioning / fairness — **M7**.
- The `inflight` response-correlation dict stays in the engine (it correlates crawler
  responses to tasks; it is not frontier state). Durable in-flight recovery for resume
  is handled by the backend's `state` column, not by this dict.

---

## 2. Current state → target state (the 4 touchpoints)

All frontier state lives inside `WXPathEngine.run()`
([engine.py](../src/wxpath/core/runtime/engine.py)). M1 swaps four call-sites; nothing
else in the engine changes.

| # | Today | After M1 |
|---|---|---|
| A | `self.seen_urls: set` in `__init__` | `self.frontier = get_frontier_backend()` (injectable) |
| B | `queue = asyncio.Queue()`; `submitter()` does `await queue.get()` + `seen` check + `seen_urls.add` | `submitter()` does `await self.frontier.pop()`; dedup moved into `frontier.push()` |
| C | `_process_pipeline`: `if next_depth <= max_depth and intent.url not in self.seen_urls: queue.put_nowait(CrawlTask(...))` | `if next_depth <= max_depth: await self.frontier.push(CrawlTask(...))` (push dedups) |
| D | `is_terminal(): return queue.empty() and pending_tasks <= 0`; (no "done" signal) | `is_terminal(): (await frontier.size()) == 0 and pending_tasks <= 0`; add `await frontier.mark_done(task.url)` after a response is fully processed |

The crawler ([crawler.py](../src/wxpath/http/client/crawler.py)) is **untouched**: it has
its own internal `_pending`/`_results` worker pool. `crawler.submit(req)` and
`async for resp in crawler` are unchanged. The "frontier" is strictly the *engine-level
URL queue + dedup that decides what to hand the crawler next.*

---

## 3. The interface

`src/wxpath/http/frontier/base.py`:

```python
from typing import Optional, Protocol, runtime_checkable
from wxpath.core.models import CrawlTask

@runtime_checkable
class Frontier(Protocol):
    """Engine-level URL frontier + dedup. Owns {queued, inflight, done, seen}.

    Ordering contract: pop() returns the highest-priority queued task, ties broken
    by ascending discovery order (a monotonic counter, never wall-clock). With all
    priorities equal (M1 default = depth), this reproduces FIFO/BFS exactly (I5).
    """

    async def push(self, task: CrawlTask) -> bool:
        """Enqueue a task. Dedups by URL: if the URL has ever been seen (queued,
        inflight, or done), do nothing and return False. Otherwise record it
        'queued' and return True. Idempotent."""

    async def pop(self) -> Optional[CrawlTask]:
        """Return the next task (highest priority, earliest discovery), atomically
        flipping it 'queued' -> 'inflight'. Awaits when empty; returns None only
        after close() is signalled and the queue is drained."""

    async def mark_done(self, url: str) -> None:
        """Mark a URL 'done' (its response was fully processed). Enables resume to
        distinguish completed work from interrupted in-flight work."""

    async def seen(self, url: str) -> bool:
        """True if the URL exists in any state (queued | inflight | done)."""

    async def size(self) -> int:
        """Count of 'queued' tasks (drives the terminal condition)."""

    async def checkpoint(self) -> None:
        """Durability barrier (commit/flush). No-op for in-memory."""

    async def close(self) -> None:
        """Release resources; unblock any pending pop()."""
```

**Design notes**
- **Dedup is the frontier's job, enforced at `push`.** This collapses today's two-layer
  dedup (the `not in seen` pre-check in `_process_pipeline` + the authoritative
  `seen_urls.add` in `submitter`) into one atomic operation. Equivalence argument in §7.
- **`pop()` flips to `inflight` atomically.** With a single submitter task there is no
  concurrent-pop race; the flip exists so the durable backends can recover interrupted
  work on resume.
- **`mark_done` is new** and the one genuinely new engine call. Without it, a crash
  leaves popped-but-unfinished URLs marked seen-but-not-done → on resume they'd be
  skipped and never fetched (data loss). `mark_done` closes that gap.
- The interface is **async** end-to-end so SQLite/Redis I/O never blocks the loop;
  in-memory implementations are trivial wrappers.

---

## 4. Backends

### 4.1 `InMemoryFrontier` (default — behavior-preserving)

`src/wxpath/http/frontier/memory.py`:

```python
import asyncio
from typing import Optional
from wxpath.core.models import CrawlTask
from wxpath.http.frontier.base import Frontier

class InMemoryFrontier(Frontier):
    """Faithful wrapper over asyncio.Queue + a seen set. Zero behavior change.
    M2 swaps the FIFO Queue for a PriorityQueue; M1 keeps FIFO."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[CrawlTask] = asyncio.Queue()
        self._seen: set[str] = set()
        self._closed = False

    async def push(self, task: CrawlTask) -> bool:
        if task.url in self._seen:
            return False
        self._seen.add(task.url)            # mark seen at push (see §7 equivalence)
        self._queue.put_nowait(task)
        return True

    async def pop(self) -> Optional[CrawlTask]:
        if self._closed and self._queue.empty():
            return None
        return await self._queue.get()      # blocks like today; cancelled on teardown

    async def mark_done(self, url: str) -> None:
        return                              # no resume in memory → no-op

    async def seen(self, url: str) -> bool:
        return url in self._seen

    async def size(self) -> int:
        return self._queue.qsize()

    async def checkpoint(self) -> None:
        return

    async def close(self) -> None:
        self._closed = True
```

> The engine's `finally` already cancels the submitter task on teardown (M0), so
> `pop()` blocking on `queue.get()` is unblocked by cancellation exactly as today —
> no sentinel needed. `close()` covers explicit drain.

### 4.2 `SQLiteFrontier` (single-node, resumable)

`src/wxpath/http/frontier/sqlite.py`. One table, WAL mode, one covering index.

```sql
CREATE TABLE IF NOT EXISTS frontier (
    url            TEXT PRIMARY KEY,         -- raw URL (M6 will swap to canonical form)
    state          TEXT NOT NULL,            -- 'queued' | 'inflight' | 'done'
    priority       REAL NOT NULL DEFAULT 0,  -- M2 consumes; M1 leaves at depth
    depth          INTEGER NOT NULL,
    discovery_seq  INTEGER NOT NULL,         -- monotonic counter, NOT wall-clock (I6)
    backlink       TEXT,
    segments       BLOB                      -- pickled CrawlTask.segments (next expr)
);
CREATE INDEX IF NOT EXISTS ix_pop ON frontier (state, priority DESC, discovery_seq ASC);
```

Operation → SQL:

| Method | SQL |
|---|---|
| `push` | `INSERT OR IGNORE INTO frontier(url,state,priority,depth,discovery_seq,backlink,segments) VALUES (?, 'queued', ?, ?, ?, ?, ?)` → return `cursor.rowcount == 1` (IGNORE on existing PK = dedup) |
| `pop` | `SELECT url,depth,backlink,segments FROM frontier WHERE state='queued' ORDER BY priority DESC, discovery_seq ASC LIMIT 1`; then `UPDATE frontier SET state='inflight' WHERE url=?` — single transaction. If no row: `await asyncio.sleep(poll_interval)` and retry; return None once `_closed`. |
| `mark_done` | `UPDATE frontier SET state='done' WHERE url=?` |
| `seen` | `SELECT 1 FROM frontier WHERE url=? LIMIT 1` (any state) |
| `size` | `SELECT COUNT(*) FROM frontier WHERE state='queued'` |
| `checkpoint` | `COMMIT` / `PRAGMA wal_checkpoint(TRUNCATE)` |

**Concurrency model.** wxpath runs **one** submitter coroutine, so DB access is
effectively serialized; use a single connection guarded by an `asyncio.Lock`, run the
blocking `sqlite3` calls via `asyncio.to_thread` (or `aiosqlite`). `discovery_seq` is an
in-process monotonic counter seeded from `SELECT COALESCE(MAX(discovery_seq),-1)+1` at
open (so resume continues the sequence).

**Resume.** On open with `resume: true`, run
`UPDATE frontier SET state='queued' WHERE state='inflight'` — interrupted fetches return
to the queue. `done` rows stay done (skipped via `seen`). With `resume: false` (default),
**drop/recreate** the table so each run starts clean (preserves today's semantics and
keeps the benchmark deterministic). Path comes from settings; default lives under a
temp/work dir.

**Segments serialization.** `CrawlTask.segments` is parser AST
([parser.py](../src/wxpath/core/parser.py): `Binary`/`Segment`/`Segments`/`UrlCrawl`/`Xpath`,
all module-level classes) → `pickle` works. Store a `schema_version` (`PRAGMA user_version`)
so a wxpath upgrade that changes the AST invalidates stale frontiers loudly instead of
unpickling garbage. **Verify picklability in a unit test** (see §8); if any node proves
unpicklable, fall back to persisting the seed expression string + re-deriving — flagged
as the one real risk (§10).

### 4.3 `RedisFrontier` (distributed — same interface)

`src/wxpath/http/frontier/redis.py`. Sorted set for the queue, set for dedup. Reuses the
`redis` address already present in cache settings ergonomics.

| Method | Redis |
|---|---|
| `push` | `SADD seen url` → if 0 (already member) return False; else `ZADD queued {score} url` where `score = priority*1e12 + (MAX_SEQ - discovery_seq)` (priority-major, FIFO tie-break) + `HSET task:{url} ...` |
| `pop` | `ZPOPMAX queued` → load `task:{url}`, `SADD inflight url`; poll on empty |
| `mark_done` | `SREM inflight url`; `SADD done url` (or just rely on `seen`) |
| `seen` | `SISMEMBER seen url` |
| `size` | `ZCARD queued` |

Distributed multi-worker is a *capability unlocked by the interface*, not validated in
M1's tests (single-node is the M1 bar). Ship it behind the factory; mark "experimental".

### 4.4 Factory — `src/wxpath/http/frontier/__init__.py`

Mirror [`cache.py::get_cache_backend()`](../src/wxpath/http/client/cache.py) exactly:

```python
from wxpath.settings import SETTINGS
from wxpath.util.logging import get_logger

log = get_logger(__name__)
FRONTIER_SETTINGS = SETTINGS.http.client.frontier

def get_frontier_backend():
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
        return RedisFrontier(address=FRONTIER_SETTINGS.redis.address,
                             namespace=FRONTIER_SETTINGS.redis.namespace,
                             resume=FRONTIER_SETTINGS.resume)
    raise ValueError(f"Unknown frontier backend: {backend}")
```

---

## 5. Settings

Add a `frontier` block under `http.client`, sibling to `cache`/`crawler`, in
[settings.py](../src/wxpath/settings.py). Defaults preserve today's behavior
(`memory`, no resume):

```python
'frontier': {
    'backend': "memory",          # memory | sqlite | redis
    'resume': False,              # reattach to an existing frontier instead of resetting
    'poll_interval': 0.02,        # seconds; sqlite/redis empty-queue poll cadence
    'sqlite': {
        'path': ".wxpath_frontier.db",
    },
    'redis': {
        'address': 'redis://localhost:6379/0',
        'namespace': "wxpath:frontier",
    },
},
```

And the module-level convenience constant alongside the existing two:

```python
FRONTIER_SETTINGS = SETTINGS.http.client.frontier
```

> The benchmark workloads neutralize politeness via `settings_overrides`; they will
> keep `backend: memory` (the default), so the reproducibility gate is unaffected.

---

## 6. Engine integration (exact edits)

All in [engine.py](../src/wxpath/core/runtime/engine.py). Five small edits.

**A — `__init__`:** drop `self.seen_urls`, add an injectable frontier (injectable so
tests and the benchmark can pass a backend; default resolves from settings):

```python
# was:
#   self.seen_urls: set[str] = set()
from wxpath.http.frontier import get_frontier_backend

def __init__(self, crawler=None, concurrency=16, per_host=8, respect_robots=True,
             allowed_response_codes=None, allow_redirects=True, frontier=None):
    self.frontier = frontier or get_frontier_backend()
    ...
```

**B — `run()` local state:** remove the local `queue`; keep `inflight` + `pending_tasks`.
Bind `frontier = self.frontier` for brevity.

```python
frontier = self.frontier
inflight: dict[str, CrawlTask] = {}
pending_tasks = 0

async def is_terminal():
    return (await frontier.size()) == 0 and pending_tasks <= 0
```

> `is_terminal()` becomes a coroutine. Update its 5 call-sites in the response loop to
> `if await is_terminal(): break`. (Mechanical.)

**C — `submitter()`:** pop from the frontier; dedup already enforced at push, so the
submitter only guards against an in-flight URL (defensive) and submits:

```python
async def submitter():
    nonlocal pending_tasks
    while True:
        task = await frontier.pop()
        if task is None:                 # frontier closed & drained
            break
        if task.url in inflight:         # defensive; push-dedup makes this rare
            continue
        inflight[task.url] = task
        pending_tasks += 1
        crawler.submit(Request(task.url, max_retries=0))
```

**D — `_process_pipeline()` enqueue chokepoint:** push instead of `put_nowait`; the
`not in seen` pre-check is gone (push dedups). Note `_process_pipeline` must now `await`
the push, so the queue argument is replaced by the frontier (already on `self`):

```python
elif isinstance(intent, CrawlIntent):
    next_depth = task.depth + 1
    if next_depth <= max_depth:
        await self.frontier.push(CrawlTask(
            elem=None, url=intent.url, segments=intent.next_segments,
            depth=next_depth, backlink=task.url,
        ))
        if pbar is not None:
            pbar.total += 1; pbar.refresh()
```

> `_process_pipeline` is already an async generator, so `await` is free here. Drop the
> `queue` parameter from its signature and both call-sites.

**E — response loop:** after a page is fully processed (the `if task.segments:` / `else`
branches that yield), signal completion for resume, and close the frontier on teardown:

```python
# ...after the post_extract yields for this task...
await frontier.mark_done(task.url)
if await is_terminal():
    break
```

And in the outer `finally` (next to the existing `submit_task.cancel()`), add:

```python
await frontier.close()
```

That is the entire engine change: **4 swaps + 1 new `mark_done` call + `close()`**. No
operator, parser, crawler, or hook changes.

---

## 7. Determinism & the I5 equivalence argument

**Claim:** with `backend: memory`, M1 produces a byte-identical crawl to today.

**Why moving dedup from submit-time to push-time is safe.** Today:
- `_process_pipeline` enqueues a child if `next_depth <= max_depth and url not in seen`.
  Seen is **not** added here, so the same URL discovered on two pages can be enqueued twice.
- `submitter` adds `url` to `seen` at submit and skips any already-seen/in-flight task, so
  only the **first-popped** copy is actually fetched.

The first-popped copy is the one enqueued earliest (FIFO), i.e. the **first discovery**.
Push-time dedup (`push` rejects the second discovery) selects the **same** first-discovery
task and never enqueues the duplicate. Therefore:
- The *set* of fetched URLs is identical.
- Each URL's *depth* is the depth of its first discovery — identical (depth is assigned at
  enqueue; first enqueue wins under both schemes).
- The *submit order* is first-discovery order under both schemes (FIFO, priority=depth).

⇒ `pages_completed`, `unique_urls`, `depth_histogram`, `extracted_total`,
`selector_hit_rate`, `status_buckets` are unchanged. The only difference (a duplicate that
never occupies the queue) is strictly an efficiency gain and is invisible to every metric
the benchmark asserts.

**Codified invariants** (to append to DESIGN.md when M2+ land):
- **I5** — `backend: memory`, no scores ⇒ exact FIFO/BFS order (regression contract).
- **I6** — `discovery_seq` is a monotonic counter, never wall-clock; all dedup/ordering
  signals are pure functions of URL + discovery order ⇒ bit-reproducible.
- **I7** — the frontier is the single source of truth for {queued, inflight, done, seen};
  the engine holds no separate dedup set (`seen_urls` retired).

---

## 8. Tests

`tests/http/frontier/` (new):

1. **`test_frontier_contract.py`** — parametrized over `[InMemoryFrontier, SQLiteFrontier]`
   (tmp path): push/pop FIFO order at equal priority; `push` dedups (second push of same
   URL → False, `size` unchanged); `seen` true across queued/inflight/done; `size` counts
   only queued; `mark_done` flips state; pop after close → None.
2. **`test_sqlite_resume.py`** — push N tasks, pop+`mark_done` for K (< N), pop M more
   *without* `mark_done` (simulate in-flight), `close()`. Reopen with `resume: true`:
   assert the K done URLs are `seen` and not re-queued, the M interrupted URLs are back to
   `queued`, and total `queued == N - K`.
3. **`test_segments_pickle.py`** — parse a representative `///url(...)/map{...}` expression,
   pull a `CrawlTask.segments`, assert `pickle.loads(pickle.dumps(segments))` round-trips
   and the rehydrated op evaluates identically (guards the one real risk).

`tests/core/runtime/`:

4. **`test_engine_frontier_parity.py`** — run a fixed expression against
   `benchmarks.server.FixtureServer` (tiny corpus) twice: once with the default memory
   frontier, once with an explicitly injected `InMemoryFrontier`; assert identical result
   lists. Then run with a `SQLiteFrontier(resume=False)` and assert the **same set** of
   extracted values (order may differ only if priorities differ — they don't in M1).

`tests/benchmarks/`:

5. **Reproducibility gate (existing `test_local_fixture.py`)** must stay green unchanged
   with `backend: memory`. Add one case that runs `local_fixture` with a `SQLiteFrontier`
   and asserts the **same** `pages_completed` / `depth_histogram` / `extracted_total` as
   the memory run (proves the persistent path is faithful, not just the wrapper).

Run (per MEMORY note — from `/tmp`, `.venv`, `PYTHONPATH`):

```bash
R=/Users/mehdinazari/Documents/Workspaces/Professional/Irys/labs/lib/wxpath
cd /tmp && PYTHONPATH="$R/src:$R" "$R/.venv/bin/python" -m pytest \
    "$R/tests/http/frontier/" "$R/tests/core/runtime/" "$R/tests/benchmarks/" -q
```

---

## 9. File manifest

**Create**
- `src/wxpath/http/frontier/__init__.py` — `get_frontier_backend()` factory.
- `src/wxpath/http/frontier/base.py` — `Frontier` Protocol.
- `src/wxpath/http/frontier/memory.py` — `InMemoryFrontier`.
- `src/wxpath/http/frontier/sqlite.py` — `SQLiteFrontier`.
- `src/wxpath/http/frontier/redis.py` — `RedisFrontier` (experimental).
- `tests/http/frontier/__init__.py`, `test_frontier_contract.py`, `test_sqlite_resume.py`,
  `test_segments_pickle.py`.
- `tests/core/runtime/test_engine_frontier_parity.py`.

**Modify**
- `src/wxpath/settings.py` — add `frontier` block + `FRONTIER_SETTINGS`.
- `src/wxpath/core/runtime/engine.py` — the 5 edits in §6.
- `pyproject.toml` — add `frontier-redis = ["redis>=5.0"]` optional extra (sqlite is
  stdlib; no core dep added). Add `aiosqlite>=0.19` only if not using `asyncio.to_thread`.
- `tests/benchmarks/test_local_fixture.py` — add the SQLite-parity case (§8.5).
- `FRONTIER_ROADMAP.md` — link M1 section to this plan; tick M1 once merged.
- `DESIGN.md` — append I5–I7 (optional in M1; can defer to M2).

---

## 10. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| AST `segments` not picklable | low–med | Unit test §8.3 up front; fallback = persist seed expr string + re-derive. Gate `PRAGMA user_version`. |
| SQLite write throughput on hot loopback crawl | med | WAL + single-connection + `to_thread`; batch nothing in M1 (single submitter ⇒ low write rate). Benchmark both backends; memory stays default. |
| `is_terminal()` becoming async introduces a race at drain | low | Keep `pending_tasks` engine-local and decremented before the terminal check, exactly as today; only `size()` moves behind await. |
| Poll-loop latency on sqlite/redis empty queue | low | `poll_interval` default 20ms; only matters when the frontier momentarily empties mid-crawl. Memory backend (default) blocks, no poll. |
| Hidden second use of `seen_urls` elsewhere | low | Grep confirms `seen_urls` is referenced only in `engine.py`; retire fully. |

---

## 11. Rollout checklist (implementation order)

1. `base.py` Protocol + `memory.py` + factory + settings block. Wire engine to
   `get_frontier_backend()` (default memory). **Run full suite — must be all-green with
   zero behavior change.** ← this is the dark-ship gate.
2. `test_frontier_contract.py` (memory only) green.
3. `sqlite.py` + `test_frontier_contract.py` (parametrize sqlite) + `test_segments_pickle.py`.
4. `test_sqlite_resume.py` + the SQLite parity case in the benchmark.
5. `redis.py` + mark experimental (no CI dependency on a live Redis).
6. Docs: link from roadmap; append I5–I7.
7. Re-run the reproducibility gate; confirm `local_fixture` byte-identical on memory and
   set-identical on sqlite.

**Definition of done:** §8 tests green; `local_fixture` reproducibility unchanged on
memory; kill-and-resume on sqlite completes the same page-set with zero re-fetch;
`self.seen_urls` no longer exists.

---

## 12. Acceptance criteria (from the roadmap, concretized)

- [ ] `backend: memory` ⇒ `local_fixture` yields byte-identical coverage/extraction vs
      pre-M1 (`pages_completed=512`, the pinned depth histogram, `extracted_total=511`).
- [ ] `backend: sqlite`, `resume: true`: kill the process at N pages, restart, crawl
      completes the **same total page-set** with **no re-fetch** (verified via `mark_done`
      / `seen` state).
- [ ] `engine.seen_urls` is retired; the frontier is the sole owner of dedup state (I7).
- [ ] Switching backends is a pure settings change — no engine/operator/DSL edits.
