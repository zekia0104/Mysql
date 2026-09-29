"""Lexing MySQL text: comments, quoting, ``DELIMITER`` blocks, statement splits.

This is the layer where quiet wrongness hides. A splitter that does not
understand quoting will cut a statement in half at a semicolon inside a string;
one that does not understand backticks will mistake a column named ``where``
for the keyword; one that treats ``--`` as a comment everywhere will corrupt a
statement containing ``a - -b``. Every one of those failures produces a
confident, wrong review, so the rules here follow MySQL's lexer as closely as
the documentation allows and the tests cover each construct explicitly.

Three behaviours are worth calling out because they are easy to get wrong and
this tool depends on them:

``--`` needs whitespace
    MySQL only starts a line comment when ``--`` is followed by whitespace or a
    control character. ``SELECT 1--2`` is arithmetic, not a comment.

Executable comments are code
    ``/*!80000 , ALGORITHM=INSTANT */`` is *run* by MySQL 8.0.00 and later. The
    tokens inside are therefore part of the statement and are marked
    ``hidden`` -- the analysis sees them, and a rule can point out that a
    human reading the file would not.

``DELIMITER`` is a client command
    It is not SQL, it changes what ends a statement, and it is how every
    migration file containing a trigger or procedure is written. Ignoring it
    merges the whole body into one nonsense statement.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    COMMENT,
    DELIMITER_CMD,
    EXEC_COMMENT,
    NUMBER,
    PUNCT,
    QUOTED_IDENT,
    STRING,
    TERMINATOR,
    WORD,
    Statement,
    Token,
)

DEFAULT_DELIMITER = ";"

#: Longest first, so ``<=>`` is not read as ``<`` followed by ``=>``.
OPERATORS = (
    "<=>",
    "->>",
    "->",
    "||",
    "&&",
    ":=",
    "<>",
    "!=",
    ">=",
    "<=",
    "<<",
    ">>",
)

PUNCTUATION = set("()[]{},.;+-*/%=<>!&|^~?@:#\\")


def _is_word_start(char: str) -> bool:
    """Identifier start characters, including ``@`` for user variables.

    MySQL allows unquoted identifiers to contain most of the BMP, so a schema
    called ``订单`` must lex as one word rather than as punctuation.
    """
    return char.isalpha() or char in "_$@" or ord(char) > 127


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char in "_$" or ord(char) > 127


def _starts_line_comment(text: str, index: int) -> bool:
    """``--`` begins a comment only before whitespace, a control char, or EOL."""
    if not text.startswith("--", index):
        return False
    following = index + 2
    if following >= len(text):
        return True
    return text[following] in " \t\r\n\f\v"


class _Scanner:
    """Character scanner producing tokens with exact positions."""

    def __init__(self, text: str, *, line: int = 1, column: int = 1, hidden: bool = False) -> None:
        self.text = text
        self.length = len(text)
        self.index = 0
        self.line = line
        self.column = column
        self.hidden = hidden
        self.delimiter = DEFAULT_DELIMITER
        self.tokens: list[Token] = []
        #: True while inside ``/*! ... */``; nested /* */ does not nest in MySQL.
        self.in_exec_comment = False
        #: Index into ``self.tokens`` where the statement being scanned began,
        #: so ``DELIMITER`` is only honoured at the start of a statement.
        self.statement_start = 0

    # -- position bookkeeping ------------------------------------------- #
    def _advance(self, count: int) -> None:
        for _ in range(count):
            if self.index >= self.length:
                return
            if self.text[self.index] == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
            self.index += 1

    def _emit(
        self,
        kind: str,
        start: int,
        *,
        value: str | None = None,
        line: int | None = None,
        column: int | None = None,
        terminated: bool = True,
        hidden: bool | None = None,
    ) -> None:
        raw = self.text[start : self.index]
        if hidden is None:
            # Anything lexed while inside /*! ... */ is code MySQL will run,
            # but a human reading the file will skim past it.
            hidden = self.hidden or self.in_exec_comment
        self.tokens.append(
            Token(
                kind=kind,
                text=raw,
                value=raw if value is None else value,
                line=self.line if line is None else line,
                column=self.column if column is None else column,
                offset=start,
                hidden=hidden,
                terminated=terminated,
            )
        )

    # -- scanner --------------------------------------------------------- #
    def run(self) -> list[Token]:
        previous_index = -1
        while self.index < self.length:
            if self.index == previous_index:
                # A construct consumed nothing. Rather than spin forever -- a
                # hang is the worst possible failure for a tool that runs in
                # CI -- force progress and keep lexing. The guard exists
                # because a missing ``advance`` is easy to introduce and
                # invisible in review; the tests fail loudly instead of hanging.
                self._advance(1)
                continue
            previous_index = self.index

            char = self.text[self.index]

            if char in " \t\r\n\f\v":
                # Whitespace also ends the "first word of a statement" window
                # used by DELIMITER, so consume it without emitting.
                self._advance(1)
                continue

            if char == "#" or _starts_line_comment(self.text, self.index):
                self._line_comment()
                continue

            if self.text.startswith("/*", self.index):
                if self.in_exec_comment:
                    # Inside /*! ... */, a nested /* does not open a comment:
                    # the only thing that closes an executable comment is */.
                    start, line, column = self.index, self.line, self.column
                    self._advance(2)
                    self._emit(PUNCT, start, value="/*", line=line, column=column)
                    continue
                self._block_comment()
                continue

            if self.in_exec_comment and self.text.startswith("*/", self.index):
                start, line, column = self.index, self.line, self.column
                self._advance(2)
                self._emit(EXEC_COMMENT, start, line=line, column=column, hidden=True)
                self.in_exec_comment = False
                continue

            if char in "'\"":
                self._quoted(char, STRING)
                continue

            if char == "`":
                self._quoted(char, QUOTED_IDENT)
                continue

            if self._delimiter_here():
                self._terminator()
                continue

            if char.isdigit() or (char == "." and self._digit_follows()):
                self._number()
                continue

            if _is_word_start(char):
                if self._maybe_delimiter_command():
                    continue
                self._word()
                continue

            if not self._operator_or_punct():
                # Unreachable unless PUNCTUATION and OPERATORS miss a byte;
                # consume it rather than spin forever.
                start, line, column = self.index, self.line, self.column
                self._advance(1)
                self._emit(PUNCT, start, line=line, column=column)

        return self.tokens

    # -- constructs ------------------------------------------------------ #
    def _line_comment(self) -> None:
        start, line, column = self.index, self.line, self.column
        while self.index < self.length and self.text[self.index] != "\n":
            self._advance(1)
        self._emit(COMMENT, start, line=line, column=column)

    def _block_comment(self) -> None:
        start, line, column = self.index, self.line, self.column
        executable = self.text.startswith("/*!", self.index) or self.text.startswith(
            "/*M!", self.index
        )
        self._advance(2)
        if executable:
            marker = 2 if self.text.startswith("/*M!", start) else 1
            self._advance(marker)
            version_start = self.index
            while self.index < self.length and self.text[self.index].isdigit():
                self._advance(1)
            version = self.text[version_start : self.index]
            # MariaDB's variant is recorded as "M<version>" so both facts
            # survive in one field, e.g. "M100000" versus "80029".
            dialect = "M" if marker == 2 else ""
            self._emit(
                EXEC_COMMENT,
                start,
                value=f"{dialect}{version}",
                line=line,
                column=column,
            )
            self.in_exec_comment = True
            return

        while self.index < self.length:
            if self.text.startswith("*/", self.index):
                self._advance(2)
                self._emit(COMMENT, start, line=line, column=column)
                return
            self._advance(1)
        # Ran off the end: an unterminated comment.
        self._emit(COMMENT, start, line=line, column=column, terminated=False)

    def _quoted(self, quote: str, kind: str) -> None:
        start, line, column = self.index, self.line, self.column
        self._advance(1)
        value_chars: list[str] = []
        terminated = False
        # Backslash escapes apply inside strings, but NOT inside backticks,
        # where the only escape is a doubled backtick.
        backslash_escapes = quote != "`"

        while self.index < self.length:
            char = self.text[self.index]
            if backslash_escapes and char == "\\" and self.index + 1 < self.length:
                value_chars.append(self.text[self.index + 1])
                self._advance(2)
                continue
            if char == quote:
                if self.index + 1 < self.length and self.text[self.index + 1] == quote:
                    value_chars.append(quote)
                    self._advance(2)
                    continue
                self._advance(1)
                terminated = True
                break
            value_chars.append(char)
            self._advance(1)

        self._emit(
            kind,
            start,
            value="".join(value_chars),
            line=line,
            column=column,
            terminated=terminated,
        )

    def _number(self) -> None:
        start, line, column = self.index, self.line, self.column
        if self.text.startswith(("0x", "0X"), self.index):
            self._advance(2)
            while self.index < self.length and (
                self.text[self.index].isdigit() or self.text[self.index].lower() in "abcdef"
            ):
                self._advance(1)
            self._emit(NUMBER, start, line=line, column=column)
            return

        seen_dot = False
        while self.index < self.length:
            char = self.text[self.index]
            if char.isdigit():
                self._advance(1)
            elif char == "." and not seen_dot:
                seen_dot = True
                self._advance(1)
            elif char in "eE" and self.index + 1 < self.length:
                self._advance(1)
                if self.index < self.length and self.text[self.index] in "+-":
                    self._advance(1)
            else:
                break
        self._emit(NUMBER, start, line=line, column=column)

    def _word(self) -> None:
        start, line, column = self.index, self.line, self.column
        # A user variable is `@name`, a system variable `@@name`. The `@` marks
        # the start of the identifier only -- allowing it in the middle would
        # merge `a@b` into one token -- so it is consumed before the loop.
        sigils = 0
        while sigils < 2 and self.index < self.length and self.text[self.index] == "@":
            self._advance(1)
            sigils += 1
        while (
            self.index < self.length
            and _is_word_char(self.text[self.index])
            # A delimiter made of identifier characters would otherwise be
            # swallowed by the identifier in front of it: with `DELIMITER $$`,
            # the body ends in `END$$`, and `$` is a legal identifier
            # character, so `END$$` would lex as one word and the statement
            # would never terminate. The mysql client finds delimiters
            # textually, and this is the token-level equivalent.
            and not self._delimiter_here()
        ):
            self._advance(1)
        self._emit(WORD, start, line=line, column=column)

    def _digit_follows(self) -> bool:
        return self.index + 1 < self.length and self.text[self.index + 1].isdigit()

    def _operator_or_punct(self) -> bool:
        start, line, column = self.index, self.line, self.column
        for operator in OPERATORS:
            if self.text.startswith(operator, self.index):
                self._advance(len(operator))
                self._emit(PUNCT, start, line=line, column=column)
                return True
        char = self.text[self.index]
        if char in PUNCTUATION:
            self._advance(1)
            self._emit(PUNCT, start, line=line, column=column)
            return True
        return False

    # -- DELIMITER ------------------------------------------------------- #
    def _delimiter_here(self) -> bool:
        """Whether the current position begins the statement terminator.

        Checked before every other construct so that a delimiter like ``$$`` or
        ``//`` wins over the generic rules for ``$`` and ``/``.
        """
        if self.in_exec_comment:
            return False
        if self.delimiter == DEFAULT_DELIMITER:
            # `;` is unambiguous: quotes and comments are already consumed.
            return self.text.startswith(";", self.index)
        return self.text.startswith(self.delimiter, self.index)

    def _terminator(self) -> None:
        start, line, column = self.index, self.line, self.column
        self._advance(len(self.delimiter) if self.delimiter != DEFAULT_DELIMITER else 1)
        self._emit(TERMINATOR, start, value=self.delimiter, line=line, column=column)
        self.statement_start = len(self.tokens)

    def _at_statement_start(self) -> bool:
        """True when nothing but comments has been seen since the last terminator.

        ``DELIMITER`` is a client command and is only meaningful there; a column
        or alias that happens to be called ``DELIMITER`` is not.
        """
        return not any(
            not token.is_comment for token in self.tokens[self.statement_start :]
        )

    def _maybe_delimiter_command(self) -> bool:
        """Consume a ``DELIMITER x`` client command if this is one."""
        if not self._at_statement_start():
            return False
        remaining = self.text[self.index :]
        keyword = remaining[: len("DELIMITER")]
        if keyword.upper() != "DELIMITER":
            return False
        after = self.index + len("DELIMITER")
        if after < self.length and _is_word_char(self.text[after]):
            return False
        # It must be followed by whitespace and then the new delimiter.
        cursor = after
        while cursor < self.length and self.text[cursor] in " \t":
            cursor += 1
        if cursor >= self.length or self.text[cursor] in "\r\n":
            return False

        start, line, column = self.index, self.line, self.column
        name_start = cursor
        while cursor < self.length and self.text[cursor] not in " \t\r\n":
            cursor += 1
        new_delimiter = self.text[name_start:cursor]
        if not new_delimiter:
            return False

        self._advance(cursor - self.index)
        self._emit(DELIMITER_CMD, start, value=new_delimiter, line=line, column=column)
        self.delimiter = new_delimiter
        # The command ends at the end of its line, not at a delimiter.
        self.tokens.append(
            Token(
                kind=TERMINATOR,
                text="",
                value="\n",
                line=self.line,
                column=self.column,
                offset=self.index,
            )
        )
        self.statement_start = len(self.tokens)
        return True


def tokenize(text: str) -> list[Token]:
    """Lex ``text`` into tokens, never raising on malformed input.

    Unterminated strings, identifiers and comments come back as tokens with
    ``terminated=False`` so a rule can report them, rather than as an exception
    that would stop the review.
    """
    return _Scanner(text).run()


def _attach_comments(statements: list[Statement], comments: list[Token]) -> list[Token]:
    """Give each comment to the statement it belongs to; return file-level ones.

    Two idioms have to work, and they sit on the same line:

    ``ALTER TABLE t ...; -- guard: allow(...)``
        the comment starts *after* the statement's first token, so it is a
        trailing comment of that statement;

    ``-- a note`` immediately above a statement
        attached to the statement below it.

    A comment separated from the next statement by a blank line is file level,
    which is what makes a header block read as file-scoped rather than as an
    annotation on the first statement.
    """
    file_level: list[Token] = []
    for comment in comments:
        owner: Statement | None = None

        for statement in statements:
            if not statement.tokens:
                continue
            starts_after = comment.offset > statement.tokens[0].offset
            if starts_after and comment.line <= statement.end_line:
                owner = statement
                break

        if owner is None:
            comment_end_line = comment.line + comment.text.count("\n")
            for statement in statements:
                if statement.start_line <= comment_end_line:
                    continue
                if statement.start_line - comment_end_line == 1:
                    owner = statement
                break

        if owner is None:
            file_level.append(comment)
        else:
            owner.comments.append(comment)
    return file_level


@dataclass
class ParseResult:
    """Everything one lex pass learned about a file."""

    statements: list[Statement]
    #: Comments that belong to no statement: a header block, or a trailer.
    file_comments: list[Token]
    #: Every token including comments, for file-scope inspection.
    tokens: list[Token]
    #: ``DELIMITER x`` client commands, in order. They are not SQL and never
    #: become statements, but some migration runners cannot parse them.
    delimiters: list[Token]

    @property
    def unterminated(self) -> list[Token]:
        """Strings, identifiers and comments that were never closed."""
        return [token for token in self.tokens if not token.terminated]


def parse_sql(text: str) -> ParseResult:
    """Lex ``text`` and split it into statements.

    A statement's ``text`` excludes its terminator; a trailing statement without
    one is kept, because migration runners accept that and silently dropping the
    last statement would be the worst failure this tool could have.
    """
    tokens = tokenize(text)
    comments = [token for token in tokens if token.is_comment]
    delimiters = [token for token in tokens if token.kind == DELIMITER_CMD]

    statements: list[Statement] = []
    current: list[Token] = []

    def meaningful(items: list[Token]) -> list[Token]:
        # `DELIMITER $$` is a client command, not SQL: it must not become a
        # statement of its own, nor contaminate the one that follows.
        return [
            item
            for item in items
            if not item.is_comment and item.kind != DELIMITER_CMD
        ]

    for token in tokens:
        if token.kind == TERMINATOR:
            significant = meaningful(current)
            if significant:
                statements.append(
                    Statement(
                        index=len(statements) + 1,
                        tokens=significant,
                        text=text[significant[0].offset : token.offset].strip(),
                        start_line=significant[0].line,
                        end_line=token.line,
                    )
                )
            current = []
            continue
        current.append(token)

    significant = meaningful(current)
    if significant:
        last = significant[-1]
        statements.append(
            Statement(
                index=len(statements) + 1,
                tokens=significant,
                text=text[significant[0].offset : last.end_offset].strip(),
                start_line=significant[0].line,
                end_line=last.line,
            )
        )

    file_comments = _attach_comments(statements, comments)
    for statement in statements:
        statement.comments.sort(key=lambda token: token.offset)
    return ParseResult(
        statements=statements,
        file_comments=file_comments,
        tokens=tokens,
        delimiters=delimiters,
    )


def split_statements(text: str) -> tuple[list[Statement], list[Token]]:
    """Convenience wrapper over :func:`parse_sql` for the two common outputs."""
    result = parse_sql(text)
    return result.statements, result.file_comments
