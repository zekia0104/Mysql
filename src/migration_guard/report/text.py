"""The default, human-facing output.

Colour only when a human is watching, and box-drawing characters only when the
output encoding can represent them: a report redirected to a file on a Windows
console whose code page cannot hold a glyph must not fail half way through.
"""

from __future__ import annotations

import os
import sys

from ..models import Finding, Report, Severity

DEFAULT_WIDTH = 92

RESET = "\033[0m"
CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
}

SEVERITY_COLOR = {
    Severity.OK: "green",
    Severity.INFO: "cyan",
    Severity.WARNING: "yellow",
    Severity.CRITICAL: "red",
}

UNICODE_GLYPHS = {"heavy": "=", "light": "-", "arrow": "->", "dot": "*", "bullet": "*"}
UNICODE_GLYPHS.update({"heavy": "\u2550", "light": "\u2500", "arrow": "\u2192", "dot": "\u00b7", "bullet": "\u2022"})

ASCII_GLYPHS = {"heavy": "=", "light": "-", "arrow": "->", "dot": "*", "bullet": "*"}

#: Every non-ASCII character the renderer emits on purpose, with an ASCII
#: stand-in. Kept complete by a test that audits the source tree.
GLYPH_FALLBACKS = {
    "\u2550": "=",
    "\u2500": "-",
    "\u00b7": "|",
    "\u2022": "*",
    "\u2192": "->",
    "\u2026": "...",
}


def can_encode(text: str, encoding: str | None) -> bool:
    """Whether ``encoding`` can represent ``text``; unknown encodings are capable."""
    if not encoding:
        return True
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return False
    except LookupError:
        return True
    return True


def downgrade(text: str, encoding: str | None) -> str:
    """Substitute the glyphs this renderer chose, per character.

    Per character and not per string: a GBK console cannot encode U+2022 but
    draws boxes and arrows perfectly well, so a report there keeps its layout and
    only the bullet becomes ``*``.
    """
    if can_encode(text, encoding):
        return text
    table = str.maketrans(
        {
            glyph: stand_in
            for glyph, stand_in in GLYPH_FALLBACKS.items()
            if not can_encode(glyph, encoding)
        }
    )
    return text.translate(table)


def safe_write(stream: object, text: str) -> None:
    """Write without ever raising for encoding reasons.

    Escalates: as-is, then with glyphs downgraded, then with whatever is left
    replaced. The payload is prepared in full before a byte is written, so a
    failure cannot leave half a document behind.
    """
    encoding = getattr(stream, "encoding", None)

    if can_encode(text, encoding):
        stream.write(text)  # type: ignore[attr-defined]
        return
    payload = downgrade(text, encoding)
    if can_encode(payload, encoding):
        stream.write(payload)  # type: ignore[attr-defined]
        return
    codec = encoding or "ascii"
    try:
        stream.write(payload.encode(codec, "replace").decode(codec, "replace"))  # type: ignore[attr-defined]
    except LookupError:  # pragma: no cover - an unknown code page
        stream.write(payload.encode("ascii", "replace").decode("ascii"))  # type: ignore[attr-defined]


class Style:
    """Minimal ANSI styling with a global off switch."""

    __slots__ = ("enabled", "glyphs")

    def __init__(self, enabled: bool, unicode_ok: bool) -> None:
        self.enabled = enabled
        self.glyphs = UNICODE_GLYPHS if unicode_ok else ASCII_GLYPHS

    def paint(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(CODES.get(name, "") for name in styles)
        return f"{prefix}{text}{RESET}" if prefix else text

    def glyph(self, name: str) -> str:
        return self.glyphs[name]


def should_color(override: bool | None = None) -> bool:
    if override is not None:
        return override
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _wrap(text: str, width: int, indent: int) -> str:
    limit = max(width - indent, 24)
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > limit and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(" " * indent + line for line in lines)


def _summary(report: Report, style: Style) -> str:
    counts = report.counts()
    status = report.status
    label = "clean" if status is Severity.OK else status.label
    parts = [
        f"{counts[Severity.CRITICAL]} critical",
        f"{counts[Severity.WARNING]} warning",
        f"{counts[Severity.INFO]} info",
    ]
    headline = style.paint(f"[{label}]", SEVERITY_COLOR[status], "bold")
    return (
        f"  {report.statement_count} statement(s) in {len(report.files)} file(s)"
        f"   {headline}   {'  '.join(parts)}"
    )


def _finding(finding: Finding, style: Style, width: int) -> list[str]:
    color = SEVERITY_COLOR[finding.severity]
    marker = style.paint(f"{finding.severity.label:<8}", color, "bold")
    location = style.paint(f"{finding.file}:{finding.line}", "cyan")
    out = [f"  {marker} {location}  {style.paint(finding.title, 'bold')}"]
    out.append(style.paint(f"           {finding.rule}", "dim"))
    if finding.snippet:
        out.append(_wrap(f"{style.glyph('arrow')} {finding.snippet}", width, 11))
    if finding.detail:
        out.append(_wrap(finding.detail, width, 11))
    if finding.hint:
        out.append(_wrap(f"fix: {finding.hint}", width, 11))
    return out


def render(
    report: Report,
    *,
    width: int = DEFAULT_WIDTH,
    color: bool | None = None,
    encoding: str | None = None,
) -> str:
    """Render the full text report."""
    capabilities = encoding if encoding is not None else "utf-8"
    style = Style(should_color(override=color), can_encode(UNICODE_GLYPHS["heavy"], capabilities))

    lines: list[str] = []
    lines.append(
        style.paint(f"{report.tool} {report.version}", "bold", "blue")
        + style.paint(f"  {style.glyph('dot')}  target MySQL {report.target_version}", "dim")
    )
    lines.append(style.paint(style.glyph("heavy") * width, "dim"))
    lines.append(_summary(report, style))

    for review in report.files:
        lines.append("")
        header = f"{review.path}  ({review.statement_count} statements) "
        lines.append(
            style.paint(
                header + style.glyph("light") * max(width - len(header), 4), "bold", "blue"
            )
        )
        if not review.findings:
            lines.append(
                "  " + style.paint("no findings", "green")
                + style.paint("  -- nothing here will rebuild a table or block writes", "dim")
            )
        for finding in review.findings:
            lines.append("")
            lines.extend(_finding(finding, style, width))
        for note in review.notes:
            lines.append(style.paint(f"  note: {note}", "dim"))

    if report.notes:
        lines.append("")
        for note in report.notes:
            lines.append(_wrap(f"{style.glyph('bullet')} {note}", width, 2))

    lines.append("")
    if report.status is Severity.OK:
        lines.append("  " + style.paint("Migration looks safe to apply.", "green", "bold"))
    else:
        lines.append(
            "  "
            + style.paint(
                f"Review the {report.counts()[report.status]} {report.status.label.lower()} "
                "finding(s) above before applying.",
                SEVERITY_COLOR[report.status],
                "bold",
            )
        )
    lines.append("")
    return downgrade("\n".join(lines), capabilities)
