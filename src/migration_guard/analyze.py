"""Run the rules over files and assemble a report.

The pipeline is deliberately short and linear: lex, parse, classify, judge,
filter by suppression, sort. Every step is a pure function of the file text,
which is why a review of a 300-line migration takes milliseconds and why the
whole tool can be exercised against a string in a test.
"""

from __future__ import annotations

from datetime import UTC, datetime

from . import directives as directives_module
from . import rules as rules_module
from . import statements as st
from .models import FileReview, Finding, Report, Severity
from .sqltext import ParseResult, parse_sql

TOOL_NAME = "migration-guard"
#: Rules about the suppressions themselves are never themselves suppressible,
#: or a malformed directive could silence the check that reports malformed
#: directives.
UNSUPPRESSIBLE_PREFIXES = ("directive.",)


def build_context(
    path: str, text: str, version: tuple[int, int, int]
) -> rules_module.RuleContext:
    """Lex, parse and classify one file, ready for the rules."""
    parse: ParseResult = parse_sql(text)
    suppressions = directives_module.collect(parse.statements, parse.file_comments)

    alters: list[st.AlterTable] = []
    dmls: list[tuple] = []
    for statement in parse.statements:
        alter = st.parse_alter_table(statement, version)
        if alter is not None:
            alters.append(alter)
            continue
        shape = st.parse_dml(statement)
        if shape is not None:
            dmls.append((statement, shape))

    return rules_module.RuleContext(
        path=path,
        version=version,
        parse=parse,
        alters=alters,
        dmls=dmls,
        suppressions=suppressions,
    )


def _suppressible(rule: str) -> bool:
    return not rule.startswith(UNSUPPRESSIBLE_PREFIXES)


def analyze_text(path: str, text: str, version: tuple[int, int, int]) -> FileReview:
    """Review one file's text and return its findings."""
    context = build_context(path, text, version)
    review = FileReview(path=path, statement_count=len(context.statements))

    suppressed = 0
    for rule in rules_module.RULES:
        for finding in rule(context):
            if _suppressible(finding.rule) and context.suppressions.silences(
                finding.rule, finding.statement_index
            ):
                suppressed += 1
                continue
            review.findings.append(finding)

    review.findings.sort(key=lambda f: (-int(f.severity), f.line, f.rule))
    if suppressed:
        review.notes.append(
            f"{suppressed} finding(s) suppressed by an in-file directive"
        )
    if not context.statements:
        review.notes.append("no SQL statements found in this file")
    return review


def timestamp(when: datetime | None = None) -> str:
    """RFC 3339 UTC timestamp, second precision."""
    moment = when or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def review(
    files: list[tuple[str, str]],
    *,
    version: tuple[int, int, int],
    version_label: str = "",
) -> Report:
    """Review several ``(path, text)`` pairs into one report."""
    report = Report(
        tool=TOOL_NAME,
        version=_version(),
        generated_at=timestamp(),
        target_version=version_label or ".".join(str(part) for part in version),
    )
    for path, text in files:
        report.files.append(analyze_text(path, text, version))

    total_suppressed = sum(
        1 for file_review in report.files for note in file_review.notes if "suppressed" in note
    )
    if total_suppressed:
        report.notes.append(
            "Some findings were suppressed by `migration-guard: allow(...)` directives; "
            "the directives are listed in the JSON output."
        )
    return report


def _version() -> str:
    from . import __version__

    return __version__


def worst_severity(findings: list[Finding]) -> Severity:
    return max((finding.severity for finding in findings), default=Severity.OK)
