"""Lexing: the layer where a quiet mistake becomes a confident wrong review.

Every test here corresponds to a construct that a naive splitter gets wrong,
and each one would produce a plausible-looking but incorrect finding.
"""

from __future__ import annotations

import unittest

from migration_guard.models import (
    COMMENT,
    DELIMITER_CMD,
    EXEC_COMMENT,
    NUMBER,
    PUNCT,
    QUOTED_IDENT,
    STRING,
    TERMINATOR,
    WORD,
)
from migration_guard.sqltext import DEFAULT_DELIMITER, split_statements, tokenize


def kinds(tokens) -> list[str]:
    return [token.kind for token in tokens]


def values(tokens) -> list[str]:
    return [token.value for token in tokens if token.kind != TERMINATOR]


class TokenizeBasicsTests(unittest.TestCase):
    def test_words_are_uppercased_on_demand_but_kept_verbatim(self) -> None:
        tokens = [t for t in tokenize("Alter TABLE t") if t.kind == WORD]
        self.assertEqual([t.value for t in tokens], ["Alter", "TABLE", "t"])
        self.assertEqual([t.upper for t in tokens], ["ALTER", "TABLE", "T"])

    def test_positions_are_one_based_and_track_newlines(self) -> None:
        tokens = [t for t in tokenize("SELECT 1;\n  SELECT 2") if t.kind == WORD]
        self.assertEqual((tokens[0].line, tokens[0].column), (1, 1))
        self.assertEqual((tokens[1].line, tokens[1].column), (2, 3))

    def test_offsets_slice_back_to_the_original_text(self) -> None:
        text = "SELECT * FROM `t` WHERE x = 'y'"
        for token in tokenize(text):
            self.assertEqual(text[token.offset : token.end_offset], token.text)

    def test_numbers(self) -> None:
        values_ = [t.value for t in tokenize("1 2.5 .5 1e10 0x1F") if t.kind == NUMBER]
        self.assertEqual(values_, ["1", "2.5", ".5", "1e10", "0x1F"])

    def test_multi_character_operators_are_not_split(self) -> None:
        tokens = [t for t in tokenize("a <=> b != c <> d >= e") if t.kind == PUNCT]
        self.assertEqual([t.value for t in tokens], ["<=>", "!=", "<>", ">="])

    def test_unicode_identifiers_lex_as_one_word(self) -> None:
        tokens = [t for t in tokenize("CREATE TABLE 订单 (金额 INT)") if t.kind == WORD]
        self.assertIn("订单", [t.value for t in tokens])
        self.assertIn("金额", [t.value for t in tokens])

    def test_user_variables(self) -> None:
        tokens = [t for t in tokenize("SET @@session.lock_wait_timeout = 5") if t.kind == WORD]
        self.assertEqual([t.value for t in tokens], ["SET", "@@session", "lock_wait_timeout"])

    def test_unknown_bytes_do_not_hang_the_scanner(self) -> None:
        # A \x00 in a migrated file must not spin the loop forever.
        tokens = tokenize("SELECT\x00\x01 1")
        self.assertTrue(tokens)
        self.assertIn("SELECT", values(tokens))


