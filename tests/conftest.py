"""Test bootstrap: ensure src-layout wxpath wins over any stale top-level dir.

The repo's working directory is on sys.path[0] when pytest runs. If a stale
top-level ``wxpath/`` directory (e.g. left over from a pre-refactor checkout)
exists without an ``__init__.py``, Python treats it as a namespace package and
shadows the installed ``src/wxpath/``. Prepending ``src/`` to sys.path keeps the
real package in front for the duration of the test session.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / 'src'
if _SRC.is_dir():
    _src_str = str(_SRC)
    if _src_str not in sys.path:
        sys.path.insert(0, _src_str)
