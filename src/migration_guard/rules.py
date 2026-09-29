"""The rules.

Every rule here exists because the corresponding mistake has taken a database
down in production, and every one carries a ``hint`` with the concrete
alternative. A reviewer that says "this ALTER rebuilds the table" and stops has
moved the work onto the reader; the point of the tool is to end with the safe
form of the statement.

Rules are pure functions of a :class:`RuleContext`. Nothing here reads a file,
touches a clock or talks to a server, which is what lets the whole rule set be
tested against a string.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from . import statements as st
from .directives import Suppressions
from .models import NUMBER, STRING, Finding, Severity, Statement
from .sqltext import ParseResult

# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


@dataclass
class RuleContext:
    """Everything a rule may look at for one file."""

    path: str
    version: tuple[int, int, int]
    parse: ParseResult
    alters: list[st.AlterTable] = field(default_factory=list)
    dmls: list[tuple[Statement, st.DmlShape]] = field(default_factory=list)
    suppressions: Suppressions = field(default_factory=Suppressions)

    @property
    def statements(self) -> list[Statement]:
        return self.parse.statements

    @property
    def version_label(self) -> str:
        return ".".join(str(part) for part in self.version)

    def find_by_kind(self, *kinds: str) -> Iterator[Statement]:
        wanted = {kind.upper() for kind in kinds}
        for statement in self.statements:
            if statement.kind in wanted:
                yield statement

    def first_keyword_sequence(self) -> list[tuple[Statement, list[str]]]:
        return [(statement, statement.words()[:2]) for statement in self.statements]


Rule = Callable[[RuleContext], Iterator[Finding]]


def _anchor(statement: Statement) -> str:
    """A short reminder of the statement, for the finding body."""
    text = " ".join(statement.text.split())
    return text if len(text) <= 120 else text[:117] + "..."


# --------------------------------------------------------------------------- #
# Lexical hygiene
# --------------------------------------------------------------------------- #


def rule_unterminated(ctx: RuleContext) -> Iterator[Finding]:
    """SQL that does not lex cannot be reviewed."""
    for token in ctx.parse.unterminated:
        what = {
            "string": "string literal",
            "quoted_ident": "quoted identifier",
            "comment": "block comment",
            "exec_comment": "executable comment",
        }.get(token.kind, token.kind)
        yield Finding(
            rule="sql.unterminated",
            severity=Severity.CRITICAL,
            title=f"unterminated {what}",
            file=ctx.path,
            line=token.line,
            detail=(
                f"The {what} starting at column {token.column} is never closed. Every rule "
                "in this tool depends on knowing where statements end, so the rest of the "
                "file was not reviewed reliably."
            ),
            snippet=token.text[:60],
            hint="Close the literal, or check that the file was not truncated in transit.",
        )


def rule_executable_comment(ctx: RuleContext) -> Iterator[Finding]:
    """Code MySQL runs but a reviewer skims past."""
    seen: set[int] = set()
    for statement in ctx.statements:
        if not statement.has_hidden_tokens:
            continue
        for token in statement.tokens:
            if token.hidden and token.line not in seen:
                seen.add(token.line)
                yield Finding(
                    rule="file.executable-comment",
                    severity=Severity.WARNING,
                    title="code hidden inside an executable comment",
                    file=ctx.path,
                    line=token.line,
                    statement_index=statement.index,
                    statement_kind=statement.kind,
                    detail=(
                        "MySQL executes the contents of /*! ... */ and /*M! ... */ comments, "
                        "but a reviewer, a grep or a naive migration checker will not see "
                        f"them. The hidden part here is {token.text!r}."
                    ),
                    hint=(
                        "Write the option out in the open; version-gated syntax is the "
                        "migration runner's job, not the file's."
                    ),
                )
                break


def rule_use_statement(ctx: RuleContext) -> Iterator[Finding]:
    """A migration that picks its own schema fights the runner."""
    for statement in ctx.find_by_kind("USE"):
        yield Finding(
            rule="file.use-statement",
            severity=Severity.INFO,
            title="migration selects a database itself",
            file=ctx.path,
            line=statement.start_line,
            statement_index=statement.index,
            statement_kind="USE",
            detail=(
                "The migration runner decides which schema to apply to. A USE or a "
                "schema-qualified name here will silently target the wrong database when "
                "the same migration is replayed against a staging copy."
            ),
            snippet=_anchor(statement),
            hint="Drop the USE and let the runner pass the database.",
        )


def rule_delimiter_command(ctx: RuleContext) -> Iterator[Finding]:
    """DELIMITER is a client command, not SQL."""
    for token in ctx.parse.delimiters:
        yield Finding(
            rule="file.delimiter-command",
            severity=Severity.INFO,
            title=f"uses the mysql client's DELIMITER {token.value} command",
            file=ctx.path,
            line=token.line,
            detail=(
                "DELIMITER is understood by the mysql command line client only. Programmatic "
                "runners, several managed-database consoles and most CI steps will fail on "
                "it or collapse the procedure body into one statement."
            ),
            hint="Split procedure bodies into their own file, or apply them with the client.",
        )


# --------------------------------------------------------------------------- #
# Locking and online DDL
# --------------------------------------------------------------------------- #


def rule_table_rebuild(ctx: RuleContext) -> Iterator[Finding]:
    """The expensive one: InnoDB copies every row and holds the writes."""
    for alter in ctx.alters:
        offenders = [option for option in alter.structural if option.operation == st.COPY]
        if not offenders:
            continue
        first = offenders[0]
        kinds = ", ".join(sorted({option.kind for option in offenders}))
        yield Finding(
            rule="ddl.table-rebuild",
            severity=Severity.CRITICAL,
            title=f"{alter.table_name}: {kinds} rebuilds the whole table",
            file=ctx.path,
            line=first.line,
            scope=alter.table_name,
            statement_index=alter.statement.index,
            statement_kind="ALTER",
            detail=(
                f"{first.detail}. On MySQL {ctx.version_label} this copies every row while "
                "holding a write lock, so the table is unavailable for as long as that takes "
                "and the migration cannot be rolled back half way."
            ),
            snippet=first.text,
            hint=(
                "Run it with gh-ost or pt-online-schema-change, or split it: add a nullable "
                "column, backfill in batches, then change it in a maintenance window."
            ),
        )


def rule_blocks_writes(ctx: RuleContext) -> Iterator[Finding]:
    """In-place operations that still stop writes."""
    for alter in ctx.alters:
        offenders = [
            option
            for option in alter.structural
            if option.operation == st.INPLACE_BLOCKING
        ]
        if not offenders:
            continue
        first = offenders[0]
        yield Finding(
            rule="ddl.blocks-writes",
            severity=Severity.WARNING,
            title=f"{alter.table_name}: {first.kind} blocks writes while it runs",
            file=ctx.path,
            line=first.line,
            scope=alter.table_name,
            statement_index=alter.statement.index,
            statement_kind="ALTER",
            detail=f"{first.detail}. Reads continue, but every write waits.",
            snippet=first.text,
            hint="Schedule it for a low-traffic window, or use an online schema-change tool.",
        )


def rule_implicit_algorithm(ctx: RuleContext) -> Iterator[Finding]:
    """Say what you want, so MySQL fails instead of quietly rebuilding."""
    for alter in ctx.alters:
        if not alter.structural:
            continue
        if alter.worst_operation not in (st.COPY, st.INPLACE_BLOCKING, st.UNKNOWN):
            continue
        if alter.is_explicit:
            continue
        yield Finding(
            rule="ddl.implicit-algorithm",
            severity=Severity.WARNING,
            title=f"{alter.table_name}: no ALGORITHM or LOCK given",
            file=ctx.path,
            line=alter.statement.start_line,
            scope=alter.table_name,
            statement_index=alter.statement.index,
            statement_kind="ALTER",
            detail=(
                f"This ALTER can require a {st.OPERATION_LABELS[alter.worst_operation]}. "
                "Without ALGORITHM and LOCK, MySQL picks the least disruptive method it can "
                "and *silently falls back to a rebuild* when it cannot. Stating them turns "
                "that fallback into an error you can see before it reaches production."
            ),
            snippet=_anchor(alter.statement),
            hint=(
                "Add `, ALGORITHM=INPLACE, LOCK=NONE` (or ALGORITHM=INSTANT where supported) "
                "so an unsupported path fails immediately."
            ),
        )


def rule_instant_unsupported(ctx: RuleContext) -> Iterator[Finding]:
    """Asking for INSTANT where it is not available fails the migration."""
    for alter in ctx.alters:
        if not alter.declared_instant:
            continue
        unsupported = [
            option
            for option in alter.structural
            if option.operation not in (st.INSTANT,)
        ]
        if not unsupported:
            continue
        first = unsupported[0]
        yield Finding(
            rule="ddl.instant-unsupported",
            severity=Severity.WARNING,
            title=f"{alter.table_name}: ALGORITHM=INSTANT is not available for this change",
            file=ctx.path,
            line=first.line,
            scope=alter.table_name,
            statement_index=alter.statement.index,
            statement_kind="ALTER",
            detail=(
                f"{first.kind} is a {st.OPERATION_LABELS[first.operation]} on MySQL "
                f"{ctx.version_label}, not a metadata-only change. MySQL will refuse the "
                "statement, so the migration fails at deploy time rather than at review time."
            ),
            snippet=first.text,
            hint="Drop ALGORITHM=INSTANT and plan the operation as an online change instead.",
        )


def rule_no_lock_wait_timeout(ctx: RuleContext) -> Iterator[Finding]:
    """A metadata lock wait defaults to a year, and the migration just hangs.

    Reported once per file rather than once per statement: the setting is a
    property of the session, so the seventeenth copy of the same paragraph tells
    the reader nothing and buries the findings that are specific.
    """
    guarded = any(
        statement.kind == "SET" and "LOCK_WAIT_TIMEOUT" in statement.words()
        for statement in ctx.statements
    )
    if guarded:
        return

    risky = [
        alter
        for alter in ctx.alters
        if alter.structural
        and alter.worst_operation in (st.COPY, st.INPLACE_BLOCKING, st.UNKNOWN)
    ]
    if not risky:
        return

    first = risky[0]
    lines = ", ".join(str(alter.statement.start_line) for alter in risky)
    yield Finding(
        rule="ddl.no-lock-wait-timeout",
        severity=Severity.WARNING,
        title=f"{len(risky)} DDL statement(s) without a lock_wait_timeout",
        file=ctx.path,
        line=first.statement.start_line,
        scope=first.table_name,
        statement_index=first.statement.index,
        statement_kind="ALTER",
        detail=(
            f"Lines {lines}. Every ALTER needs an exclusive metadata lock at the start and "
            "end, and lock_wait_timeout defaults to 31536000 seconds -- a year. If one "
            "long-running transaction holds a lock, this migration waits instead of failing, "
            "and every query behind it waits too."
        ),
        hint=(
            "Put `SET SESSION lock_wait_timeout = 5;` at the top of the migration and let the "
            "runner retry when it times out."
        ),
    )


def rule_repeated_alter(ctx: RuleContext) -> Iterator[Finding]:
    """Several ALTERs on one table pay the rebuild cost several times.

    Only reported when at least two of them are expensive. Two cheap statements
    are often the *correct* form -- ``ADD COLUMN ... ALGORITHM=INSTANT`` and
    ``ADD INDEX ... ALGORITHM=INPLACE, LOCK=NONE`` cannot be one statement,
    because the strictest algorithm in a statement wins and INSTANT would be
    lost. Firing there would be advice that makes the migration worse.
    """
    per_table: dict[str, list[st.AlterTable]] = {}
    for alter in ctx.alters:
        per_table.setdefault(alter.table_name.lower(), []).append(alter)

    expensive_operations = (st.COPY, st.INPLACE_BLOCKING, st.UNKNOWN)
    for table, alters in per_table.items():
        if len(alters) < 2:
            continue
        expensive = [
            alter
            for alter in alters
            if alter.structural and alter.worst_operation in expensive_operations
        ]
        if len(expensive) < 2:
            continue
        lines = ", ".join(str(alter.statement.start_line) for alter in expensive)
        yield Finding(
            rule="ddl.repeated-alter",
            severity=Severity.INFO,
            title=f"{table} is rebuilt by {len(expensive)} separate statements",
            file=ctx.path,
            line=expensive[0].statement.start_line,
            scope=table,
            statement_index=expensive[0].statement.index,
            statement_kind="ALTER",
            detail=(
                f"Statements on lines {lines} each rebuild the table, so it is copied "
                "once per statement instead of once in total. MySQL 8.0 supports atomic "
                "DDL but not a multi-statement DDL transaction, so a failure half way "
                "leaves the schema partially migrated."
            ),
            hint=(
                "Combine them: `ALTER TABLE t MODIFY COLUMN a INT, MODIFY COLUMN b INT, "
                "ADD INDEX i (a)` rebuilds once."
            ),
        )


# --------------------------------------------------------------------------- #
# Destructive and structural changes
# --------------------------------------------------------------------------- #


def rule_drop_table(ctx: RuleContext) -> Iterator[Finding]:
    for statement in ctx.find_by_kind("DROP"):
        words = statement.words()
        # `DROP TABLE`, `DROP TEMPORARY TABLE` and `DROP TABLE IF EXISTS` all
        # put TABLE in the first three words; anything else (DROP INDEX,
        # DROP VIEW) is somebody else's rule.
        if "TABLE" not in words[:3]:
            continue
        temporary = "TEMPORARY" in words
        yield Finding(
            rule="ddl.drop-table",
            severity=Severity.INFO if temporary else Severity.CRITICAL,
            title=("temporary table dropped" if temporary else "DROP TABLE destroys data"),
            file=ctx.path,
            line=statement.start_line,
            statement_index=statement.index,
            statement_kind="DROP",
            detail=(
                "A temporary table disappears with the session."
                if temporary
                else "This is irreversible, and a rollback of the migration does not bring the "
                "rows back. Anything reading the table fails the moment it disappears."
            ),
            snippet=_anchor(statement),
            hint=(
                "Rename it out of the way first (`RENAME TABLE t TO t_removed_20260926`), "
                "drop it in a later release, and take a backup you have restored at least once."
            ),
        )


def rule_drop_column(ctx: RuleContext) -> Iterator[Finding]:
    for alter in ctx.alters:
        offenders = [o for o in alter.structural if o.kind == st.DROP_COLUMN]
        if not offenders:
            continue
        first = offenders[0]
        yield Finding(
            rule="ddl.drop-column",
            severity=Severity.CRITICAL,
            title=f"drops column {first.column or '(unnamed)'} from {alter.table_name}",
            file=ctx.path,
            line=first.line,
            scope=alter.table_name,
            statement_index=alter.statement.index,
            statement_kind="ALTER",
            detail=(
                f"{first.detail}. The data is gone, and an application still selecting the "
                "column fails with an unknown-column error."
            ),
            snippet=first.text,
            hint=(
                "Ship the code change first and confirm nothing reads the column, then "
                "rename it (`CHANGE COLUMN c c_dropped ...`) and drop it a release later."
            ),
        )


def rule_truncate(ctx: RuleContext) -> Iterator[Finding]:
    for statement in ctx.find_by_kind("TRUNCATE"):
        yield Finding(
            rule="ddl.truncate",
            severity=Severity.WARNING,
            title="TRUNCATE removes every row and commits immediately",
            file=ctx.path,
            line=statement.start_line,
            statement_index=statement.index,
            statement_kind="TRUNCATE",
            detail=(
                "TRUNCATE is DDL: it commits the current transaction, cannot be rolled back, "
                "and resets AUTO_INCREMENT. On a replicated topology it can also leave the "
                "replica inconsistent with the row count the application expects."
            ),
            snippet=_anchor(statement),
            hint=(
                "If the intent is to clear a table in a migration, say so in the migration "
                "name and make sure the runner is not inside a transaction."
            ),
        )


def rule_rename_table(ctx: RuleContext) -> Iterator[Finding]:
    """Catch both `RENAME TABLE` and `ALTER TABLE ... RENAME TO`.

    They are the same hazard and almost everyone writes the second one, so a
    rule that only looked at the statement keyword would have covered neither
    the common case nor the interesting one.
    """
    candidates: list[tuple[int, str]] = []
    for statement in ctx.find_by_kind("RENAME"):
        candidates.append((statement.start_line, _anchor(statement)))
    for alter in ctx.alters:
        for option in alter.structural:
            if option.kind != st.RENAME:
                continue
            candidates.append((option.line, f"{alter.table_name}: {option.text}"))

    for line, snippet in candidates:
        yield Finding(
            rule="ddl.rename-table",
            severity=Severity.WARNING,
            title="renaming a table breaks everything that names it",
            file=ctx.path,
            line=line,
            detail=(
                "Any query, view, trigger, foreign key or replication consumer still using the "
                "old name fails the instant this commits. On a replicated setup the rename is "
                "not atomic with respect to the application."
            ),
            snippet=snippet,
            hint=(
                "Create the new table, backfill, dual-write for a release, then switch reads "
                "and drop the old name."
            ),
        )


def rule_drop_index(ctx: RuleContext) -> Iterator[Finding]:
    for alter in ctx.alters:
        offenders = [o for o in alter.structural if o.kind == st.DROP_INDEX]
        for option in offenders:
            yield Finding(
                rule="ddl.drop-index",
                severity=Severity.WARNING,
                title=f"drops index {option.column or '(unnamed)'} on {alter.table_name}",
                file=ctx.path,
                line=option.line,
                scope=alter.table_name,
                statement_index=alter.statement.index,
                statement_kind="ALTER",
                detail=(
                    "Dropping an index is cheap for the schema and expensive for the queries "
                    "that were using it: they silently become full scans. A replica lagging "
                    "behind can also be pushed over by the queries that depended on it."
                ),
                snippet=option.text,
                hint=(
                    "Check the slow query log and `performance_schema.table_io_waits_summary_by_index_usage` "
                    "for the index's read count before removing it."
                ),
            )
    for statement in ctx.find_by_kind("DROP"):
        words = statement.words()
        if len(words) >= 2 and words[1] == "INDEX":
            yield Finding(
                rule="ddl.drop-index",
                severity=Severity.WARNING,
                title="drops an index",
                file=ctx.path,
                line=statement.start_line,
                statement_index=statement.index,
                statement_kind="DROP",
                detail="Queries that relied on this index silently become table scans.",
                snippet=_anchor(statement),
                hint="Confirm the index is unused before dropping it.",
            )


def rule_foreign_key(ctx: RuleContext) -> Iterator[Finding]:
    for alter in ctx.alters:
        offenders = [o for o in alter.structural if o.kind == st.ADD_FOREIGN_KEY]
        if not offenders:
            continue
        first = offenders[0]
        yield Finding(
            rule="ddl.foreign-key",
            severity=Severity.WARNING,
            title=f"{alter.table_name}: adding a foreign key validates every existing row",
            file=ctx.path,
            line=first.line,
            scope=alter.table_name,
            statement_index=alter.statement.index,
            statement_kind="ALTER",
            detail=(
                "InnoDB checks every existing row unless foreign_key_checks=0, and the child "
                "column needs its own index or the constraint creation scans the table. A "
                "foreign key also serialises writes between the two tables from then on."
            ),
            snippet=first.text,
            hint=(
                "Create the index on the child column first, then add the constraint with "
                "`SET foreign_key_checks = 0;` around it (and re-enable it afterwards)."
            ),
        )


def rule_change_charset(ctx: RuleContext) -> Iterator[Finding]:
    for alter in ctx.alters:
        offenders = [o for o in alter.structural if o.kind == st.CONVERT_CHARSET]
        if not offenders:
            continue
        first = offenders[0]
        detail = (
            "CONVERT TO CHARACTER SET rewrites every row and, if the source charset was "
            "recorded wrongly, converts the mojibake literally -- the damage is permanent."
        )
        yield Finding(
            rule="ddl.change-charset",
            severity=Severity.CRITICAL,
            title=f"{alter.table_name}: converting the character set rewrites all data",
            file=ctx.path,
            line=first.line,
            scope=alter.table_name,
            statement_index=alter.statement.index,
            statement_kind="ALTER",
            detail=detail,
            snippet=first.text,
            hint=(
                "Test on a restored copy first, and check with `SELECT HEX(col) ...` that the "
                "bytes really are in the charset you think they are."
            ),
        )


def rule_add_column_not_null(ctx: RuleContext) -> Iterator[Finding]:
    """A NOT NULL column with no default breaks inserts and replication alike."""
    for alter in ctx.alters:
        for option in alter.structural:
            if option.kind != st.ADD_COLUMN:
                continue
            group = _option_tokens(alter.statement, option)
            if not st.contains_word(group, "NOT") or not st.contains_word(group, "NULL"):
                continue
            if st.contains_word(group, "DEFAULT") or st.contains_word(group, "AUTO_INCREMENT"):
                continue
            yield Finding(
                rule="ddl.add-column-not-null",
                severity=Severity.WARNING,
                title=f"{alter.table_name}: {option.column or 'new column'} is NOT NULL with no default",
                file=ctx.path,
                line=option.line,
                scope=alter.table_name,
                statement_index=alter.statement.index,
                statement_kind="ALTER",
                detail=(
                    "Existing rows get an implicit value that depends on sql_mode, and an "
                    "INSERT that does not name the column fails outright under strict mode. "
                    "Row-based replication can then diverge between primary and replica."
                ),
                snippet=option.text,
                hint=(
                    "Add it nullable, backfill in batches, then `MODIFY ... NOT NULL` once "
                    "every row has a value."
                ),
            )


def _option_tokens(statement: Statement, option: st.AlterOption) -> list:
    """The tokens of one alter option, located by its first token's offset."""
    for index, token in enumerate(statement.significant()):
        if token.offset >= 0 and option.text[:24] in " ".join(
            item.text for item in statement.significant()[index : index + 12]
        ):
            depth = 0
            collected = []
            for item in statement.significant()[index:]:
                if item.is_punct("("):
                    depth += 1
                elif item.is_punct(")"):
                    depth -= 1
                if depth == 0 and item.is_punct(","):
                    break
                collected.append(item)
            return collected
    return statement.significant()