class CommentTests(unittest.TestCase):
    def test_hash_starts_a_comment(self) -> None:
        tokens = tokenize("# a note\nSELECT 1")
        self.assertEqual(kinds(tokens)[0], COMMENT)

    def test_double_dash_needs_whitespace_after_it(self) -> None:
        # MySQL: `--` only starts a comment before whitespace or end of line.
        # `SELECT 1--2` is arithmetic, and treating it as a comment would hide
        # the `2` from every rule.
        tokens = tokenize("SELECT 1--2")
        self.assertNotIn(COMMENT, kinds(tokens))
        self.assertIn("2", values(tokens))

    def test_double_dash_with_space_is_a_comment(self) -> None:
        tokens = tokenize("SELECT 1 -- 2\n")
        self.assertIn(COMMENT, kinds(tokens))
        self.assertNotIn("2", values(tokens))

    def test_double_dash_at_end_of_line_is_a_comment(self) -> None:
        self.assertIn(COMMENT, kinds(tokenize("SELECT 1 --")))

    def test_block_comment(self) -> None:
        tokens = tokenize("SELECT /* hi */ 1")
        self.assertIn(COMMENT, kinds(tokens))
        self.assertNotIn("hi", values(tokens))

    def test_unterminated_block_comment_is_reported_not_raised(self) -> None:
        tokens = tokenize("SELECT 1 /* never closed")
        comment = [t for t in tokens if t.kind == COMMENT][0]
        self.assertFalse(comment.terminated)

    def test_semicolon_inside_a_comment_does_not_split(self) -> None:
        statements, _ = split_statements("SELECT 1 /* a ; b */; SELECT 2;")
        self.assertEqual(len(statements), 2)


class QuotingTests(unittest.TestCase):
    def test_semicolon_inside_a_string_does_not_split(self) -> None:
        statements, _ = split_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;")
        self.assertEqual(len(statements), 2)
        self.assertIn("'a;b'", statements[0].text)

    def test_doubled_quote_is_an_escaped_quote(self) -> None:
        tokens = [t for t in tokenize("'it''s'") if t.kind == STRING]
        self.assertEqual(tokens[0].value, "it's")
        self.assertTrue(tokens[0].terminated)

    def test_backslash_escape(self) -> None:
        tokens = [t for t in tokenize(r"'a\'b'") if t.kind == STRING]
        self.assertEqual(tokens[0].value, "a'b")

    def test_backslash_is_literal_inside_backticks(self) -> None:
        tokens = [t for t in tokenize(r"`a\b`") if t.kind == QUOTED_IDENT]
        self.assertEqual(tokens[0].value, r"a\b")

    def test_doubled_backtick_is_an_escaped_backtick(self) -> None:
        tokens = [t for t in tokenize("`a``b`") if t.kind == QUOTED_IDENT]
        self.assertEqual(tokens[0].value, "a`b")

    def test_double_quotes_are_scanned_like_a_string(self) -> None:
        # With default sql_mode this is a string; with ANSI_QUOTES it is an
        # identifier. Either way the end of the token is found the same way.
        tokens = [t for t in tokenize('"a;b"') if t.kind == STRING]
        self.assertEqual(tokens[0].value, "a;b")

    def test_unterminated_string_is_reported_not_raised(self) -> None:
        tokens = [t for t in tokenize("SELECT 'abc") if t.kind == STRING]
        self.assertFalse(tokens[0].terminated)

    def test_a_keyword_inside_backticks_is_an_identifier(self) -> None:
        # A column literally called `where` must not be read as the keyword.
        statement_tokens = tokenize("SELECT `where` FROM t")
        identifiers = [t for t in statement_tokens if t.kind == QUOTED_IDENT]
        self.assertEqual([t.value for t in identifiers], ["where"])
        self.assertEqual([t.upper for t in statement_tokens if t.kind == WORD], ["SELECT", "FROM", "T"])


