"""Machine-readable output.

Carries the pre-formatted findings for a person reading raw JSON, and enough
structure for a dashboard or a policy gate to make its own decision without
re-parsing prose.
"""

from __future__ import annotations

import json

from ..models import Report


def render(report: Report, *, indent: int = 2) -> str:
    """Serialise the report as pretty-printed JSON."""
    return json.dumps(report.to_dict(), indent=indent, ensure_ascii=False)


def render_compact(report: Report) -> str:
    """Single-line JSON, for log shipping."""
    return json.dumps(report.to_dict(), separators=(",", ":"), ensure_ascii=False)