# --------------------------------------------------------------------------- #
# DML
# --------------------------------------------------------------------------- #


def rule_dml_without_where(ctx: RuleContext) -> Iterator[Finding]:
    for statement, shape in ctx.dmls:
        if shape.has_where:
            continue
        verb = shape.kind
        yield Finding(
            rule=f"dml.{verb.lower()}-without-where",
            severity=Severity.CRITICAL,
            title=f"{verb} without a WHERE clause touches every row",
            file=ctx.path,
            line=statement.start_line,
            scope=shape.table,
            statement_index=statement.index,
            statement_kind=verb,
            detail=(
                f"All rows in {shape.table or 'the table'} are affected. As one transaction "
                "this holds locks on everything it touches and, on row-based replication, "
                "sends every changed row to each replica -- the lag can outlast the statement "
                "by hours."
            ),
            snippet=_anchor(statement),
            hint=(
                "Batch it: repeat `... WHERE id > ? ORDER BY id LIMIT 1000` until it affects "
                "no rows, with a short sleep between batches."
            ),
        )


def rule_delete_without_limit(ctx: RuleContext) -> Iterator[Finding]:
    for statement, shape in ctx.dmls:
        if shape.kind != "DELETE" or not shape.has_where or shape.has_limit:
            continue
        yield Finding(
            rule="dml.delete-without-limit",
            severity=Severity.INFO,
            title="DELETE has a WHERE but no LIMIT",
            file=ctx.path,
            line=statement.start_line,
            scope=shape.table,
            statement_index=statement.index,
            statement_kind="DELETE",
            detail=(
                "An unbounded DELETE holds its locks until it finishes and can be very long "
                "on a large table even with an index on the predicate."
            ),
            snippet=_anchor(statement),
            hint="Add `LIMIT 1000` and loop until the affected-row count is zero.",
        )


