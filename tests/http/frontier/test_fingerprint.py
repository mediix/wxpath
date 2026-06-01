"""M6 layer 2: SimHash content fingerprinting + ContentDeduper strategy."""

import lxml.html
import pytest

from wxpath.http.frontier.fingerprint import (
    NullDeduper,
    SimHashDeduper,
    get_content_deduper,
    hamming,
    simhash,
)

_T1 = "the quick brown fox jumps over the lazy dog near the river bank " * 5
_T1_NEAR = _T1 + "and then it trotted away quietly into the woods"
_T2 = "quantum chromodynamics describes the strong interaction between quarks " * 5


def _page(body_html: str):
    return lxml.html.fromstring(f"<html><body>{body_html}</body></html>")


# --------------------------------------------------------------------------- #
# Pure SimHash / Hamming
# --------------------------------------------------------------------------- #
def test_simhash_is_deterministic():
    # Stable hash → same fingerprint across calls (no PYTHONHASHSEED dependence).
    assert simhash(_T1) == simhash(_T1)


def test_empty_text_is_zero():
    assert simhash("") == 0
    assert simhash("   ") == 0


def test_identical_text_zero_distance():
    assert hamming(simhash(_T1), simhash(_T1)) == 0


def test_near_text_is_closer_than_unrelated_text():
    near = hamming(simhash(_T1), simhash(_T1_NEAR))
    far = hamming(simhash(_T1), simhash(_T2))
    assert near < far


def test_hamming_counts_differing_bits():
    assert hamming(0b1011, 0b1110) == 2
    assert hamming(7, 7) == 0


def test_shingle_size_is_honored():
    # Different shingle sizes generally yield different fingerprints for the same
    # text — proving the parameter actually feeds the feature set.
    assert simhash(_T1, shingle_size=2) != simhash(_T1, shingle_size=8)


# --------------------------------------------------------------------------- #
# NullDeduper (default)
# --------------------------------------------------------------------------- #
def test_null_deduper_never_dedups():
    d = NullDeduper()
    page = _page("<article><p>" + _T1 + "</p></article>")
    assert d.is_duplicate(page) is False
    assert d.is_duplicate(page) is False  # even the identical page again


# --------------------------------------------------------------------------- #
# SimHashDeduper
# --------------------------------------------------------------------------- #
def test_first_page_kept_identical_second_is_duplicate():
    d = SimHashDeduper()
    p1 = _page("<article><p>" + _T1 + "</p></article>")
    p2 = _page("<article><p>" + _T1 + "</p></article>")  # identical content
    assert d.is_duplicate(p1) is False   # first occurrence recorded + kept
    assert d.is_duplicate(p2) is True    # near-dup (distance 0) skipped
    assert d.duplicates == 1


def test_distinct_pages_are_not_duplicates():
    d = SimHashDeduper()
    assert d.is_duplicate(_page("<article><p>" + _T1 + "</p></article>")) is False
    assert d.is_duplicate(_page("<article><p>" + _T2 + "</p></article>")) is False
    assert d.duplicates == 0


def test_empty_content_is_never_duplicate_and_not_recorded():
    d = SimHashDeduper()
    assert d.is_duplicate(_page("")) is False           # no text → no fingerprint
    assert d.is_duplicate(_page("")) is False           # still not a dup
    # An empty page must not poison the registry: a real page after it is kept.
    assert d.is_duplicate(_page("<article><p>" + _T1 + "</p></article>")) is False


def test_threshold_is_consulted():
    # Max threshold ⇒ any second page is "within" distance ⇒ duplicate.
    loose = SimHashDeduper(hamming_threshold=64)
    assert loose.is_duplicate(_page("<article><p>" + _T1 + "</p></article>")) is False
    assert loose.is_duplicate(_page("<article><p>" + _T2 + "</p></article>")) is True

    # Zero threshold ⇒ only exact-fingerprint matches dedup; distinct text does not.
    strict = SimHashDeduper(hamming_threshold=0)
    assert strict.is_duplicate(_page("<article><p>" + _T1 + "</p></article>")) is False
    assert strict.is_duplicate(_page("<article><p>" + _T2 + "</p></article>")) is False


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def test_factory_disabled_returns_null():
    assert isinstance(get_content_deduper(), NullDeduper)


def test_factory_enabled_returns_simhash(monkeypatch):
    from wxpath.http import frontier as frontier_pkg
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.fingerprint, "enabled", True)
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.fingerprint, "hamming_threshold", 5)
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.fingerprint, "shingle_size", 3)
    d = get_content_deduper()
    assert isinstance(d, SimHashDeduper)
    assert d._threshold == 5
    assert d._shingle_size == 3
