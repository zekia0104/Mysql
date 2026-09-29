"""Statement shapes: what an ``ALTER TABLE`` actually asks InnoDB to do.

The valuable part of this project is not "you wrote an ALTER" but "this ALTER
makes InnoDB rebuild a 12-million-row table while holding the writes". Getting
that right means classifying every alter option against the online-DDL
behaviour of the *target server version*, because the same statement is instant
on 8.0.29 and a full rebuild on 5.7.

The matrix below follows MySQL's "Online DDL Operations" tables. Where the
documentation is conditional -- an operation is in-place unless the table has a
full-text index, say -- the classification deliberately takes the *worst*
plausible outcome. A review tool that understates a lock is worse than one that
overstates it, because the failure mode is a production incident rather than an
annoying warning.

Anything not covered is reported as UNKNOWN rather than guessed at, and a rule
surfaces that so the reader knows the tool did not check it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import NUMBER, PUNCT, QUOTED_IDENT, WORD, Statement, Token

#: Token kinds that denote a name: a bare word or a backtick-quoted identifier.
#: ``ALTER TABLE `orders` ...`` is the most common spelling there is, so a
#: reader that only accepted WORD tokens would silently review nothing.
IDENTIFIER_KINDS = (WORD, QUOTED_IDENT)

# --------------------------------------------------------------------------- #
# Operation classes
# --------------------------------------------------------------------------- #

#: Metadata-only change: no rebuild, no DML block, effectively instantaneous.
INSTANT = "instant"
#: Rebuild-free and allows concurrent reads and writes.
INPLACE_ONLINE = "inplace-online"
#: Rebuild-free but blocks DML for the duration, unless the tool is careful.
INPLACE_BLOCKING = "inplace-blocking"
#: Copies the whole table. Blocks writes for as long as that takes.
COPY = "copy"
#: Not recognised. Reported rather than assumed.
UNKNOWN = "unknown"

OPERATION_LABELS = {
    INSTANT: "metadata only",
    INPLACE_ONLINE: "in-place, concurrent DML allowed",
    INPLACE_BLOCKING: "in-place, but blocks writes",
    COPY: "full table rebuild",
    UNKNOWN: "not determined",
}

#: Operations that need a table rebuild, worst first for reporting.
REBUILDING_OPERATIONS = frozenset({COPY})
BLOCKING_OPERATIONS = frozenset({COPY, INPLACE_BLOCKING})

# Alter option kinds.
ADD_COLUMN = "ADD COLUMN"
ADD_INDEX = "ADD INDEX"
ADD_PRIMARY_KEY = "ADD PRIMARY KEY"
ADD_FOREIGN_KEY = "ADD FOREIGN KEY"
ADD_CONSTRAINT_OTHER = "ADD CONSTRAINT"
DROP_COLUMN = "DROP COLUMN"
DROP_INDEX = "DROP INDEX"
DROP_PRIMARY_KEY = "DROP PRIMARY KEY"
DROP_FOREIGN_KEY = "DROP FOREIGN KEY"
MODIFY_COLUMN = "MODIFY COLUMN"
SET_DEFAULT = "ALTER COLUMN"
RENAME = "RENAME"
CONVERT_CHARSET = "CONVERT TO CHARACTER SET"
CHANGE_ENGINE = "ENGINE"
CHANGE_ROW_FORMAT = "ROW_FORMAT"
SET_AUTO_INCREMENT = "AUTO_INCREMENT"
PARTITION = "PARTITION"
FORCE_REBUILD = "FORCE"
ORDER_BY = "ORDER BY"
KEYS_TOGGLE = "KEYS"
ALGORITHM_SETTING = "ALGORITHM"
LOCK_SETTING = "LOCK"
VALIDATION_SETTING = "VALIDATION"
UNRECOGNISED = "UNRECOGNISED"

_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int]:
    """Turn ``"8.0.29"`` into ``(8, 0, 29)``; unparsable input becomes ``(8, 0, 0)``.

    Defaulting to a recent 8.0 release rather than to 0 matters: it is the
    version almost everyone runs, and treating it as ancient would flag
    perfectly safe ``ADD COLUMN`` statements.
    """
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        return (8, 0, 0)
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


@dataclass(frozen=True)
class AlterOption:
    """One comma-separated clause of an ``ALTER TABLE``."""

    kind: str
    text: str
    line: int
    operation: str
    detail: str = ""
    column: str = ""
    hidden: bool = False
    #: For index definitions: the ``(column, prefix_length)`` pairs, which the
    #: schema-aware rules need in order to compare against what exists.
    column_specs: tuple[tuple[str, int | None], ...] = ()
    #: For column definitions: the declared type, taken from the tokens rather
    #: than re-parsed out of ``text``, so the schema-aware rules compare the
    #: real type even when the statement's spacing is unusual.
    type_text: str = ""

    @property
    def is_setting(self) -> bool:
        """``ALGORITHM=`` / ``LOCK=`` / ``WITH VALIDATION`` are modifiers."""
        return self.kind in (ALGORITHM_SETTING, LOCK_SETTING, VALIDATION_SETTING)

    @property
    def is_structural(self) -> bool:
        return not self.is_setting and self.kind != KEYS_TOGGLE


@dataclass
class AlterTable:
    """A parsed ``ALTER TABLE`` with its options classified."""

    statement: Statement
    table: str
    schema: str = ""
    options: list[AlterOption] = field(default_factory=list)
    algorithm: str = ""
    lock: str = ""

    @property
    def table_name(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table

    @property
    def structural(self) -> list[AlterOption]:
        return [option for option in self.options if option.is_structural]

    @property
    def worst_operation(self) -> str:
        order = [UNKNOWN, INSTANT, INPLACE_ONLINE, INPLACE_BLOCKING, COPY]
        worst = INSTANT
        for option in self.structural:
            if order.index(option.operation) > order.index(worst):
                worst = option.operation
        return worst

    @property
    def rebuilds(self) -> bool:
        return self.worst_operation in REBUILDING_OPERATIONS

    @property
    def blocks_writes(self) -> bool:
        return self.worst_operation in BLOCKING_OPERATIONS

    @property
    def is_explicit(self) -> bool:
        """Whether the author stated ALGORITHM and LOCK."""
        return bool(self.algorithm) and bool(self.lock)

    @property
    def declared_instant(self) -> bool:
        return self.algorithm.upper() == "INSTANT"

    @property
    def asks_for_no_locking(self) -> bool:
        return self.lock.upper() == "NONE"

    def option_kinds(self) -> set[str]:
        return {option.kind for option in self.structural}


# --------------------------------------------------------------------------- #
# Online-DDL classification
# --------------------------------------------------------------------------- #


def classify_add_column(version: tuple[int, int, int], *, positioned: bool) -> str:
    """``ADD COLUMN`` is instant only on recent 8.0, and only sometimes.

    * 8.0.12 made adding a column metadata-only when it is appended last.
    * 8.0.29 extended that to any position, and made ``DROP COLUMN`` instant too.
    * 5.7 and 8.0.0-8.0.11 rebuild the table, which is the case that has taken
      production down for a decade.
    """
    major, minor, patch = version
    if major < 8:
        return COPY
    if (minor, patch) >= (0, 29):
        return INSTANT
    if (minor, patch) >= (0, 12):
        return COPY if positioned else INSTANT
    return COPY


def classify_drop_column(version: tuple[int, int, int]) -> str:
    major, minor, patch = version
    if major > 8 or (major == 8 and (minor, patch) >= (0, 29)):
        return INSTANT
    return COPY


def classify_alter_option(
    kind: str,
    version: tuple[int, int, int],
    *,
    positioned: bool = False,
    unique: bool = False,
    fulltext: bool = False,
    spatial: bool = False,
) -> tuple[str, str]:
    """Return ``(operation, detail)`` for one alter option.

    ``detail`` explains the verdict in the words a reader wants, because
    "full table rebuild" alone does not tell anyone what to change.
    """
    major, minor, patch = version
    modern = major > 8 or (major == 8 and (minor, patch) >= (0, 29))
    has_instant = major == 8 and (minor, patch) >= (0, 12)

    if kind == ADD_COLUMN:
        operation = classify_add_column(version, positioned=positioned)
        if operation is INSTANT:
            return operation, "metadata-only change on this server version"
        if positioned:
            return operation, (
                "ADD COLUMN with a position rebuilds the table before 8.0.29; "
                "appending the column at the end is metadata-only"
            )
        return operation, (
            f"ADD COLUMN rebuilds the table on {major}.{minor}; "
            "8.0.12 and later make it metadata-only"
        )

    if kind == DROP_COLUMN:
        operation = classify_drop_column(version)
        if operation is INSTANT:
            return operation, "metadata-only change on this server version"
        return operation, (
            f"DROP COLUMN rebuilds the table on {major}.{minor}; "
            "8.0.29 and later make it metadata-only"
        )

    if kind == ADD_INDEX:
        if fulltext or spatial:
            return INPLACE_BLOCKING, (
                "the first full-text or spatial index on a table blocks writes while it is built"
            )
        return INPLACE_ONLINE, "index build proceeds while the table stays writable"

    if kind == DROP_INDEX:
        return INPLACE_ONLINE, "the index is dropped without touching the rows"

    if kind in (ADD_PRIMARY_KEY, DROP_PRIMARY_KEY):
        return COPY, "changing the primary key reorganises the clustered index"

    if kind == ADD_FOREIGN_KEY:
        return INPLACE_ONLINE, (
            "a foreign key needs an index on the child column; with "
            "foreign_key_checks=0 the check is skipped and it is much faster"
        )

    if kind == DROP_FOREIGN_KEY:
        return INPLACE_ONLINE, "the constraint is removed without touching the rows"

    if kind == ADD_CONSTRAINT_OTHER:
        return INPLACE_BLOCKING, "CHECK and similar constraints are validated on add"

    if kind == MODIFY_COLUMN:
        return COPY, (
            "changing a column definition usually copies every row; "
            "only pure NULL/NOT NULL or DEFAULT changes avoid a rebuild"
        )

    if kind == SET_DEFAULT:
        return INPLACE_ONLINE, "changing only the default does not touch existing rows"

    if kind in (CONVERT_CHARSET, CHANGE_ENGINE, CHANGE_ROW_FORMAT, FORCE_REBUILD, ORDER_BY):
        return COPY, "the storage of every row is rewritten"

    if kind == RENAME:
        return INPLACE_ONLINE, "renaming does not touch the rows"

    if kind == SET_AUTO_INCREMENT:
        return INPLACE_ONLINE, "the counter is updated in the data dictionary"

    if kind == PARTITION:
        return INPLACE_ONLINE, "partition maintenance moves whole partitions, not rows"

    if kind == KEYS_TOGGLE:
        return INPLACE_ONLINE, "MyISAM index toggling; no effect on InnoDB"

    if kind in (ALGORITHM_SETTING, LOCK_SETTING, VALIDATION_SETTING):
        return INSTANT, "statement modifier, not a change"

    _ = has_instant, modern
    return UNKNOWN, "this alter option is not covered by the online-DDL matrix"


# --------------------------------------------------------------------------- #
# Token helpers
# --------------------------------------------------------------------------- #


def split_top_level(tokens: list[Token], separator: str = ",") -> list[list[Token]]:
    """Split on ``separator`` at paren depth zero.

    ``ADD INDEX i (a, b), ADD COLUMN c INT`` must become two options, not
    three, so commas inside an index definition or a column type such as
    ``DECIMAL(10, 2)`` have to be ignored.
    """
    groups: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for token in tokens:
        if token.kind == PUNCT and token.value == "(":
            depth += 1
        elif token.kind == PUNCT and token.value == ")":
            depth = max(depth - 1, 0)
        if depth == 0 and token.kind == PUNCT and token.value == separator:
            if current:
                groups.append(current)
            current = []
            continue
        current.append(token)
    if current:
        groups.append(current)
    return groups


def identifier_of(token: Token | None) -> str:
    """The name a token denotes, quoted or not."""
    return token.value if token is not None else ""


def join_tokens(tokens: list[Token]) -> str:
    """Reassemble tokens into readable SQL, with single spaces.

    Used for the snippet shown in a finding. It is deliberately not a
    formatter: the point is that the reader recognises their own statement, not
    that the whitespace is preserved exactly.
    """
    parts: list[str] = []
    for token in tokens:
        if token.kind in (PUNCT,) and token.value in (",", ")", ";"):
            if parts:
                parts[-1] = parts[-1] + token.value
            else:
                parts.append(token.value)
            continue
        if token.kind == PUNCT and token.value == "(":
            if parts:
                parts[-1] = parts[-1] + "("
            else:
                parts.append("(")
            continue
        if token.kind == PUNCT and token.value == ".":
            if parts:
                parts[-1] = parts[-1] + "."
            continue
        if parts and not parts[-1].endswith(("(", ".")):
            parts.append(" ")
        parts.append(token.text)
    return "".join(parts).strip()


def qualified_name(tokens: list[Token], start: int) -> tuple[str, str, int]:
    """Read ``[schema.]table`` at ``start``; return ``(schema, table, next_index)``."""
    parts: list[str] = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token.kind in IDENTIFIER_KINDS:
            parts.append(token.value)
            index += 1
            if index < len(tokens) and tokens[index].is_punct("."):
                index += 1
                continue
            break
        break
    if len(parts) >= 2:
        return parts[-2], parts[-1], index
    if parts:
        return "", parts[0], index
    return "", "", index


def matching_paren(tokens: list[Token], opening: int | None) -> int | None:
    """Index of the ``)`` that closes the ``(`` at ``opening``, or ``None``."""
    if opening is None or opening >= len(tokens):
        return None
    depth = 0
    for index in range(opening, len(tokens)):
        token = tokens[index]
        if token.kind == PUNCT and token.value == "(":
            depth += 1
        elif token.kind == PUNCT and token.value == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def index_columns_from(group: list[Token]) -> list[tuple[str, int | None]]:
    """``(column, prefix_length)`` pairs from an index definition's parentheses.

    Handles ``(a, b)``, ``(a(10), b)`` and ``(a ASC, b DESC)``, because all three
    appear in real dumps and a prefix length changes whether InnoDB will accept
    the index at all.
    """
    opening = next((i for i, token in enumerate(group) if token.is_punct("(")), None)
    closing = matching_paren(group, opening)
    if opening is None or closing is None:
        return []

    found: list[tuple[str, int | None]] = []
    for part in split_top_level(group[opening + 1 : closing]):
        name = ""
        prefix: int | None = None
        for index, token in enumerate(part):
            if not name:
                if token.kind not in IDENTIFIER_KINDS:
                    break
                if token.kind == WORD and token.upper in ("ASC", "DESC"):
                    break
                name = token.value
                continue
            if token.kind == PUNCT and token.value == "(":
                for later in part[index + 1 :]:
                    if later.kind == NUMBER:
                        prefix = int(later.value)
                        break
                break
            if token.kind == WORD and token.upper in ("ASC", "DESC"):
                break
        if name:
            found.append((name, prefix))
    return found


def tokens_after(tokens: list[Token], keyword: str) -> list[Token]:
    """Everything after the first top-level occurrence of ``keyword``."""
    depth = 0
    for index, token in enumerate(tokens):
        if token.kind == PUNCT and token.value == "(":
            depth += 1
        elif token.kind == PUNCT and token.value == ")":
            depth = max(depth - 1, 0)
        elif depth == 0 and token.kind == WORD and token.upper == keyword:
            return tokens[index + 1 :]
    return []


def contains_word(tokens: list[Token], *keywords: str) -> bool:
    wanted = {keyword.upper() for keyword in keywords}
    return any(token.kind == WORD and token.upper in wanted for token in tokens)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_alter_table(statement: Statement, version: tuple[int, int, int]) -> AlterTable | None:
    """Parse an ``ALTER TABLE`` statement, or return ``None`` for anything else."""
    tokens = statement.significant()
    if len(tokens) < 3:
        return None
    if not (tokens[0].is_word("ALTER") and tokens[1].is_word("TABLE")):
        return None

    schema, table, index = qualified_name(tokens, 2)
    if not table:
        return None

    tail = tokens[index:]
    result = AlterTable(statement=statement, table=table, schema=schema)

    for group in split_top_level(tail):
        option = _classify_group(group, version)
        if option is None:
            continue
        if option.kind == ALGORITHM_SETTING:
            result.algorithm = option.text.split("=", 1)[-1].strip()
            continue
        if option.kind == LOCK_SETTING:
            result.lock = option.text.split("=", 1)[-1].strip()
            continue
        result.options.append(option)

    return result


def _classify_group(group: list[Token], version: tuple[int, int, int]) -> AlterOption | None:
    """Turn one comma-separated clause into a classified :class:`AlterOption`."""
    words = [token.upper for token in group if token.kind == WORD]
    if not words:
        return None
    line = group[0].line
    hidden = any(token.hidden for token in group)
    text = join_tokens(group)

    # Statement modifiers first: they change how the rest is executed.
    if words[0] == "ALGORITHM":
        value = _value_after_equals(group)
        return AlterOption(ALGORITHM_SETTING, text, line, INSTANT, value, hidden=hidden)
    if words[0] == "LOCK":
        value = _value_after_equals(group)
        return AlterOption(LOCK_SETTING, text, line, INSTANT, value, hidden=hidden)
    if words[0] == "WITH" and "VALIDATION" in words:
        return AlterOption(VALIDATION_SETTING, text, line, INSTANT, "", hidden=hidden)

    if words[0] == "ADD":
        return _classify_add(group, words, text, line, version, hidden)
    if words[0] == "DROP":
        return _classify_drop(group, words, text, line, version, hidden)
    if words[0] in ("MODIFY", "CHANGE"):
        column = _identifier_after(group, {"MODIFY", "CHANGE", "COLUMN"})
        operation, detail = classify_alter_option(MODIFY_COLUMN, version)
        return AlterOption(
            MODIFY_COLUMN,
            text,
            line,
            operation,
            detail,
            column,
            hidden,
            (),
            declared_type(group, column),
        )
    if words[0] == "ALTER":
        column = _identifier_after(group, {"ALTER", "COLUMN", "SET", "DROP", "DEFAULT"})
        operation, detail = classify_alter_option(SET_DEFAULT, version)
        return AlterOption(SET_DEFAULT, text, line, operation, detail, column, hidden)
    if words[0] == "RENAME":
        operation, detail = classify_alter_option(RENAME, version)
        return AlterOption(RENAME, text, line, operation, detail, hidden=hidden)
    if words[0] == "CONVERT":
        operation, detail = classify_alter_option(CONVERT_CHARSET, version)
        return AlterOption(CONVERT_CHARSET, text, line, operation, detail, hidden=hidden)
    if words[0] == "ENGINE":
        operation, detail = classify_alter_option(CHANGE_ENGINE, version)
        return AlterOption(CHANGE_ENGINE, text, line, operation, detail, hidden=hidden)
    if words[0] == "ROW_FORMAT":
        operation, detail = classify_alter_option(CHANGE_ROW_FORMAT, version)
        return AlterOption(CHANGE_ROW_FORMAT, text, line, operation, detail, hidden=hidden)
    if words[0] == "AUTO_INCREMENT":
        operation, detail = classify_alter_option(SET_AUTO_INCREMENT, version)
        return AlterOption(SET_AUTO_INCREMENT, text, line, operation, detail, hidden=hidden)
    if words[0] == "PARTITION":
        # `ALTER TABLE t PARTITION BY ...` repartitions: every row is rewritten.
        return AlterOption(
            PARTITION, text, line, COPY, "repartitioning rewrites every row", hidden=hidden
        )
    if words[0] == "FORCE":
        operation, detail = classify_alter_option(FORCE_REBUILD, version)
        return AlterOption(FORCE_REBUILD, text, line, operation, detail, hidden=hidden)
    if words[0] == "ORDER":
        operation, detail = classify_alter_option(ORDER_BY, version)
        return AlterOption(ORDER_BY, text, line, operation, detail, hidden=hidden)
    if words[0] in ("DISABLE", "ENABLE") and "KEYS" in words:
        operation, detail = classify_alter_option(KEYS_TOGGLE, version)
        return AlterOption(KEYS_TOGGLE, text, line, operation, detail, hidden=hidden)

    return AlterOption(UNRECOGNISED, text, line, UNKNOWN, "not covered by the matrix", hidden=hidden)


def _classify_add(
    group: list[Token],
    words: list[str],
    text: str,
    line: int,
    version: tuple[int, int, int],
    hidden: bool,
) -> AlterOption:
    rest = words[1:]
    # `ADD COLUMN c INT` and `ADD c INT` are the same thing; COLUMN is optional.
    if rest and rest[0] == "COLUMN":
        rest = rest[1:]
    if rest and rest[0] in ("INDEX", "KEY"):
        operation, detail = classify_alter_option(ADD_INDEX, version)
        return AlterOption(
            ADD_INDEX,
            text,
            line,
            operation,
            detail,
            _index_name(group),
            hidden,
            tuple(index_columns_from(group)),
        )

    if rest and rest[0] in ("UNIQUE", "FULLTEXT", "SPATIAL"):
        operation, detail = classify_alter_option(
            ADD_INDEX,
            version,
            unique=rest[0] == "UNIQUE",
            fulltext=rest[0] == "FULLTEXT",
            spatial=rest[0] == "SPATIAL",
        )
        return AlterOption(
            ADD_INDEX,
            text,
            line,
            operation,
            detail,
            _index_name(group),
            hidden,
            tuple(index_columns_from(group)),
        )

    if rest and rest[0] == "PRIMARY":
        operation, detail = classify_alter_option(ADD_PRIMARY_KEY, version)
        return AlterOption(ADD_PRIMARY_KEY, text, line, operation, detail, hidden=hidden)

    if rest and rest[0] == "CONSTRAINT":
        if "FOREIGN" in rest:
            operation, detail = classify_alter_option(ADD_FOREIGN_KEY, version)
            return AlterOption(ADD_FOREIGN_KEY, text, line, operation, detail, hidden=hidden)
        operation, detail = classify_alter_option(ADD_CONSTRAINT_OTHER, version)
        return AlterOption(ADD_CONSTRAINT_OTHER, text, line, operation, detail, hidden=hidden)

    if rest and rest[0] == "FOREIGN":
        operation, detail = classify_alter_option(ADD_FOREIGN_KEY, version)
        return AlterOption(ADD_FOREIGN_KEY, text, line, operation, detail, hidden=hidden)

    if rest and rest[0] == "PARTITION":
        operation, detail = classify_alter_option(PARTITION, version)
        return AlterOption(PARTITION, text, line, operation, detail, hidden=hidden)

    # Otherwise it is an added column.
    positioned = contains_word(group, "AFTER", "FIRST")
    operation, detail = classify_alter_option(ADD_COLUMN, version, positioned=positioned)
    column = _identifier_after(group, {"ADD", "COLUMN"})
    return AlterOption(
        ADD_COLUMN,
        text,
        line,
        operation,
        detail,
        column,
        hidden,
        (),
        declared_type(group, column),
    )


def _classify_drop(
    group: list[Token],
    words: list[str],
    text: str,
    line: int,
    version: tuple[int, int, int],
    hidden: bool,
) -> AlterOption:
    rest = words[1:]
    if rest and rest[0] == "COLUMN":
        rest = rest[1:]
    if rest and rest[0] in ("INDEX", "KEY"):
        operation, detail = classify_alter_option(DROP_INDEX, version)
        return AlterOption(DROP_INDEX, text, line, operation, detail, _index_name_text(text), hidden)
    if rest and rest[0] == "PRIMARY":
        operation, detail = classify_alter_option(DROP_PRIMARY_KEY, version)
        return AlterOption(DROP_PRIMARY_KEY, text, line, operation, detail, hidden=hidden)
    if rest and rest[0] == "FOREIGN":
        operation, detail = classify_alter_option(DROP_FOREIGN_KEY, version)
        return AlterOption(DROP_FOREIGN_KEY, text, line, operation, detail, hidden=hidden)
    if rest and rest[0] == "PARTITION":
        operation, detail = classify_alter_option(PARTITION, version)
        return AlterOption(PARTITION, text, line, operation, detail, hidden=hidden)
    operation, detail = classify_alter_option(DROP_COLUMN, version)
    column = _identifier_after(group, {"DROP", "COLUMN"})
    return AlterOption(DROP_COLUMN, text, line, operation, detail, column, hidden)


def _value_after_equals(group: list[Token]) -> str:
    for index, token in enumerate(group):
        if token.kind == PUNCT and token.value == "=" and index + 1 < len(group):
            return group[index + 1].value
    return ""


def declared_type(group: list[Token], column_name: str) -> str:
    """The type a column definition declares, e.g. ``DECIMAL(8,2)``.

    Everything after the column's name, up to the first attribute keyword.
    Reading it from the tokens rather than from the rendered statement matters:
    ``MODIFY COLUMN total DECIMAL(8,2)`` and ``MODIFY COLUMN `total`
    DECIMAL(8,2)`` must produce the same comparison, and a text split would
    depend on the spelling of the name.
    """
    stop = {
        "NOT",
        "NULL",
        "DEFAULT",
        "AUTO_INCREMENT",
        "COMMENT",
        "COLLATE",
        "CHARACTER",
        "PRIMARY",
        "UNIQUE",
        "KEY",
        "AFTER",
        "FIRST",
        "REFERENCES",
        "GENERATED",
        "VIRTUAL",
        "STORED",
        "INVISIBLE",
        "VISIBLE",
    }
    collected: list[Token] = []
    seen_name = False
    for token in group:
        if not seen_name:
            if token.kind in IDENTIFIER_KINDS and token.value.lower() == column_name.lower():
                seen_name = True
            continue
        if token.kind == WORD and token.upper in stop:
            break
        collected.append(token)
    # `MODIFY COLUMN` and `CHANGE COLUMN old new TYPE` repeat keywords; drop any
    # that survived the name match.
    while collected and collected[0].kind == WORD and collected[0].upper in ("COLUMN",):
        collected.pop(0)
    return join_tokens(collected)


def _identifier_after(group: list[Token], skip: set[str]) -> str:
    """The first identifier in ``group`` that is not one of ``skip``.

    Taken from the tokens rather than from an uppercased keyword list, because
    the name is *reported back to the reader*: `fulfilment_note` printed as
    `FULFILMENT_NOTE` looks like a different column.
    """
    for token in group:
        if token.kind not in IDENTIFIER_KINDS:
            continue
        if token.kind == WORD and token.upper in skip:
            continue
        return token.value
    return ""


def _index_name(group: list[Token]) -> str:
    """The index name in ``ADD INDEX name (...)``, skipping the optional keyword."""
    skip = {"ADD", "INDEX", "KEY", "UNIQUE", "FULLTEXT", "SPATIAL"}
    seen_keyword = False
    for token in group:
        if token.kind not in IDENTIFIER_KINDS:
            continue
        if token.kind == WORD and token.upper in skip:
            seen_keyword = True
            continue
        if seen_keyword:
            return token.value
    return ""


def _index_name_text(text: str) -> str:
    match = re.search(r"\b(?:INDEX|KEY)\s+`?([\w$]+)`?", text, re.IGNORECASE)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- #
# DML shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DmlShape:
    """Enough of an ``UPDATE``/``DELETE`` to judge how far it will reach."""

    kind: str
    table: str = ""
    has_where: bool = False
    predicate: list[Token] = field(default_factory=list)
    has_limit: bool = False
    has_join: bool = False
    has_subquery: bool = False


def parse_dml(statement: Statement) -> DmlShape | None:
    """Describe an ``UPDATE`` or ``DELETE``, or return ``None``."""
    tokens = statement.significant()
    if not tokens:
        return None

    if tokens[0].is_word("UPDATE"):
        table_tokens = tokens[1:]
        _, table, index = qualified_name(table_tokens, 0)
        predicate = tokens_after(table_tokens, "WHERE")
        return DmlShape(
            kind="UPDATE",
            table=table,
            has_where=bool(predicate),
            predicate=predicate,
            has_limit=contains_word(tokens, "LIMIT"),
            has_join=contains_word(tokens, "JOIN"),
            has_subquery=_has_subquery(predicate),
        )

    if tokens[0].is_word("DELETE"):
        rest = tokens[1:]
        if rest and rest[0].is_word("FROM"):
            rest = rest[1:]
        _, table, index = qualified_name(rest, 0)
        predicate = tokens_after(tokens, "WHERE")
        return DmlShape(
            kind="DELETE",
            table=table,
            has_where=bool(predicate),
            predicate=predicate,
            has_limit=contains_word(tokens, "LIMIT"),
            has_join=contains_word(tokens, "JOIN"),
            has_subquery=_has_subquery(predicate),
        )

    return None


def _has_subquery(predicate: list[Token]) -> bool:
    for token in predicate:
        if token.kind == PUNCT and token.value == "(":
            return True
    return contains_word(predicate, "SELECT")


#: Predicate patterns that make an index unusable. Each entry is
#: ``(description, matcher)``; the matcher receives the predicate tokens.
def unbounded_predicate_reasons(predicate: list[Token]) -> list[str]:
    """Why this ``WHERE`` clause may scan the whole table.

    Deliberately heuristic. Without the schema there is no way to know whether
    the optimizer can use an index, so the tool reports what a human would
    notice on a first read and says "verify with EXPLAIN" rather than pretending
    to be certain.
    """
    reasons: list[str] = []
    if not predicate:
        return ["there is no WHERE clause at all"]

    upper = [token.upper if token.kind == WORD else token.value for token in predicate]

    for index, token in enumerate(predicate):
        # `LIKE '%foo'` cannot use a B-tree index.
        if token.kind == WORD and token.upper == "LIKE" and index + 1 < len(predicate):
            literal = predicate[index + 1]
            if literal.text.startswith(("'%", '"%')):
                reasons.append("LIKE with a leading wildcard cannot use an index")
        # `column != value` and `NOT IN` are non-selective by nature.
        if token.kind == PUNCT and token.value in ("!=", "<>"):
            reasons.append("a `!=` / `<>` comparison is a range scan at best")
        if token.kind == WORD and token.upper == "NOT" and index + 1 < len(predicate):
            if predicate[index + 1].upper in ("IN", "LIKE", "BETWEEN"):
                reasons.append(f"NOT {predicate[index + 1].upper} is rarely selective")

    if "OR" in upper:
        reasons.append("an OR between different columns usually defeats the index")

    # `WHERE 1 = 1` and friends match every row while looking like a filter.
    for index in range(len(predicate) - 2):
        left, operator, right = predicate[index], predicate[index + 1], predicate[index + 2]
        if left.kind == NUMBER and operator.is_punct("=") and right.kind == NUMBER:
            reasons.append("a constant comparison such as `1 = 1` matches every row")
            break

    # A function applied to the left-hand side of a comparison hides the column.
    aggregates = {"COUNT", "SUM", "MIN", "MAX", "AVG", "COALESCE", "IFNULL", "NOW", "CURRENT_TIMESTAMP"}
    for index in range(len(predicate) - 2):
        token = predicate[index]
        if token.kind != WORD or token.upper in aggregates:
            continue
        if not predicate[index + 1].is_punct("("):
            continue
        for later in range(index + 2, min(index + 9, len(predicate))):
            if predicate[later].is_punct("="):
                reasons.append(
                    f"{token.value}(...) is applied to a column, "
                    "so the optimizer cannot use an index on it"
                )
                return reasons
        return reasons

    return reasons
