# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.2.0] - 2026-09-26

### Added

- **Schema-aware rules**, enabled by `--schema <dump>`. Pass the output of
  `mysqldump --no-data` and six more checks run, because half the dangerous
  changes in a migration are only dangerous relative to what is already there.

  - `ddl.narrow-type` -- `MODIFY` to a width that cannot hold the current data.
    Under strict mode the ALTER fails on the first offending row *after* the
    table has been rebuilt; without strict mode it truncates and the original
    values are gone.
  - `ddl.duplicate-index` -- an index over exactly the columns the table already
    indexes. It costs write throughput on every INSERT and buys nothing.
  - `ddl.redundant-index` -- the new index makes an existing one unnecessary,
    because the shorter one is a leading prefix of the longer.
  - `ddl.text-index-no-prefix` -- indexing a `TEXT` or `BLOB` column without a
    prefix length, which MySQL rejects with `ERROR 1170` at deploy time.
  - `ddl.index-too-long` -- a key over InnoDB's 3072-byte limit (`ERROR 1071`),
    computed from the real column widths and the table's character set.
  - `ddl.unknown-column` -- the migration names a column the schema does not
    declare, which is usually a typo and always `ERROR 1054`.

  Without `--schema` these rules stay silent rather than guessing, and the report
  records which sources were used, so a reader can tell "checked and fine" from
  "not checked".

- `examples/schema/shop.sql`, a dump written the way `mysqldump --no-data`
  writes one, so the schema-aware rules can be exercised without a database.

### Changed

- The registry coverage test now judges its fixture twice, once with a schema and
  once without, and asserts the union covers every registered rule. It reported
  all six new rules as unreachable before they were wired in, which is precisely
  what it exists for.

### Fixed

- `MODIFY COLUMN` and `CHANGE COLUMN` reported the column name in upper case,
  because it was taken from an uppercased keyword list rather than from the
  token. The same mistake had already been fixed for `ADD COLUMN`; both now read
  the name from the token, and the declared type with it, so a schema comparison
  is not sensitive to how the statement happens to be spelled.

## [0.1.0] - 2026-09-26

First release.

### Added

- **A MySQL lexer that handles what actually breaks naive splitters**:
  ``--`` comments (which require whitespace in MySQL, so ``SELECT 1--2`` is
  arithmetic), ``#`` comments, block comments, doubled-quote and backslash
  escapes inside strings, doubled-backtick escapes inside identifiers, and the
  mysql client's ``DELIMITER`` command for procedure bodies. Unterminated
  literals are reported rather than raised, and a scanner progress guard makes a
  hang impossible.

- **Executable comments are treated as code.** MySQL runs ``/*!80000 ... */``,
  so an ``ALTER TABLE ... /*!80000 , ALGORITHM=INSTANT */`` really does request
  INSTANT and the analysis must see it -- but a reviewer skimming the file will
  not. The option is honoured *and* reported.

- **An online-DDL matrix, version aware.** Every alter option is classified as
  metadata-only, in-place, in-place-but-blocking or a full rebuild against the
  target server version, because the same ``ADD COLUMN`` is instant on 8.0.29,
  instant-only-when-appended on 8.0.12, and a full rebuild on 5.7. Where MySQL's
  documentation is conditional the classification takes the worst plausible
  outcome: understating a lock causes an incident, overstating one causes an
  annoying warning.

- **27 rules**, each with the concrete alternative rather than only a complaint.
  The ones worth calling out:

  - ``ddl.table-rebuild`` -- the statement copies every row while holding writes.
  - ``ddl.no-lock-wait-timeout`` -- ``lock_wait_timeout`` defaults to a *year*,
    so a blocked DDL hangs the migration and queues every query behind it.
  - ``ddl.implicit-algorithm`` -- without ``ALGORITHM``/``LOCK``, MySQL silently
    falls back to a rebuild instead of failing.
  - ``ddl.instant-unsupported`` -- ``ALGORITHM=INSTANT`` on an operation that
    cannot be instant fails the migration at deploy time.
  - ``ddl.add-column-not-null`` -- a ``NOT NULL`` column with no default breaks
    inserts under strict mode and can diverge on row-based replication.
  - ``dml.update-without-where`` / ``dml.delete-without-where``.
  - ``session.foreign-key-checks-off``.
  - ``file.executable-comment`` -- code MySQL runs and reviewers do not see.

- **In-SQL suppressions**, the ``# noqa`` equivalent::

      ALTER TABLE country_codes MODIFY code CHAR(2);  -- migration-guard: allow(ddl.table-rebuild) reason: 249 rows

  ``allow-file(...)`` covers a whole file. Two rules audit the suppressions
  themselves -- one for a rule id that does not exist (a typo silences nothing),
  one for a missing ``reason:`` -- and the ``directive.*`` rules cannot
  themselves be suppressed.

- **Three output formats**: text for a terminal, JSON for a pipeline, and GitHub
  Actions annotations so findings land on the lines of the pull request diff.
  Output encoding is handled per character, so a report redirected on a console
  whose code page cannot hold a glyph degrades instead of failing.

- **A reusable GitHub Action** (``action.yml``) so a repository can adopt the
  check with four lines of YAML.

- **117 tests** with no dependencies and no install step, including a registry
  coverage test that proves every registered rule can fire. That test exists
  because two rules shipped in development with a token-kind comparison against
  ``"NUMBER"`` instead of the ``"number"`` constant: they never fired, looked
  green in review, and protected nobody.

[Unreleased]: https://github.com/zekia0104/Mysql/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zekia0104/Mysql/releases/tag/v0.1.0