def rule_unbounded_predicate(ctx: RuleContext) -> Iterator[Finding]:
    for statement, shape in ctx.dmls:
        if not shape.has_where:
            continue
        reasons = st.unbounded_predicate_reasons(shape.predicate)
        if not reasons:
            continue
        yield Finding(
            rule="dml.unbounded-predicate",
            severity=Severity.WARNING,
            title=f"{shape.kind} predicate may scan the whole table",
            file=ctx.path,
            line=statement.start_line,
            scope=shape.table,
            statement_index=statement.index,
            statement_kind=shape.kind,
            detail=(
                "; ".join(reasons)
                + ". Whether the optimizer can still use an index depends on the schema, "
                "which this run did not read."
            ),
            snippet=_anchor(statement),
            hint="Confirm with EXPLAIN on a restored copy before running it for real.",
        )


# --------------------------------------------------------------------------- #
# Session settings
# --------------------------------------------------------------------------- #


def rule_foreign_key_checks_off(ctx: RuleContext) -> Iterator[Finding]:
    for statement in ctx.statements:
        if statement.kind != "SET":
            continue
        words = statement.words()
        if "FOREIGN_KEY_CHECKS" not in words:
            continue
        if not any(
            token.kind == NUMBER and token.value == "0" for token in statement.tokens
        ):
            continue
        yield Finding(
            rule="session.foreign-key-checks-off",
            severity=Severity.CRITICAL,
            title="foreign_key_checks is disabled",
            file=ctx.path,
            line=statement.start_line,
            statement_index=statement.index,
            statement_kind="SET",
            detail=(
                "With the checks off, rows that violate a foreign key are accepted. The "
                "violations are not reported later -- they surface as a query returning "
                "nothing, months afterwards -- and a migration that forgets to re-enable the "
                "setting leaves every later statement unprotected."
            ),
            snippet=_anchor(statement),
            hint=(
                "Keep the window as small as possible, re-enable it immediately after, and "
                "run `pt-archiver`/a manual join to look for orphans before re-enabling."
            ),
        )


