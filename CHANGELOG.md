# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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

[Unreleased]: https://github.com/zekia0104/mysql-migration-guard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zekia0104/mysql-migration-guard/releases/tag/v0.1.0
