"""The online-DDL matrix, the rules and the suppressions.

The rule registry is exercised as a whole: one fixture that breaks every rule at
once, and a check that each registered rule produced at least one finding. That
guard exists because a rule that never fires is invisible -- it looks green in
review and silently stops protecting anyone.
"""

from __future__ import annotations

import json
import unittest

from migration_guard.analyze import analyze_text, build_context, review
from migration_guard.directives import collect, parse_directive
from migration_guard.models import Severity
from migration_guard.report import github, json_report, render, text
from migration_guard.rules import RULE_IDS, RULES
from migration_guard.sqltext import parse_sql
from migration_guard import statements as st

V80 = st.parse_version("8.0.29")
V57 = st.parse_version("5.7.44")
V8012 = st.parse_version("8.0.12")


def findings(sql: str, version=V80, path: str = "m.sql") -> list:
    return analyze_text(path, sql, version).findings


def rules_fired(sql: str, version=V80) -> set[str]:
    return {finding.rule for finding in findings(sql, version)}


def alter_of(sql: str, version=V80):
    context = build_context("m.sql", sql, version)
    return context.alters[0]


class VersionParsingTests(unittest.TestCase):
    def test_parses_three_part_versions(self) -> None:
        self.assertEqual(st.parse_version("8.0.29"), (8, 0, 29))
        self.assertEqual(st.parse_version("5.7"), (5, 7, 0))
        self.assertEqual(st.parse_version("8"), (8, 0, 0))

    def test_garbage_defaults_to_a_modern_8_0(self) -> None:
        # Defaulting to 0 would flag perfectly safe ADD COLUMN statements on
        # every machine that did not pass --target-version.
        self.assertEqual(st.parse_version(""), (8, 0, 0))
        self.assertEqual(st.parse_version("mariadb"), (8, 0, 0))


class OnlineDdlMatrixTests(unittest.TestCase):
    def test_add_column_is_instant_on_8029_and_a_rebuild_on_57(self) -> None:
        self.assertEqual(st.classify_add_column(V80, positioned=False), st.INSTANT)
        self.assertEqual(st.classify_add_column(V57, positioned=False), st.COPY)

    def test_positioned_add_column_only_became_instant_in_8029(self) -> None:
        # 8.0.12 made *appending* a column metadata-only; putting it anywhere
        # else still rebuilt until 8.0.29. Getting this boundary wrong is how a
        # tool tells someone an ALTER is safe when it is not.
        self.assertEqual(st.classify_add_column(V8012, positioned=True), st.COPY)
        self.assertEqual(st.classify_add_column(V8012, positioned=False), st.INSTANT)
        self.assertEqual(st.classify_add_column(V80, positioned=True), st.INSTANT)

    def test_drop_column_became_instant_in_8029(self) -> None:
        self.assertEqual(st.classify_drop_column(V8012), st.COPY)
        self.assertEqual(st.classify_drop_column(V80), st.INSTANT)

    def test_add_index_is_online_but_fulltext_blocks(self) -> None:
        self.assertEqual(st.classify_alter_option(st.ADD_INDEX, V80)[0], st.INPLACE_ONLINE)
        self.assertEqual(
            st.classify_alter_option(st.ADD_INDEX, V80, fulltext=True)[0], st.INPLACE_BLOCKING
        )

    def test_primary_key_change_rebuilds(self) -> None:
        self.assertEqual(st.classify_alter_option(st.ADD_PRIMARY_KEY, V80)[0], st.COPY)
        self.assertEqual(st.classify_alter_option(st.DROP_PRIMARY_KEY, V80)[0], st.COPY)

    def test_modify_column_rebuilds(self) -> None:
        self.assertEqual(st.classify_alter_option(st.MODIFY_COLUMN, V80)[0], st.COPY)

    def test_setting_only_the_default_is_online(self) -> None:
        self.assertEqual(st.classify_alter_option(st.SET_DEFAULT, V80)[0], st.INPLACE_ONLINE)

    def test_unknown_options_are_unknown_not_guessed(self) -> None:
        self.assertEqual(st.classify_alter_option("SOMETHING NEW", V80)[0], st.UNKNOWN)