def rule_sql_mode_relaxed(ctx: RuleContext) -> Iterator[Finding]:
    for statement in ctx.statements:
        if statement.kind != "SET":
            continue
        if "SQL_MODE" not in statement.words():
            continue
        stripped = any(
            token.kind == STRING and token.value.strip() == "" for token in statement.tokens
        )
        if not stripped and not st.contains_word(statement.tokens, "NO_ENGINE_SUBSTITUTION"):
            continue
        yield Finding(
            rule="session.sql-mode-relaxed",
            severity=Severity.WARNING,
            title="sql_mode is relaxed inside the migration",
            file=ctx.path,
            line=statement.start_line,
            statement_index=statement.index,
            statement_kind="SET",
            detail=(
                "Clearing sql_mode disables strict mode for the rest of the session: "
                "truncated values, invalid dates and out-of-range numbers are accepted with a "
                "warning that nothing reads."
            ),
            snippet=_anchor(statement),
            hint="Set only the flags you need, and restore the previous value afterwards.",
        )


# --------------------------------------------------------------------------- #
# Suppression hygiene
# --------------------------------------------------------------------------- #


KNOWN_RULE_IDS: tuple[str, ...] = ()


def rule_directive_unknown(ctx: RuleContext) -> Iterator[Finding]:
    for directive in ctx.suppressions.all:
        if not directive.rules:
            yield Finding(
                rule="directive.empty",
                severity=Severity.WARNING,
                title=f"{directive.action}() names no rule",
                file=ctx.path,
                line=directive.line,
                detail=(
                    "The directive parses but silences nothing, which usually means the rule "
                    "id was deleted during editing."
                ),
                snippet=directive.source[:80],
                hint="Remove the directive, or name the rule it was meant to silence.",
            )
            continue
        unknown = [name for name in directive.rules if name not in KNOWN_RULE_IDS]
        if unknown:
            yield Finding(
                rule="directive.unknown-rule",
                severity=Severity.WARNING,
                title=f"suppression names unknown rule(s): {', '.join(unknown)}",
                file=ctx.path,
                line=directive.line,
                detail=(
                    "A misspelled rule id silences nothing at all, so the finding it was meant "
                    "to hide still fires -- or worse, the author believes it was suppressed."
                ),
                snippet=directive.source[:80],
                hint="Run `migration-guard --list-rules` for the exact identifiers.",
            )


