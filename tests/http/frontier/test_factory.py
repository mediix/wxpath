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
