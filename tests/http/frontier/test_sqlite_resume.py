"""Resume semantics for the SQLite frontier.

Simulates a crash: some URLs completed (mark_done), some interrupted mid-flight
(popped but never marked done). Reopening with resume=True must return interrupted
URLs to the queue and never re-queue completed ones.
"""

import pytest

from wxpath.core.models import CrawlTask
from wxpath.http.frontier.sqlite import SQLiteFrontier


def _task(url: str, depth: int = 1) -> CrawlTask:
    return CrawlTask(elem=None, url=url, segments=[], depth=depth, backlink=None)


@pytest.mark.asyncio
async def test_resume_reclaims_inflight_and_skips_done(tmp_path):
    path = str(tmp_path / "frontier.db")
    N, K, M = 8, 3, 2  # total, completed, interrupted

    f = SQLiteFrontier(path=path, resume=False)
    for i in range(N):
        assert await f.push(_task(f"http://x/{i}")) is True
    assert await f.size() == N

    # Complete K (FIFO → urls 0..K-1).
    done_urls = set()
    for _ in range(K):
        t = await f.pop()
        await f.mark_done(t.url)
        done_urls.add(t.url)

    # Interrupt M (popped → inflight, never marked done).
    interrupted = set()
    for _ in range(M):
        t = await f.pop()
        interrupted.add(t.url)

    assert await f.size() == N - K - M
    await f.close()  # "crash": close without finishing the interrupted ones

    # Reopen and resume.
    f2 = SQLiteFrontier(path=path, resume=True)

    # Interrupted work is back in the queue; completed work is not.
    assert await f2.size() == N - K  # (N-K-M still queued) + (M reclaimed)
    for url in done_urls:
        assert await f2.seen(url) is True       # recorded, but 'done'
    for url in interrupted:
        assert await f2.seen(url) is True

    # Draining yields exactly the non-done URLs; the K done URLs never reappear.
    popped = set()
    for _ in range(N - K):
        t = await f2.pop()
        popped.add(t.url)
    assert popped.isdisjoint(done_urls)
    assert interrupted.issubset(popped)
    assert await f2.size() == 0
    await f2.close()


@pytest.mark.asyncio
async def test_no_resume_starts_clean(tmp_path):
    path = str(tmp_path / "frontier.db")
    f = SQLiteFrontier(path=path, resume=False)
    for i in range(4):
        await f.push(_task(f"http://x/{i}"))
    await f.close()

    # Default (resume=False) wipes the prior frontier.
    f2 = SQLiteFrontier(path=path, resume=False)
    assert await f2.size() == 0
    assert await f2.seen("http://x/0") is False
    await f2.close()
