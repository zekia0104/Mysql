"""In-SQL suppression, the ``# noqa`` of migrations.

A review tool that cannot be silenced gets disabled. But a suppression that
silences something silently is worse than the finding it hid, so two rules watch
the suppressions themselves: one for a directive naming a rule that does not
exist (a typo quietly disabling nothing), and one for a suppression with no
stated reason.

Syntax, in any comment::

    -- migration-guard: allow(ddl.table-rebuild) reason: lookup table, 40 rows
    ALTER TABLE country_codes MODIFY code CHAR(2);

    -- migration-guard: allow-file(file.use-statement) reason: generated dump

``allow`` applies to the statement the comment is attached to; ``allow-file``
applies to the whole file. ``reason:`` is optional but recommended, and its
absence is reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import COMMENT, EXEC_COMMENT, Statement, Token

PREFIX = "migration-guard:"
_PATTERN = re.compile(
    r"migration-guard:\s*(?P<action>allow-file|allow)\s*\((?P<rules>[^)]*)\)"
    r"(?:\s*reason\s*:\s*(?P<reason>.*))?",
    re.IGNORECASE | re.DOTALL,
)

ACTION_STATEMENT = "allow"
ACTION_FILE = "allow-file"


@dataclass(frozen=True)
class Directive:
    """One parsed suppression."""

    action: str
    rules: tuple[str, ...]
    reason: str
    line: int
    source: str = ""

    @property
    def is_file_scoped(self) -> bool:
        return self.action == ACTION_FILE


@dataclass
class Suppressions:
    """What the file asked to silence, resolved by scope."""

    #: Rule ids silenced for every statement in the file.
    file_rules: set[str] = field(default_factory=set)
    #: Rule ids silenced for one statement, keyed by statement index.
    statement_rules: dict[int, set[str]] = field(default_factory=dict)
    #: Every directive found, for the rules that audit them.
    all: list[Directive] = field(default_factory=list)

    def silences(self, rule: str, statement_index: int) -> bool:
        if rule in self.file_rules:
            return True
        return rule in self.statement_rules.get(statement_index, set())

    @property
    def is_empty(self) -> bool:
        return not self.all


def clean_comment(text: str) -> str:
    """Strip comment syntax so only the payload remains."""
    body = text.strip()
    if body.startswith("--"):
        body = body[2:]
    elif body.startswith("#"):
        body = body[1:]
    elif body.startswith("/*!"):
        body = body[3:]
        body = re.sub(r"^\d*", "", body)
    elif body.startswith("/*M!"):
        body = body[4:]
        body = re.sub(r"^\d*", "", body)
    elif body.startswith("/*"):
        body = body[2:]
    if body.endswith("*/"):
        body = body[:-2]
    return body.strip()


def parse_directive(text: str, line: int) -> Directive | None:
    """Parse one comment into a :class:`Directive`, or ``None`` if it is not one."""
    match = _PATTERN.search(text)
    if not match:
        return None
    rules = tuple(
        name.strip() for name in match.group("rules").split(",") if name.strip()
    )
    reason = (match.group("reason") or "").strip()
    return Directive(
        action=match.group("action").lower(),
        rules=rules,
        reason=reason,
        line=line,
        source=text.strip(),
    )


def collect(statements: list[Statement], file_comments: list[Token]) -> Suppressions:
    """Gather every directive in the file and resolve it to a scope."""
    result = Suppressions()

    for comment in file_comments:
        directive = parse_directive(
            clean_comment(comment.text), comment.line
        )
        if directive is not None:
            result.all.append(directive)
            if directive.is_file_scoped:
                result.file_rules.update(directive.rules)

    for statement in statements:
        for comment in statement.comments:
            if comment.kind not in (COMMENT, EXEC_COMMENT):
                continue
            directive = parse_directive(clean_comment(comment.text), comment.line)
            if directive is None:
                continue
            result.all.append(directive)
            if directive.is_file_scoped:
                result.file_rules.update(directive.rules)
            else:
                target = result.statement_rules.setdefault(statement.index, set())
                target.update(directive.rules)

    return result
