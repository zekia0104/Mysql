"""Test package.

``migration_guard`` uses a ``src/`` layout, so the package is not importable
from the repository root without an install. Rather than requiring
``pip install -e .`` before the tests can run, this shim puts ``src`` on the
path. CI also runs the suite once *with* the package installed, so packaging
mistakes still surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REPO_ROOT = SRC.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

__all__ = ["FIXTURES", "REPO_ROOT", "SRC"]
