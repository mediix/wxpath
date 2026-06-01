"""SQLite frontier — single-node, disk-persistent, resumable.

One table (WAL mode) is the durable source of truth for {queued, inflight, done,
seen}. A crash leaves the table intact; reopening with ``resume=True`` returns any
interrupted in-flight URLs to the queue and skips completed ones, so the crawl
finishes the same page-set without re-fetching.

Concurrency: wxpath drives the engine on a single event loop. Every method below
performs its SQLite work synchronously with no ``await`` between touching the
connection and returning, so two coroutines can never use the connection at once —
no lock or worker thread is required. Only :meth:`pop` awaits, and only on
``asyncio.sleep`` *between* probes (never mid-statement).

Ordering (M2): ``priority ASC, discovery_seq ASC`` — lower priority value popped
sooner (the ``models.py`` convention), ties broken FIFO. With the default
``priority == depth`` this reproduces the historical BFS order exactly
(``discovery_seq`` is monotonic with crawl depth), so the change is dark until M3
supplies a non-depth priority.
"""

import asyncio
import pickle
import sqlite3
from typing import Optional

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.base import Frontier
from wxpath.util.logging import get_logger

log = get_logger(__name__)

# Bump when the on-disk schema or the pickled `segments` shape changes; a mismatch
# against a pre-existing db raises rather than silently unpickling stale data.
SCHEMA_VERSION = 2  # v2: pop ordering/index include `priority` (M2)


class SQLiteFrontier(Frontier):
    def __init__(self, path: str, resume: bool = False, poll_interval: float = 0.02) -> None:
        self._path = path
        self._poll_interval = poll_interval
        self._closed = False
        # check_same_thread=False is defensive only (all access is single-threaded);
        # it avoids surprises if an engine is driven from a non-creating thread.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._setup(resume)
        self._seq = self._next_seq()

    def _setup(self, resume: bool) -> None:
        conn = self._conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError(
                f"frontier schema mismatch: db={version} code={SCHEMA_VERSION}; "
                f"delete {self._path} or migrate it."
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS frontier (
                url           TEXT PRIMARY KEY,
                state         TEXT NOT NULL,
                priority      REAL NOT NULL DEFAULT 0,
                depth         INTEGER NOT NULL,
                discovery_seq INTEGER NOT NULL,
                backlink      TEXT,
                segments      BLOB
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_pop ON frontier (state, priority, discovery_seq)"
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if resume:
            # Interrupted in-flight work returns to the queue; 'done' stays done.
            conn.execute("UPDATE frontier SET state='queued' WHERE state='inflight'")
        else:
            conn.execute("DELETE FROM frontier")
        conn.commit()

    def _next_seq(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(discovery_seq), -1) + 1 FROM frontier"
        ).fetchone()
        return int(row[0])

    async def push(self, task: CrawlTask) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO frontier "
            "(url, state, priority, depth, discovery_seq, backlink, segments) "
            "VALUES (?, 'queued', ?, ?, ?, ?, ?)",
            (
                task.url,
                float(task.priority),
                task.depth,
                self._seq,
                task.backlink,
                pickle.dumps(task.segments),
            ),
        )
        self._conn.commit()
        if cur.rowcount == 1:
            self._seq += 1
            return True
        return False  # URL already present in some state → deduped

    async def pop(self) -> Optional[CrawlTask]:
        while True:
            if self._closed:
                return None
            row = self._conn.execute(
                "SELECT url, depth, backlink, segments FROM frontier "
                "WHERE state='queued' ORDER BY priority ASC, discovery_seq ASC LIMIT 1"
            ).fetchone()
            if row is not None:
                url, depth, backlink, segments = row
                self._conn.execute("UPDATE frontier SET state='inflight' WHERE url=?", (url,))
                self._conn.commit()
                return CrawlTask(
                    elem=None,
                    url=url,
                    segments=pickle.loads(segments),
                    depth=depth,
                    backlink=backlink,
                )
            # Queue momentarily empty but crawl may not be done; poll until a push
            # lands or the frontier is closed.
            await asyncio.sleep(self._poll_interval)

    async def mark_done(self, url: str) -> None:
        self._conn.execute("UPDATE frontier SET state='done' WHERE url=?", (url,))
        self._conn.commit()

    async def seen(self, url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM frontier WHERE url=? LIMIT 1", (url,)
        ).fetchone()
        return row is not None

    async def size(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM frontier WHERE state='queued'"
        ).fetchone()
        return int(row[0])

    async def checkpoint(self) -> None:
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.commit()

    async def close(self) -> None:
        self._closed = True
        try:
            self._conn.commit()
            self._conn.close()
        except sqlite3.Error:
            pass
