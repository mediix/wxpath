"""M4b: pure URL-path-repeat trap detection (frontier/trap.py)."""

import pytest

from wxpath.http.frontier.trap import (
    is_path_trap,
    max_consecutive_cycle,
    _segments,
)


class TestSegments:
    def test_strips_empty_and_query_fragment(self):
        assert _segments("http://h/a/b/c") == ["a", "b", "c"]
        assert _segments("http://h/a//b/") == ["a", "b"]          # empties dropped
        assert _segments("http://h/a/b?x=1#frag") == ["a", "b"]   # query/fragment ignored
        assert _segments("http://h/") == []


class TestMaxConsecutiveCycle:
    def test_no_repeat_is_one(self):
        assert max_consecutive_cycle(["a", "b", "c", "d"], max_period=4) == (1, 1)

    def test_period1_run(self):
        assert max_consecutive_cycle(["x", "x", "x"], max_period=4) == (1, 3)

    def test_period2_cycle(self):
        # a/b repeated 4×
        period, reps = max_consecutive_cycle(["a", "b"] * 4, max_period=4)
        assert (period, reps) == (2, 4)

    def test_cycle_longer_than_max_period_not_seen(self):
        # period-3 cycle but we only scan up to period 2 → no big run found
        segs = ["a", "b", "c"] * 3
        _, reps = max_consecutive_cycle(segs, max_period=2)
        assert reps == 1

    def test_run_embedded_mid_path(self):
        # distinct prefix, then a period-1 run of 4
        segs = ["docs", "x", "x", "x", "x"]
        assert max_consecutive_cycle(segs, max_period=4) == (1, 4)


class TestIsPathTrap:
    def test_legit_deep_chain_never_trapped(self):
        # A legitimate deep chain of distinct segments — equal length to a trap —
        # is never pruned (the design §5 acceptance).
        url = "http://h/docs/guide/intro/setup/install/config/deploy/scale"
        assert is_path_trap(url, max_path_repeat=3, max_period=4) is False

    def test_period2_trap_at_boundary(self):
        # 3 repeats kept (== threshold), 4 repeats dropped (> threshold)
        keep = "http://h/" + "/".join(["a", "b"] * 3)   # reps == 3
        drop = "http://h/" + "/".join(["a", "b"] * 4)   # reps == 4
        assert is_path_trap(keep, max_path_repeat=3, max_period=4) is False
        assert is_path_trap(drop, max_path_repeat=3, max_period=4) is True

    def test_period1_trap(self):
        assert is_path_trap("http://h/next/next/next/next", max_path_repeat=3) is True
        assert is_path_trap("http://h/next/next/next", max_path_repeat=3) is False

    def test_query_string_does_not_trip_path_filter(self):
        # repetition only in the query is ignored by the path filter
        assert is_path_trap("http://h/search?q=a&q=a&q=a&q=a") is False

    def test_max_path_repeat_below_one_disables(self):
        url = "http://h/" + "/".join(["a", "b"] * 9)
        assert is_path_trap(url, max_path_repeat=0) is False

    def test_deterministic(self):
        url = "http://h/" + "/".join(["a", "b"] * 5)
        assert is_path_trap(url) == is_path_trap(url) is True