class AlterParsingTests(unittest.TestCase):
    def test_splits_options_at_top_level_commas_only(self) -> None:
        alter = alter_of(
            "ALTER TABLE t ADD COLUMN a DECIMAL(10, 2), ADD COLUMN b VARCHAR(4), "
            "ADD INDEX i (a, b);"
        )
        self.assertEqual([option.kind for option in alter.options], [
            st.ADD_COLUMN, st.ADD_COLUMN, st.ADD_INDEX,
        ])

    def test_schema_qualified_table(self) -> None:
        alter = alter_of("ALTER TABLE shop.orders ADD COLUMN c INT;")
        self.assertEqual(alter.table, "orders")
        self.assertEqual(alter.schema, "shop")
        self.assertEqual(alter.table_name, "shop.orders")

    def test_backtick_quoted_table(self) -> None:
        alter = alter_of("ALTER TABLE `orders` ADD COLUMN c INT;")
        self.assertEqual(alter.table, "orders")

    def test_algorithm_and_lock_are_settings_not_changes(self) -> None:
        alter = alter_of(
            "ALTER TABLE t ADD COLUMN c INT, ALGORITHM=INPLACE, LOCK=NONE;"
        )
        self.assertEqual(alter.algorithm, "INPLACE")
        self.assertEqual(alter.lock, "NONE")
        self.assertTrue(alter.is_explicit)
        self.assertEqual(len(alter.structural), 1)

    def test_column_name_keeps_its_original_case(self) -> None:
        # The name is reported back to the reader, so upper-casing it makes the
        # finding look like it is about a different column.
        alter = alter_of("ALTER TABLE t ADD COLUMN fulfilment_note TEXT;")
        self.assertEqual(alter.options[0].column, "fulfilment_note")

    def test_worst_operation_wins(self) -> None:
        alter = alter_of("ALTER TABLE t ADD COLUMN a INT, MODIFY COLUMN b INT;")
        self.assertEqual(alter.worst_operation, st.COPY)
        self.assertTrue(alter.rebuilds)

    def test_instantly_safe_alter_reports_nothing(self) -> None:
        alter = alter_of("ALTER TABLE t ADD COLUMN a INT, ALGORITHM=INSTANT;")
        self.assertEqual(alter.worst_operation, st.INSTANT)
        self.assertFalse(alter.blocks_writes)

    def test_add_index_without_the_column_keyword(self) -> None:
        alter = alter_of("ALTER TABLE t ADD INDEX idx (a);")
        self.assertEqual(alter.options[0].kind, st.ADD_INDEX)

    def test_add_unique_key(self) -> None:
        alter = alter_of("ALTER TABLE t ADD UNIQUE KEY uk_email (email);")
        self.assertEqual(alter.options[0].kind, st.ADD_INDEX)

    def test_add_foreign_key(self) -> None:
        alter = alter_of(
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES o (id);"
        )
        self.assertEqual(alter.options[0].kind, st.ADD_FOREIGN_KEY)

    def test_positioned_add_column_is_detected(self) -> None:
        alter = alter_of("ALTER TABLE t ADD COLUMN a INT AFTER b;")
        self.assertEqual(alter.options[0].operation, st.INSTANT)  # 8.0.29
        alter57 = alter_of("ALTER TABLE t ADD COLUMN a INT AFTER b;", V57)
        self.assertEqual(alter57.options[0].operation, st.COPY)

    def test_not_an_alter_returns_none(self) -> None:
        context = build_context("m.sql", "SELECT 1;", V80)
        self.assertEqual(context.alters, [])


class DmlShapeTests(unittest.TestCase):
    def test_update_without_where(self) -> None:
        context = build_context("m.sql", "UPDATE orders SET a = 1;", V80)
        shape = context.dmls[0][1]
        self.assertEqual(shape.kind, "UPDATE")
        self.assertEqual(shape.table, "orders")
        self.assertFalse(shape.has_where)

    def test_delete_with_where_and_limit(self) -> None:
        context = build_context("m.sql", "DELETE FROM t WHERE id < 10 LIMIT 100;", V80)
        shape = context.dmls[0][1]
        self.assertTrue(shape.has_where)
        self.assertTrue(shape.has_limit)

    def test_leading_wildcard_like_is_flagged(self) -> None:
        context = build_context("m.sql", "UPDATE t SET a = 1 WHERE name LIKE '%x';", V80)
        reasons = st.unbounded_predicate_reasons(context.dmls[0][1].predicate)
        self.assertTrue(any("leading wildcard" in reason for reason in reasons))

    def test_function_on_column_is_flagged(self) -> None:
        context = build_context("m.sql", "UPDATE t SET a = 1 WHERE DATE(created) = '2026-01-01';", V80)
        reasons = st.unbounded_predicate_reasons(context.dmls[0][1].predicate)
        self.assertTrue(any("DATE(" in reason for reason in reasons))

    def test_selective_predicate_is_not_flagged(self) -> None:
        context = build_context("m.sql", "UPDATE t SET a = 1 WHERE id = 42;", V80)
        self.assertEqual(st.unbounded_predicate_reasons(context.dmls[0][1].predicate), [])


