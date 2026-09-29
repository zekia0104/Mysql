"""``python -m migration_guard`` -- identical to the ``migration-guard`` script."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