class ExecutableCommentTests(unittest.TestCase):
    def test_plain_executable_comment_is_marked(self) -> None:
        tokens = tokenize("/*!40101 SET NAMES utf8 */")
        self.assertIn(EXEC_COMMENT, kinds(tokens))

    def test_version_is_captured(self) -> None:
        opener = [t for t in tokenize("/*!80029 DROP COLUMN c */") if t.kind == EXEC_COMMENT][0]
        self.assertEqual(opener.value, "80029")

    def test_code_inside_is_visible_but_marked_hidden(self) -> None:
        # MySQL runs this. The analysis must see ALGORITHM=INSTANT, because
        # otherwise the tool would report a false "missing ALGORITHM".
        tokens = tokenize("ALTER TABLE t ADD COLUMN c INT /*!80000 , ALGORITHM=INSTANT */;")
        hidden = [t for t in tokens if t.hidden and t.kind == WORD]
        self.assertIn("ALGORITHM", [t.upper for t in hidden])
        self.assertIn("INSTANT", [t.upper for t in hidden])

    def test_hidden_tokens_keep_their_real_position(self) -> None:
        text = "ALTER TABLE t\n  ADD COLUMN c INT /*!80000 , ALGORITHM=INSTANT */;"
        tokens = tokenize(text)
        instant = [t for t in tokens if t.upper == "INSTANT"][0]
        self.assertEqual(instant.line, 2)
        self.assertEqual(text[instant.offset : instant.end_offset], "INSTANT")

    def test_the_construct_is_reported_by_the_statement(self) -> None:
        statements, _ = split_statements("ALTER TABLE t ADD COLUMN c INT /*!80000 , LOCK=NONE */;")
        self.assertTrue(statements[0].has_hidden_tokens)

    def test_plain_comment_is_not_hidden(self) -> None:
        statements, _ = split_statements("ALTER TABLE t ADD COLUMN c INT /* just a note */;")
        self.assertFalse(statements[0].has_hidden_tokens)

    def test_maria_executable_comment(self) -> None:
        # The dialect marker and the version both survive in one field.
        opener = [t for t in tokenize("/*M!100000 SELECT 1 */") if t.kind == EXEC_COMMENT][0]
        self.assertEqual(opener.value, "M100000")

    def test_plain_executable_comment_without_a_version(self) -> None:
        opener = [t for t in tokenize("/*! SELECT 1 */") if t.kind == EXEC_COMMENT][0]
        self.assertEqual(opener.value, "")


class DelimiterTests(unittest.TestCase):
    def test_delimiter_command_is_recognised(self) -> None:
        tokens = tokenize("DELIMITER $$\nSELECT 1$$")
        command = [t for t in tokens if t.kind == DELIMITER_CMD]
        self.assertEqual(command[0].value, "$$")

    def test_body_containing_semicolons_stays_one_statement(self) -> None:
        text = (
            "DELIMITER $$\n"
            "CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW\n"
            "BEGIN\n"
            "  SET NEW.a = 1;\n"
            "  SET NEW.b = 2;\n"
            "END$$\n"
            "DELIMITER ;\n"
            "SELECT 1;\n"
        )
        statements, _ = split_statements(text)
        self.assertEqual([s.kind for s in statements], ["CREATE", "SELECT"])
        self.assertIn("SET NEW.b = 2;", statements[0].text)

    def test_delimiter_restores_to_semicolon(self) -> None:
        text = "DELIMITER $$\nSELECT 1$$\nDELIMITER ;\nSELECT 2;\nSELECT 3;"
        statements, _ = split_statements(text)
        self.assertEqual(len(statements), 3)

    def test_slash_delimiter(self) -> None:
        text = "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END//\nDELIMITER ;"
        statements, _ = split_statements(text)
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0].kind, "CREATE")

    def test_a_word_merely_starting_with_delimiter_is_not_a_command(self) -> None:
        statements, _ = split_statements("SELECT DELIMITER_X FROM t;")
        self.assertEqual(len(statements), 1)
        self.assertNotIn("DELIMITERS", [s.kind for s in statements])

    def test_delimiter_is_not_a_statement_of_its_own(self) -> None:
        statements, _ = split_statements("DELIMITER $$\nSELECT 1$$")
        self.assertEqual(len(statements), 1)
        self.assertEqual(DEFAULT_DELIMITER, ";")