class RuleTests(unittest.TestCase):
    def test_rebuild_is_critical_and_names_the_table(self) -> None:
        found = findings("ALTER TABLE orders MODIFY COLUMN total DECIMAL(12,2);")
        top = [f for f in found if f.rule == "ddl.table-rebuild"][0]
        self.assertIs(top.severity, Severity.CRITICAL)
        self.assertIn("orders", top.title)
        self.assertEqual(top.line, 1)
        self.assertTrue(top.hint)

    def test_instant_add_column_is_clean_on_8029(self) -> None:
        self.assertEqual(rules_fired("ALTER TABLE t ADD COLUMN c INT;"), set())

    def test_the_same_statement_is_critical_on_57(self) -> None:
        self.assertIn("ddl.table-rebuild", rules_fired("ALTER TABLE t ADD COLUMN c INT;", V57))

    def test_lock_wait_timeout_is_reported_once_per_file(self) -> None:
        sql = (
            "ALTER TABLE a MODIFY COLUMN x INT;\n"
            "ALTER TABLE b MODIFY COLUMN y INT;\n"
            "ALTER TABLE c MODIFY COLUMN z INT;\n"
        )
        count = sum(1 for f in findings(sql) if f.rule == "ddl.no-lock-wait-timeout")
        self.assertEqual(count, 1)

    def test_lock_wait_timeout_is_silent_when_guarded(self) -> None:
        sql = "SET SESSION lock_wait_timeout = 5;\nALTER TABLE a MODIFY COLUMN x INT;\n"
        self.assertNotIn("ddl.no-lock-wait-timeout", rules_fired(sql))

    def test_update_without_where_is_critical(self) -> None:
        self.assertIn("dml.update-without-where", rules_fired("UPDATE t SET a = 1;"))

    def test_delete_without_where_is_critical(self) -> None:
        self.assertIn("dml.delete-without-where", rules_fired("DELETE FROM t;"))

    def test_drop_table_is_critical_but_temporary_is_not(self) -> None:
        self.assertIn("ddl.drop-table", rules_fired("DROP TABLE t;"))
        temporary = [f for f in findings("DROP TEMPORARY TABLE t;") if f.rule == "ddl.drop-table"]
        self.assertIs(temporary[0].severity, Severity.INFO)

    def test_foreign_key_checks_off_is_critical(self) -> None:
        # This rule silently never fired once because it compared the token kind
        # against "NUMBER" instead of the "number" constant.
        self.assertIn(
            "session.foreign-key-checks-off", rules_fired("SET foreign_key_checks = 0;")
        )

    def test_relaxed_sql_mode_is_a_warning(self) -> None:
        self.assertIn("session.sql-mode-relaxed", rules_fired("SET sql_mode = '';"))

    def test_executable_comment_is_reported(self) -> None:
        found = findings("ALTER TABLE t ADD COLUMN c INT /*!80000 , LOCK=NONE */;")
        self.assertIn("file.executable-comment", {f.rule for f in found})

    def test_delimiter_use_is_reported(self) -> None:
        found = findings("DELIMITER $$\nCREATE TRIGGER g BEFORE INSERT ON t FOR EACH ROW BEGIN END$$\n")
        self.assertIn("file.delimiter-command", {f.rule for f in found})

    def test_unterminated_string_is_critical(self) -> None:
        found = findings("UPDATE t SET a = 'oops;")
        self.assertIn("sql.unterminated", {f.rule for f in found})

    def test_use_statement_is_informational(self) -> None:
        found = [f for f in findings("USE shop;") if f.rule == "file.use-statement"]
        self.assertIs(found[0].severity, Severity.INFO)

    def test_findings_are_sorted_worst_first(self) -> None:
        sql = "SET sql_mode = '';\nUPDATE t SET a = 1;\nDROP TABLE x;\n"
        severities = [f.severity for f in findings(sql)]
        self.assertEqual(severities, sorted(severities, reverse=True))

    def test_two_cheap_alters_are_not_reported_as_repeated(self) -> None:
        # ADD COLUMN ... ALGORITHM=INSTANT and ADD INDEX ... LOCK=NONE cannot be
        # one statement, because the strictest algorithm wins and INSTANT would
        # be lost. Telling the author to merge them would make it worse.
        sql = (
            "SET SESSION lock_wait_timeout = 5;\n"
            "ALTER TABLE t ADD COLUMN c INT NULL, ALGORITHM=INSTANT;\n"
            "ALTER TABLE t ADD INDEX i (c), ALGORITHM=INPLACE, LOCK=NONE;\n"
        )
        self.assertNotIn("ddl.repeated-alter", rules_fired(sql))

    def test_two_rebuilding_alters_are_reported(self) -> None:
        sql = (
            "ALTER TABLE t MODIFY COLUMN a INT;\n"
            "ALTER TABLE t MODIFY COLUMN b INT;\n"
        )
        self.assertIn("ddl.repeated-alter", rules_fired(sql))


