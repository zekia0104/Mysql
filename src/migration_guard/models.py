"""Core data types shared by parsing, analysis and rendering.

A review is built from three layers:

``Token``      one lexical unit with an exact source position
``Statement``  one SQL statement, with its tokens, comments and line range
``Finding``    one rule violation, anchored to a file and a line

Every finding carries the line it came from. A linter that says "something in
this 400-line migration is dangerous" has moved the work onto the reader; the
whole point of this tool is to point at the statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    """Ordered so that ``max()`` over severities is meaningful."""

    OK = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name

    @classmethod
    def coerce(cls, value: Severity | str | int) -> Severity:
        if isinstance(value, Severity):
            return value
        if isinstance(value, str):
            return cls[value.strip().upper()]
        return cls(value)


#: Exit codes are the public contract: they let the tool gate a migration
#: without anyone parsing its output. Deliberately identical to sysprobe's, so
#: the two tools behave the same way in a pipeline.
EXIT_OK = 0
EXIT_WARNING = 1
EXIT_CRITICAL = 2
EXIT_ERROR = 3

EXIT_CODE_BY_SEVERITY = {
    Severity.OK: EXIT_OK,
    Severity.INFO: EXIT_OK,
    Severity.WARNING: EXIT_WARNING,
    Severity.CRITICAL: EXIT_CRITICAL,
}


def exit_code_for(findings: list[Finding]) -> int:
    """Worst finding wins; INFO is advisory and never fails a build."""
    worst = Severity.OK
    for finding in findings:
        if finding.severity > worst:
            worst = finding.severity
    return EXIT_CODE_BY_SEVERITY[worst]


# --------------------------------------------------------------------------- #
# Lexical layer
# --------------------------------------------------------------------------- #

# Token kinds.
WORD = "word"
NUMBER = "number"
PUNCT = "punct"
STRING = "string"
QUOTED_IDENT = "quoted_ident"
COMMENT = "comment"
EXEC_COMMENT = "exec_comment"
DELIMITER_CMD = "delimiter"
#: The character sequence that ended a statement. A distinct kind rather than a
#: ``;`` check, because ``DELIMITER $$`` makes ``$$`` the terminator and ``$$``
#: would otherwise lex as an identifier.
TERMINATOR = "terminator"


@dataclass(frozen=True)
class Token:
    """One lexical unit, with the position needed to point a finding at it.

    ``line`` and ``column`` are 1-based and refer to the token's first
    character, so an editor can jump straight to it. ``offset`` is the absolute
    character index in the source, which is what lets a statement carry its own
    raw text without re-scanning the file.
    """

    kind: str
    text: str
    value: str
    line: int
    column: int
    offset: int = 0
    #: True when the token came from inside an executable comment (``/*! ... */``).
    #: MySQL runs that code, so the analysis must see it -- but a reviewer
    #: reading the file may not, which is itself worth reporting.
    hidden: bool = False
    #: False for a string, identifier or comment that was never closed. SQL
    #: that does not lex is worth saying out loud rather than silently
    #: analysing as if it did.
    terminated: bool = True

    @property
    def upper(self) -> str:
        return self.value.upper()

    @property
    def end_offset(self) -> int:
        return self.offset + len(self.text)

    @property
    def is_comment(self) -> bool:
        return self.kind in (COMMENT, EXEC_COMMENT)

    def is_word(self, *candidates: str) -> bool:
        return self.kind == WORD and self.upper in candidates

    def is_punct(self, *candidates: str) -> bool:
        return self.kind == PUNCT and self.value in candidates

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        flags = ""
        if self.hidden:
            flags += " hidden"
        if not self.terminated:
            flags += " unterminated"
        return f"Token({self.kind} {self.value!r} @{self.line}:{self.column}{flags})"


@dataclass
class Statement:
    """One SQL statement together with everything a rule might need."""

    index: int
    #: Token stream with comments removed; executable-comment contents included
    #: but marked ``hidden``.
    tokens: list[Token]
    #: Comments that belong to this statement (leading or trailing).
    comments: list[Token] = field(default_factory=list)
    #: Raw source text of the statement, comments included.
    text: str = ""
    start_line: int = 0
    end_line: int = 0

    @property
    def kind(self) -> str:
        """The leading keyword, uppercased: ``ALTER``, ``CREATE``, ``UPDATE``..."""
        for token in self.tokens:
            if token.kind == WORD:
                return token.upper
        return "UNKNOWN"

    @property
    def keywords(self) -> list[str]:
        return [token.upper for token in self.tokens if token.kind == WORD]

    @property
    def has_hidden_tokens(self) -> bool:
        return any(token.hidden for token in self.tokens)

    def words(self) -> list[str]:
        return self.keywords

    def significant(self) -> list[Token]:
        """Tokens with all comments stripped (executable comments already are)."""
        return [token for token in self.tokens if not token.is_comment]

    def first_line(self) -> int:
        return self.start_line or (self.tokens[0].line if self.tokens else 0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Statement(#{self.index} {self.kind} line={self.start_line})"


# --------------------------------------------------------------------------- #
# Findings and the report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Finding:
    """One rule violation, anchored to a file and a line.

    ``rule`` is a stable identifier (``ddl.table-rebuild``) so that downstream
    tooling, suppression directives and documentation can all refer to a check
    without depending on its wording.
    """

    rule: str
    severity: Severity
    title: str
    file: str
    line: int
    detail: str = ""
    scope: str = ""
    statement_index: int = 0
    statement_kind: str = ""
    snippet: str = ""
    value: float | None = None
    threshold: float | None = None
    unit: str = ""
    hint: str = ""

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity.label,
            "title": self.title,
            "file": self.file,
            "line": self.line,
            "column": 1,
            "detail": self.detail,
            "scope": self.scope,
            "statement_index": self.statement_index,
            "statement_kind": self.statement_kind,
            "snippet": self.snippet,
            "value": self.value,
            "threshold": self.threshold,
            "unit": self.unit,
            "hint": self.hint,
        }


@dataclass
class FileReview:
    """What was found in one input file."""

    path: str
    statement_count: int = 0
    findings: list[Finding] = field(default_factory=list)
    #: Non-fatal problems reading the file (unreadable, empty, not SQL).
    notes: list[str] = field(default_factory=list)

    @property
    def worst(self) -> Severity:
        return max((f.severity for f in self.findings), default=Severity.OK)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "statement_count": self.statement_count,
            "status": self.worst.label,
            "notes": list(self.notes),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class Report:
    tool: str
    version: str
    generated_at: str
    target_version: str
    files: list[FileReview] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Schema and metadata sources that were used, for the JSON document.
    sources: dict[str, Any] = field(default_factory=dict)

    @property
    def findings(self) -> list[Finding]:
        return [finding for review in self.files for finding in review.findings]

    @property
    def statement_count(self) -> int:
        return sum(review.statement_count for review in self.files)

    @property
    def status(self) -> Severity:
        return max((review.worst for review in self.files), default=Severity.OK)

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.findings)

    def counts(self) -> dict[Severity, int]:
        counts = {severity: 0 for severity in Severity}
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "tool": self.tool,
            "version": self.version,
            "generated_at": self.generated_at,
            "target_version": self.target_version,
            "status": self.status.label,
            "exit_code": self.exit_code,
            "statement_count": self.statement_count,
            "counts": {
                "critical": counts[Severity.CRITICAL],
                "warning": counts[Severity.WARNING],
                "info": counts[Severity.INFO],
            },
            "sources": self.sources,
            "notes": list(self.notes),
            "files": [review.to_dict() for review in self.files],
            "findings": [finding.to_dict() for finding in self.findings],
        }
