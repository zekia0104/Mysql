"""mysql-migration-guard -- review MySQL migrations before they reach production.

The package reads ``.sql`` files, classifies each statement against InnoDB's
online-DDL behaviour for a target server version, and reports the changes that
will rebuild a table, block writes or destroy data.

There are no third-party dependencies, on purpose: the tool runs in a CI step
that has Python and nothing else.

Typical use::

    from migration_guard.analyze import review
    from migration_guard.statements import parse_version

    report = review([("migrations/001.sql", sql_text)], version=parse_version("8.0.29"))
    raise SystemExit(report.exit_code)
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