class RegistryCoverageTests(unittest.TestCase):
    """Every registered rule must be able to fire.

    A rule that can never produce a finding is worse than a missing one: it looks
    like coverage in the source and protects nobody. The fixture below breaks
    every rule at once, so a typo in a token-kind comparison or an unreachable
    branch shows up here rather than in production.
    """

    EVERYTHING_WRONG = """
-- migration-guard: allow(ddl.not-a-real-rule)
-- migration-guard: allow()
USE shop;
SET foreign_key_checks = 0;
SET sql_mode = '';
ALTER TABLE orders MODIFY COLUMN total DECIMAL(12,2) NOT NULL;
ALTER TABLE orders ADD COLUMN note TEXT NOT NULL;
ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE orders CONVERT TO CHARACTER SET utf8mb4;
ALTER TABLE orders DROP COLUMN legacy;
ALTER TABLE orders ADD UNIQUE INDEX uq_a (a) /*!80000 , LOCK=NONE */;
ALTER TABLE orders DROP INDEX idx_old;
ALTER TABLE orders RENAME TO orders_v2;
ALTER TABLE orders FORCE;
ALTER TABLE orders MODIFY COLUMN total INT, ALGORITHM=INSTANT;
DROP TABLE legacy_archive;
TRUNCATE TABLE staging;
UPDATE orders SET status = 'x';
DELETE FROM items;
DELETE FROM items WHERE note LIKE '%x';
ALTER TABLE items ADD FULLTEXT INDEX ft (body);
ALTER TABLE items PARTITION BY HASH (id) PARTITIONS 4;
DROP INDEX idx ON other;
DELIMITER $$
SELECT 1$$
DELIMITER ;
UPDATE t SET a = 'unterminated;
"""

    def test_every_rule_id_is_unique_and_documented(self) -> None:
        self.assertEqual(len(RULE_IDS), len(set(RULE_IDS)))
        self.assertEqual(set(RULE_IDS), set(RULE_IDS))

    def test_the_fixture_fires_every_registered_rule(self) -> None:
        fired = rules_fired(self.EVERYTHING_WRONG)
        missing = sorted(set(RULE_IDS) - fired)
        self.assertEqual(
            missing,
            [],
            "these rules never fired on the everything-wrong fixture: " + ", ".join(missing),
        )

    def test_no_rule_invented_an_undocumented_id(self) -> None:
        fired = rules_fired(self.EVERYTHING_WRONG)
        self.assertEqual(sorted(fired - set(RULE_IDS)), [])

    def test_the_registry_is_not_empty(self) -> None:
        self.assertGreaterEqual(len(RULES), 20)


