"""Output renderers.

Each module turns a :class:`~migration_guard.models.Report` into a string. They
share nothing but the model, so adding a format means adding one file and one
entry in :data:`RENDERERS`.
"""

from __future__ import annotations

from collections.abc import Callable

from ..models import Report
from . import github, json_report, text

RENDERERS: dict[str, Callable[[Report], str]] = {
    "text": text.render,
    "json": json_report.render,
    "github": github.render,
}

FORMAT_CHOICES = ("text", "json", "github")


def render(report: Report, fmt: str) -> str:
    """Render ``report`` in ``fmt`` (one of :data:`FORMAT_CHOICES`)."""
    try:
        renderer = RENDERERS[fmt]
    except KeyError as exc:  # pragma: no cover - argparse restricts the value
        raise ValueError(f"unknown output format: {fmt!r}") from exc
    return renderer(report)


__all__ = ["FORMAT_CHOICES", "RENDERERS", "github", "json_report", "render", "text"]
