"""GitHub Actions annotations.

A workflow that runs ``migration-guard --format github`` gets every finding
attached to the line of the migration that caused it, so a reviewer sees it in
the diff instead of scrolling a log. The syntax is the one GitHub documents for
workflow commands::

    ::error file=migrations/001.sql,line=12::message

The message is escaped because a newline would end the command early and a
finding's detail regularly contains several sentences.
"""

from __future__ import annotations

from ..models import Finding, Report, Severity

#: GitHub renders ``error`` and ``warning`` in the diff; ``notice`` is the
#: equivalent for something informational.
COMMAND_BY_SEVERITY = {
    Severity.CRITICAL: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "notice",
    Severity.OK: "notice",
}


def _escape(text: str) -> str:
    return (
        text.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _escape_property(text: str) -> str:
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def annotation(finding: Finding) -> str:
    """One workflow command for one finding."""
    command = COMMAND_BY_SEVERITY[finding.severity]
    properties = [f"file={_escape_property(finding.file)}"]
    if finding.line:
        properties.append(f"line={finding.line}")
    properties.append(f"title={_escape_property(finding.rule)}")

    message = finding.title
    if finding.hint:
        message = f"{message} -- {finding.hint}"
    if finding.detail and finding.severity is Severity.CRITICAL:
        message = f"{message}\n{finding.detail}"
    return f"::{command} {','.join(properties)}::{_escape(message)}"


def render(report: Report) -> str:
    """Every finding as a workflow command, plus a summary line."""
    lines = [annotation(finding) for finding in report.findings]
    counts = report.counts()
    lines.append(
        f"{report.tool}: {counts[Severity.CRITICAL]} critical, "
        f"{counts[Severity.WARNING]} warning, {counts[Severity.INFO]} info "
        f"across {report.statement_count} statement(s)"
    )
    if not report.findings:
        lines.append("No findings: this migration will not rebuild a table or block writes.")
    return "\n".join(lines) + "\n"