class SuppressionTests(unittest.TestCase):
    def test_parses_a_directive(self) -> None:
        directive = parse_directive(
            "-- migration-guard: allow(ddl.table-rebuild, ddl.drop-table) reason: tiny lookup table",
            3,
        )
        self.assertIsNotNone(directive)
        assert directive is not None
        self.assertEqual(directive.rules, ("ddl.table-rebuild", "ddl.drop-table"))
        self.assertEqual(directive.reason, "tiny lookup table")
        self.assertFalse(directive.is_file_scoped)

    def test_allow_file_is_file_scoped(self) -> None:
        directive = parse_directive("# migration-guard: allow-file(file.use-statement)", 1)
        self.assertTrue(directive is not None and directive.is_file_scoped)

    def test_non_directive_comments_are_ignored(self) -> None:
        self.assertIsNone(parse_directive("-- just a note", 1))

    def test_a_trailing_directive_silences_its_statement(self) -> None:
        sql = (
            "ALTER TABLE t MODIFY COLUMN c INT;  "
            "-- migration-guard: allow(ddl.table-rebuild) reason: 40 rows\n"
        )
        self.assertNotIn("ddl.table-rebuild", rules_fired(sql))

    def test_a_leading_directive_silences_the_next_statement(self) -> None:
        sql = (
            "-- migration-guard: allow(ddl.table-rebuild) reason: 40 rows\n"
            "ALTER TABLE t MODIFY COLUMN c INT;\n"
        )
        self.assertNotIn("ddl.table-rebuild", rules_fired(sql))

    def test_a_file_directive_silences_everything(self) -> None:
        sql = (
            "-- migration-guard: allow-file(ddl.table-rebuild) reason: generated\n"
            "\n"
            "ALTER TABLE t MODIFY COLUMN c INT;\n"
            "ALTER TABLE u MODIFY COLUMN d INT;\n"
        )
        self.assertNotIn("ddl.table-rebuild", rules_fired(sql))

    def test_suppression_does_not_leak_to_other_rules(self) -> None:
        sql = (
            "ALTER TABLE t MODIFY COLUMN c INT;  "
            "-- migration-guard: allow(ddl.table-rebuild) reason: small\n"
        )
        fired = rules_fired(sql)
        self.assertIn("ddl.no-lock-wait-timeout", fired)

    def test_unknown_rule_in_a_directive_is_reported(self) -> None:
        sql = (
            "ALTER TABLE t ADD COLUMN c INT;  "
            "-- migration-guard: allow(ddl.typo-here) reason: oops\n"
        )
        self.assertIn("directive.unknown-rule", rules_fired(sql))

    def test_missing_reason_is_reported(self) -> None:
        sql = (
            "ALTER TABLE t ADD COLUMN c INT;  "
            "-- migration-guard: allow(ddl.table-rebuild)\n"
        )
        self.assertIn("directive.missing-reason", rules_fired(sql))

    def test_directive_rules_cannot_be_suppressed(self) -> None:
        # Otherwise one malformed directive could silence the check that
        # reports malformed directives.
        sql = (
            "-- migration-guard: allow-file(directive.unknown-rule) reason: trying it on\n"
            "\n"
            "ALTER TABLE t ADD COLUMN c INT;  -- migration-guard: allow(ddl.nope)\n"
        )
        self.assertIn("directive.unknown-rule", rules_fired(sql))

    def test_collect_reports_every_directive(self) -> None:
        parse = parse_sql(
            "-- migration-guard: allow-file(x) reason: y\n\nALTER TABLE t ADD COLUMN c INT;\n"
        )
        suppressions = collect(parse.statements, parse.file_comments)
        self.assertEqual(len(suppressions.all), 1)
        self.assertEqual(suppressions.file_rules, {"x"})


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = review(
            [("m.sql", "ALTER TABLE t MODIFY COLUMN c INT;\nDROP TABLE x;\n")],
            version=V80,
            version_label="8.0.29",
        )

    def test_counts_and_exit_code(self) -> None:
        self.assertEqual(self.report.counts()[Severity.CRITICAL], 2)
        self.assertEqual(self.report.exit_code, 2)
        self.assertEqual(self.report.statement_count, 2)

    def test_clean_report_exits_zero(self) -> None:
        clean = review([("m.sql", "ALTER TABLE t ADD COLUMN c INT;\n")], version=V80)
        self.assertEqual(clean.exit_code, 0)
        self.assertIs(clean.status, Severity.OK)

    def test_json_round_trips(self) -> None:
        document = json.loads(json_report.render(self.report))
        self.assertEqual(document["tool"], "migration-guard")
        self.assertEqual(document["exit_code"], 2)
        self.assertEqual(len(document["findings"]), len(self.report.findings))
        self.assertIn("line", document["findings"][0])

    def test_text_names_the_file_and_line(self) -> None:
        output = text.render(self.report, color=False)
        self.assertIn("m.sql:1", output)
        self.assertIn("ddl.table-rebuild", output)
        self.assertIn("fix:", output)

    def test_text_is_pure_ascii_when_asked(self) -> None:
        output = text.render(self.report, color=False, encoding="ascii")
        self.assertTrue(output.isascii(), [c for c in output if not c.isascii()])

    def test_github_annotations_are_well_formed(self) -> None:
        output = github.render(self.report)
        first = output.splitlines()[0]
        self.assertTrue(first.startswith("::error file=m.sql,line=1,"))
        self.assertIn("title=ddl.table-rebuild", first)

    def test_github_escapes_newlines_in_a_message(self) -> None:
        output = github.render(self.report)
        for line in output.splitlines():
            if line.startswith("::"):
                self.assertNotIn("\n", line.split("::", 2)[-1])

    def test_every_format_renders(self) -> None:
        for fmt in ("text", "json", "github"):
            with self.subTest(fmt=fmt):
                self.assertTrue(render(self.report, fmt).strip())

    def test_empty_input_is_not_an_error(self) -> None:
        empty = review([("m.sql", "")], version=V80)
        self.assertEqual(empty.exit_code, 0)
        self.assertTrue(empty.files[0].notes)


if __name__ == "__main__":
    unittest.main()