def rule_directive_missing_reason(ctx: RuleContext) -> Iterator[Finding]:
    for directive in ctx.suppressions.all:
        if directive.reason:
            continue
        yield Finding(
            rule="directive.missing-reason",
            severity=Severity.INFO,
            title=f"{directive.action}() gives no reason",
            file=ctx.path,
            line=directive.line,
            detail=(
                "The next person to read this file cannot tell whether the exception is still "
                "valid, so it will outlive the situation that justified it."
            ),
            snippet=directive.source[:80],
            hint="Add `reason: ...` after the rule list.",
        )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

RULES: tuple[Rule, ...] = (
    rule_unterminated,
    rule_table_rebuild,
    rule_drop_table,
    rule_drop_column,
    rule_change_charset,
    rule_dml_without_where,
    rule_foreign_key_checks_off,
    rule_blocks_writes,
    rule_implicit_algorithm,
    rule_instant_unsupported,
    rule_no_lock_wait_timeout,
    rule_add_column_not_null,
    rule_truncate,
    rule_rename_table,
    rule_drop_index,
    rule_foreign_key,
    rule_unbounded_predicate,
    rule_sql_mode_relaxed,
    rule_executable_comment,
    rule_directive_unknown,
    rule_repeated_alter,
    rule_delete_without_limit,
    rule_use_statement,
    rule_delimiter_command,
    rule_directive_missing_reason,
)

#: Every identifier a rule can emit. Kept explicit so ``--list-rules`` and the
#: ``allow(...)`` validator can never disagree with the code.
RULE_IDS: tuple[str, ...] = (
    "ddl.add-column-not-null",
    "ddl.blocks-writes",
    "ddl.change-charset",
    "ddl.drop-column",
    "ddl.drop-index",
    "ddl.drop-table",
    "ddl.foreign-key",
    "ddl.implicit-algorithm",
    "ddl.instant-unsupported",
    "ddl.no-lock-wait-timeout",
    "ddl.repeated-alter",
    "ddl.rename-table",
    "ddl.table-rebuild",
    "ddl.truncate",
    "dml.delete-without-limit",
    "dml.delete-without-where",
    "dml.unbounded-predicate",
    "dml.update-without-where",
    "directive.empty",
    "directive.missing-reason",
    "directive.unknown-rule",
    "file.delimiter-command",
    "file.executable-comment",
    "file.use-statement",
    "session.foreign-key-checks-off",
    "session.sql-mode-relaxed",
    "sql.unterminated",
)

KNOWN_RULE_IDS = RULE_IDS