class StatementSplittingTests(unittest.TestCase):
    def test_empty_input(self) -> None:
        statements, comments = split_statements("")
        self.assertEqual(statements, [])
        self.assertEqual(comments, [])

    def test_comments_only_produce_no_statements(self) -> None:
        statements, comments = split_statements("-- just a note\n/* and another */\n")
        self.assertEqual(statements, [])
        self.assertEqual(len(comments), 2)

    def test_trailing_statement_without_a_terminator_is_kept(self) -> None:
        # The mysql client requires the terminator, but migration runners do
        # not, and silently dropping the last statement would be the worst
        # possible failure for this tool.
        statements, _ = split_statements("SELECT 1;\nSELECT 2")
        self.assertEqual(len(statements), 2)
        self.assertEqual(statements[1].text, "SELECT 2")

    def test_statement_text_excludes_the_terminator(self) -> None:
        statements, _ = split_statements("SELECT 1 ;")
        self.assertNotIn(";", statements[0].text)

    def test_line_ranges_and_indices(self) -> None:
        text = "SELECT 1;\n\nSELECT 2;\n"
        statements, _ = split_statements(text)
        self.assertEqual([s.index for s in statements], [1, 2])
        self.assertEqual((statements[0].start_line, statements[0].end_line), (1, 1))
        self.assertEqual((statements[1].start_line, statements[1].end_line), (3, 3))

    def test_multiline_statement_span(self) -> None:
        text = "ALTER TABLE t\n  ADD COLUMN a INT,\n  ADD COLUMN b INT;\n"
        statements, _ = split_statements(text)
        self.assertEqual((statements[0].start_line, statements[0].end_line), (1, 3))

    def test_kind_is_the_leading_keyword(self) -> None:
        text = "alter table t add column c int; create index i on t (c);"
        statements, _ = split_statements(text)
        self.assertEqual([s.kind for s in statements], ["ALTER", "CREATE"])

    def test_comments_are_attached_to_their_statement(self) -> None:
        text = (
            "-- leading note\n"
            "SELECT 1;  -- trailing note\n"
            "\n"
            "SELECT 2;\n"
            "-- file trailer\n"
        )
        statements, file_level = split_statements(text)
        self.assertEqual(len(statements[0].comments), 2)
        self.assertEqual(len(statements[1].comments), 0)
        self.assertEqual(len(file_level), 1)
        self.assertIn("file trailer", file_level[0].text)

    def test_leading_comment_attaches_to_the_next_statement(self) -> None:
        text = "-- guard: allow(ddl.drop-table) reason: fixture\nDROP TABLE t;"
        statements, file_level = split_statements(text)
        self.assertEqual(file_level, [])
        self.assertEqual(len(statements[0].comments), 1)

    def test_header_block_separated_by_a_blank_line_is_file_level(self) -> None:
        # A blank line is what makes a header read as file-scoped rather than as
        # an annotation on the first statement.
        text = "-- Copyright\n-- guard: allow-file(ddl.drop-table) reason: generated\n\nDROP TABLE t;"
        statements, file_level = split_statements(text)
        self.assertEqual(len(file_level), 2)
        self.assertEqual(statements[0].comments, [])

    def test_trailing_comment_after_the_last_statement_is_file_level(self) -> None:
        text = "SELECT 1;\n\n-- a trailer\n"
        statements, file_level = split_statements(text)
        self.assertEqual(len(statements[0].comments), 0)
        self.assertEqual(len(file_level), 1)

    def test_significant_tokens_exclude_comments(self) -> None:
        statements, _ = split_statements("SELECT /* note */ 1;")
        self.assertNotIn(COMMENT, [t.kind for t in statements[0].tokens])

    def test_crlf_input_does_not_shift_lines(self) -> None:
        statements, _ = split_statements("SELECT 1;\r\nSELECT 2;\r\n")
        self.assertEqual(statements[1].start_line, 2)

    def test_never_raises_on_garbage(self) -> None:
        for garbage in (")))", "'", "`", "/*", "DELIMITER", ";;;", "\x00\xff", "SELECT 'a;b"):
            with self.subTest(garbage=garbage):
                split_statements(garbage)  # must not raise


if __name__ == "__main__":
    unittest.main()
