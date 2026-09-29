"""Reading an existing schema, so a migration can be judged against it.

Half of the dangerous changes in a migration are only dangerous relative to what
is already there: adding an index that a table already has, widening an index
past InnoDB's key limit, or narrowing a column that holds more than the new type
can store. None of that is visible in the migration file alone.

The input is the output of ``mysqldump --no-data`` (or any file containing
``CREATE TABLE`` statements), which every MySQL installation can produce::

    mysqldump --no-data --skip-comments --compact shop > schema.sql

That keeps the tool's promise intact -- no server connection, no driver, no
credentials -- while making it considerably less blind. When no schema is given,
the schema-aware rules simply do not run, and nothing else changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import NUMBER, PUNCT, WORD, Statement, Token
from .sqltext import parse_sql
from .statements import (
    IDENTIFIER_KINDS,
    index_columns_from,
    join_tokens,
    matching_paren,
    qualified_name,
    split_top_level,
)

#: InnoDB's index key limit, in bytes. 3072 with the DYNAMIC and COMPRESSED row
#: formats (the default since 5.7) and 767 with REDUNDANT and COMPACT.
KEY_LIMIT_DYNAMIC = 3072
KEY_LIMIT_COMPACT = 767

#: Bytes per character for the character sets that matter in practice.
CHARSET_WIDTH = {
    "utf8mb4": 4,
    "utf8": 3,
    "utf8mb3": 3,
    "latin1": 1,
    "ascii": 1,
    "binary": 1,
    "gbk": 2,
    "gb2312": 2,
    "big5": 2,
    "utf16": 4,
    "utf32": 4,
}

#: Integer types by storage width, for comparing a column change.
INTEGER_WIDTH = {
    "TINYINT": 1,
    "SMALLINT": 2,
    "MEDIUMINT": 3,
    "INT": 4,
    "INTEGER": 4,
    "BIGINT": 8,
}

#: The text and blob families, smallest first.
TEXT_FAMILY = ("TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT")
BLOB_FAMILY = ("TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB")

#: Types that cannot be indexed without a prefix length.
UNINDEXABLE_WHOLE = set(TEXT_FAMILY) | set(BLOB_FAMILY) | {"JSON", "GEOMETRY"}

_TYPE_RE = re.compile(r"^(?P<base>[A-Za-z]+)\s*(?:\((?P<args>[^)]*)\))?")


@dataclass
class Column:
    """One column as the schema declares it."""

    name: str
    type_text: str
    nullable: bool = True
    default: str | None = None
    charset: str = ""
    generated: bool = False

    @property
    def base_type(self) -> str:
        match = _TYPE_RE.match(self.type_text.strip())
        return (match.group("base") or "").upper() if match else ""

    @property
    def length(self) -> int | None:
        """The declared length, for the types that take one."""
        match = _TYPE_RE.match(self.type_text.strip())
        if not match or not match.group("args"):
            return None
        first = match.group("args").split(",")[0].strip()
        return int(first) if first.isdigit() else None

    @property
    def precision(self) -> tuple[int, int] | None:
        """``(precision, scale)`` for DECIMAL and friends."""
        match = _TYPE_RE.match(self.type_text.strip())
        if not match or not match.group("args"):
            return None
        parts = [part.strip() for part in match.group("args").split(",")]
        if not parts[0].isdigit():
            return None
        precision = int(parts[0])
        scale = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return (precision, scale)

    @property
    def is_integer(self) -> bool:
        return self.base_type in INTEGER_WIDTH

    @property
    def is_text_or_blob(self) -> bool:
        return self.base_type in UNINDEXABLE_WHOLE

    def storage_bytes(self) -> int | None:
        """Bytes this column occupies in an index, or ``None`` if unbounded.

        Only the character types depend on the table's character set, which is
        why the width is resolved per column rather than per index.
        """
        base = self.base_type
        if base in INTEGER_WIDTH:
            return INTEGER_WIDTH[base]
        if base in ("DATE",):
            return 3
        if base in ("TIME",):
            return 3
        if base in ("DATETIME", "TIMESTAMP"):
            return 5
        if base in ("YEAR",):
            return 1
        if base in ("FLOAT",):
            return 4
        if base in ("DOUBLE", "REAL"):
            return 8
        if base in ("CHAR", "VARCHAR"):
            width = CHARSET_WIDTH.get(self.charset, 4)
            return (self.length or 0) * width
        if base == "DECIMAL":
            precision = self.precision
            if precision:
                # MySQL stores DECIMAL in groups of nine digits per four bytes.
                digits = precision[0]
                return (digits // 9) * 4 + [0, 1, 1, 2, 2, 3, 3, 4, 4, 4][digits % 9]
            return None
        if base in UNINDEXABLE_WHOLE:
            return None
        return None


@dataclass
class IndexColumn:
    """One column inside an index definition, with its optional prefix length."""

    name: str
    prefix_length: int | None = None

    def key(self) -> tuple[str, int | None]:
        return (self.name.lower(), self.prefix_length)


@dataclass
class Index:
    """A key as the schema declares it."""

    name: str
    columns: list[IndexColumn] = field(default_factory=list)
    unique: bool = False
    primary: bool = False
    kind: str = "BTREE"

    def column_names(self) -> list[str]:
        return [column.name.lower() for column in self.columns]

    def to_key(self) -> tuple:
        return tuple(column.key() for column in self.columns)


@dataclass
class Table:
    """One table from the schema."""

    name: str
    schema: str = ""
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    engine: str = ""
    charset: str = ""
    row_format: str = ""

    @property
    def table_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def column(self, name: str) -> Column | None:
        wanted = name.strip("`").lower()
        for candidate in self.columns:
            if candidate.name.lower() == wanted:
                return candidate
        return None

    def index_named(self, name: str) -> Index | None:
        wanted = name.strip("`").lower()
        for index in self.indexes:
            if index.name.lower() == wanted:
                return index
        return None

    def indexes_covering(self, columns: list[str]) -> list[Index]:
        """Indexes whose leading columns are exactly ``columns``."""
        wanted = [name.lower() for name in columns]
        return [index for index in self.indexes if index.column_names() == wanted]

    def indexes_prefixed_by(self, columns: list[str]) -> list[Index]:
        """Indexes whose leading columns are a strict prefix of ``columns``."""
        wanted = [name.lower() for name in columns]
        found = []
        for index in self.indexes:
            names = index.column_names()
            if len(names) < len(wanted) and wanted[: len(names)] == names:
                found.append(index)
        return found

    def indexes_starting_with(self, column: str) -> list[Index]:
        wanted = column.lower()
        return [index for index in self.indexes if index.column_names()[:1] == [wanted]]

    @property
    def key_limit(self) -> int:
        if self.row_format.upper() in ("COMPACT", "REDUNDANT"):
            return KEY_LIMIT_COMPACT
        return KEY_LIMIT_DYNAMIC

    def column_charset(self, column: Column) -> str:
        return (column.charset or self.charset or "utf8mb4").lower()


@dataclass
class Schema:
    """Every table found in one schema file."""

    tables: dict[str, Table] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def table(self, name: str) -> Table | None:
        key = name.strip("`").lower()
        if key in self.tables:
            return self.tables[key]
        # A migration may qualify the name while the dump does not, or the other
        # way round; match on the bare table name as a fallback.
        return self.tables.get(key.split(".")[-1])

    def __len__(self) -> int:
        return len(self.tables)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_schema(text: str, *, source: str = "schema") -> Schema:
    """Read every ``CREATE TABLE`` in ``text`` into a :class:`Schema`."""
    schema = Schema()
    parsed = parse_sql(text)
    for statement in parsed.statements:
        if statement.kind == "CREATE" and _is_create_table(statement):
            table = parse_create_table(statement)
            if table is None:
                continue
            key = table.name.lower()
            if key in schema.tables:
                schema.notes.append(f"{source}: {table.name} defined more than once, kept the last")
            schema.tables[key] = table
    if not schema.tables:
        schema.notes.append(f"{source}: no CREATE TABLE statements found")
    return schema


def _is_create_table(statement: Statement) -> bool:
    words = statement.words()
    return bool(words) and words[0] == "CREATE" and "TABLE" in words[:4]


def parse_create_table(statement: Statement) -> Table | None:
    """Turn one ``CREATE TABLE`` statement into a :class:`Table`."""
    tokens = statement.significant()

    # Find the TABLE keyword that introduces the name, then step over the
    # optional IF NOT EXISTS before reading it.
    search_from = 0
    for candidate, token in enumerate(tokens):
        if token.is_word("TABLE"):
            search_from = candidate + 1
            break
    while search_from < len(tokens) and tokens[search_from].is_word("IF", "NOT", "EXISTS"):
        search_from += 1

    schema_name, name, body_start = qualified_name(tokens, search_from)
    if not name:
        return None

    table = Table(name=name, schema=schema_name)

    # Everything between the outermost parentheses is the definition list.
    opening = _index_of_punct(tokens, "(", body_start)
    closing = _matching_paren(tokens, opening)
    if opening is None or closing is None:
        return None

    for group in split_top_level(tokens[opening + 1 : closing]):
        _parse_definition(group, table)

    for token in tokens[closing + 1 :]:
        if token.kind != WORD:
            continue
        upper = token.upper
        if upper == "ENGINE":
            table.engine = _option_value(tokens, token)
        elif upper in ("CHARSET", "CHARACTER"):
            table.charset = _option_value(tokens, token)
        elif upper == "COLLATE":
            collation = _option_value(tokens, token)
            if collation and not table.charset:
                table.charset = collation.split("_")[0]
        elif upper == "ROW_FORMAT":
            table.row_format = _option_value(tokens, token)
    return table


def _index_of_punct(tokens: list[Token], value: str, start: int = 0) -> int | None:
    for index in range(start, len(tokens)):
        if tokens[index].kind == PUNCT and tokens[index].value == value:
            return index
    return None


def _matching_paren(tokens: list[Token], opening: int | None) -> int | None:
    return matching_paren(tokens, opening)


def _option_value(tokens: list[Token], token: Token) -> str:
    for index, candidate in enumerate(tokens):
        if candidate is not token:
            continue
        for later in tokens[index + 1 : index + 3]:
            if later.kind == PUNCT and later.value == "=":
                continue
            if later.kind in IDENTIFIER_KINDS:
                return later.value
        break
    return ""


def _parse_definition(group: list[Token], table: Table) -> None:
    """One comma-separated entry inside ``CREATE TABLE (...)``."""
    words = [token.upper for token in group if token.kind == WORD]
    if not words:
        return

    if words[0] == "PRIMARY":
        table.indexes.append(
            Index(
                name="PRIMARY",
                columns=_index_columns(group),
                unique=True,
                primary=True,
                kind="BTREE",
            )
        )
        return

    if words[0] in ("UNIQUE", "KEY", "INDEX", "FULLTEXT", "SPATIAL"):
        kind = "FULLTEXT" if "FULLTEXT" in words else ("SPATIAL" if "SPATIAL" in words else "BTREE")
        unique = "UNIQUE" in words
        name = _index_definition_name(group)
        table.indexes.append(
            Index(name=name, columns=_index_columns(group), unique=unique, kind=kind)
        )
        return

    if words[0] == "CONSTRAINT" or words[0] == "FOREIGN":
        # Foreign keys are stored as an index in InnoDB; record the child column
        # so `ddl.fk-missing-index` can tell whether one exists.
        for index, token in enumerate(group):
            if token.is_word("KEY", "INDEX") and index + 1 < len(group):
                columns = _index_columns(group[index:])
                if columns:
                    table.indexes.append(
                        Index(name=columns[0].name, columns=columns, kind="FOREIGN")
                    )
                break
        return

    column = _parse_column(group)
    if column is not None:
        table.columns.append(column)


def _index_definition_name(group: list[Token]) -> str:
    skip = {"UNIQUE", "KEY", "INDEX", "FULLTEXT", "SPATIAL", "USING", "BTREE", "HASH"}
    for token in group:
        if token.kind not in IDENTIFIER_KINDS:
            continue
        if token.kind == WORD and token.upper in skip:
            continue
        # The first identifier that is not `USING BTREE` and is followed by "("
        # is the name; a definition like `KEY (a)` has no name at all.
        return token.value
    return ""


def _index_columns(tokens: list[Token]) -> list[IndexColumn]:
    """The column list of an index definition, with prefix lengths."""
    return [
        IndexColumn(name=name, prefix_length=prefix)
        for name, prefix in index_columns_from(tokens)
    ]


def _parse_column(group: list[Token]) -> Column | None:
    """One column definition: ``name type [attributes]``."""
    name = ""
    type_start = None
    for index, token in enumerate(group):
        if not name:
            if token.kind in IDENTIFIER_KINDS:
                name = token.value
                continue
            return None
        if token.kind == WORD or (token.kind == PUNCT and token.value == "("):
            type_start = index
            break
    if not name or type_start is None:
        return None

    # The type is the word plus an optional parenthesised argument list, which
    # may itself contain commas: `DECIMAL(10, 2)`.
    type_end = type_start + 1
    if type_start < len(group) and group[type_start].kind == WORD:
        if type_end < len(group) and group[type_end].is_punct("("):
            closing = _matching_paren(group, type_end)
            type_end = (closing + 1) if closing is not None else type_end
    type_text = join_tokens(group[type_start:type_end]).replace(" ", "")

    upper = [token.upper for token in group if token.kind == WORD]
    nullable = not any(
        first == "NOT" and second == "NULL" for first, second in zip(upper, upper[1:])
    )

    default: str | None = None
    charset = ""
    for index, token in enumerate(group):
        if token.is_word("DEFAULT") and index + 1 < len(group):
            default = group[index + 1].value
        if token.is_word("CHARACTER") and index + 2 < len(group):
            if group[index + 1].is_word("SET"):
                charset = group[index + 2].value.lower()
        if token.is_word("COLLATE") and index + 1 < len(group):
            charset = charset or group[index + 1].value.split("_")[0].lower()

    return Column(
        name=name,
        type_text=type_text,
        nullable=nullable,
        default=default,
        charset=charset,
        generated=any(token.is_word("GENERATED", "VIRTUAL", "STORED") for token in group),
    )


# --------------------------------------------------------------------------- #
# Comparisons
# --------------------------------------------------------------------------- #


def narrows_type(old: Column, new_type_text: str) -> str | None:
    """Why ``new_type_text`` cannot hold what ``old`` holds, or ``None``.

    Only the cases where the loss is certain are reported: a smaller integer
    width, a shorter string, a smaller decimal, or a text family moving
    downwards. Widening is never reported, and a change between unrelated types
    is left to the migration's author because there is no way to judge it from
    the schema alone.
    """
    candidate = Column(name=old.name, type_text=new_type_text, charset=old.charset)
    if not candidate.base_type:
        return None

    old_base, new_base = old.base_type, candidate.base_type
    if old_base in INTEGER_WIDTH and new_base in INTEGER_WIDTH:
        if INTEGER_WIDTH[new_base] < INTEGER_WIDTH[old_base]:
            return (
                f"{old_base} holds up to "
                f"{2 ** (8 * INTEGER_WIDTH[old_base]) - 1} but {new_base} stops at "
                f"{2 ** (8 * INTEGER_WIDTH[new_base]) - 1}"
            )
        return None

    if old_base in ("CHAR", "VARCHAR") and new_base in ("CHAR", "VARCHAR"):
        old_length, new_length = old.length or 0, candidate.length or 0
        if new_length and old_length and new_length < old_length:
            return f"VARCHAR({old_length}) becomes VARCHAR({new_length})"
        return None

    if old_base in TEXT_FAMILY and new_base in TEXT_FAMILY:
        if TEXT_FAMILY.index(new_base) < TEXT_FAMILY.index(old_base):
            return f"{old_base} becomes {new_base}"
        return None

    if old_base in BLOB_FAMILY and new_base in BLOB_FAMILY:
        if BLOB_FAMILY.index(new_base) < BLOB_FAMILY.index(old_base):
            return f"{old_base} becomes {new_base}"
        return None

    if old_base == "DECIMAL" and new_base == "DECIMAL":
        old_precision, new_precision = old.precision, candidate.precision
        if old_precision and new_precision:
            if new_precision[0] < old_precision[0]:
                return f"DECIMAL({old_precision[0]},{old_precision[1]}) becomes DECIMAL({new_precision[0]},{new_precision[1]})"
            if new_precision[1] < old_precision[1]:
                return f"the scale drops from {old_precision[1]} to {new_precision[1]} decimal places"
        return None

    # A type that became unbounded (TEXT/BLOB) is a widening, not a narrowing.
    if old_base in ("CHAR", "VARCHAR") and new_base in TEXT_FAMILY:
        return None

    return None


def index_key_bytes(table: Table, columns: list[IndexColumn]) -> tuple[int | None, str]:
    """Total key length in bytes, plus the column that made it unbounded.

    Returns ``(None, name)`` when a column cannot be indexed without a prefix
    length, which is a different error and is reported by its own message.
    """
    total = 0
    for spec in columns:
        column = table.column(spec.name)
        if column is None:
            continue
        if spec.prefix_length is not None:
            width = CHARSET_WIDTH.get(table.column_charset(column), 4)
            total += spec.prefix_length * width
            continue
        size = column.storage_bytes()
        if size is None:
            return None, spec.name
        total += size
    return total, ""
