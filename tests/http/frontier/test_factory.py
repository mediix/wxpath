"""get_frontier_backend() selects the backend purely from settings (acceptance §12:
switching backends is a settings change, no engine/operator/DSL edits)."""

import pytest

from wxpath.http import frontier as frontier_pkg
from wxpath.http.frontier import get_frontier_backend
from wxpath.http.frontier.memory import InMemoryFrontier
from wxpath.http.frontier.sqlite import SQLiteFrontier


def test_default_backend_is_memory():
    assert isinstance(get_frontier_backend(), InMemoryFrontier)


def test_settings_select_sqlite(monkeypatch, tmp_path):
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS, "backend", "sqlite")
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.sqlite, "path", str(tmp_path / "f.db"))
    assert isinstance(get_frontier_backend(), SQLiteFrontier)


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS, "backend", "bogus")
    with pytest.raises(ValueError):
        get_frontier_backend()


# --- M4b: trap filter wrapping is settings-gated --------------------------

def test_trap_disabled_returns_bare_backend():
    # Default (trap.enabled is False) ⇒ no wrapper, identical object graph (I5).
    from wxpath.http.frontier.trap import TrapFilterFrontier
    backend = get_frontier_backend()
    assert isinstance(backend, InMemoryFrontier)
    assert not isinstance(backend, TrapFilterFrontier)


def test_trap_enabled_wraps_configured_backend(monkeypatch):
    from wxpath.http.frontier.trap import TrapFilterFrontier
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.trap, "enabled", True)
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.trap, "max_path_repeat", 2)
    backend = get_frontier_backend()
    assert isinstance(backend, TrapFilterFrontier)
    assert isinstance(backend._inner, InMemoryFrontier)   # wraps the configured backend
    assert backend._max_path_repeat == 2


def test_canonical_disabled_returns_bare_backend():
    # Default (canonical.enabled is False) ⇒ no wrapper, identical object graph (I5/I11).
    from wxpath.http.frontier.canonical import CanonicalizingFrontier
    backend = get_frontier_backend()
    assert isinstance(backend, InMemoryFrontier)
    assert not isinstance(backend, CanonicalizingFrontier)


def test_canonical_enabled_wraps_configured_backend(monkeypatch):
    from wxpath.http.frontier.canonical import CanonicalizingFrontier
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.canonical, "enabled", True)
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.canonical, "strip_params", ["utm_*"])
    backend = get_frontier_backend()
    assert isinstance(backend, CanonicalizingFrontier)
    assert isinstance(backend._inner, InMemoryFrontier)
    assert backend._strip_params == ("utm_*",)


def test_canonical_is_outermost_over_trap(monkeypatch):
    # With both enabled, the canonicalizer wraps the trap filter (URLs are
    # normalized before trap detection / dedup ever see them).
    from wxpath.http.frontier.canonical import CanonicalizingFrontier
    from wxpath.http.frontier.trap import TrapFilterFrontier
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.trap, "enabled", True)
    monkeypatch.setattr(frontier_pkg.FRONTIER_SETTINGS.canonical, "enabled", True)
    backend = get_frontier_backend()
    assert isinstance(backend, CanonicalizingFrontier)
    assert isinstance(backend._inner, TrapFilterFrontier)
    assert isinstance(backend._inner._inner, InMemoryFrontier)
