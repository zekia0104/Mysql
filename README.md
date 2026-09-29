# mysql-migration-guard

**Find the statements in your MySQL migration that will lock production, before they run.**

[![CI](https://github.com/zekia0104/mysql-migration-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/zekia0104/mysql-migration-guard/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

```console
$ migration-guard migrations/2026_09_26__orders_indexing.sql
CRITICAL migrations/2026_09_26__orders_indexing.sql:4  orders: MODIFY COLUMN rebuilds the whole table
         ddl.table-rebuild
         → MODIFY COLUMN total DECIMAL(12, 2) NOT NULL
         changing a column definition usually copies every row; only pure NULL/NOT NULL
         or DEFAULT changes avoid a rebuild. On MySQL 8.0.29 this copies every row while
         holding a write lock, so the table is unavailable for as long as that takes and
         the migration cannot be rolled back half way.
         fix: Run it with gh-ost or pt-online-schema-change, or split it: add a nullable
         column, backfill in batches, then change it in a maintenance window.

CRITICAL migrations/2026_09_26__orders_indexing.sql:18  UPDATE without a WHERE clause touches every row
         dml.update-without-where
         fix: Batch it: repeat `... WHERE id > ? ORDER BY id LIMIT 1000` until it affects
         no rows, with a short sleep between batches.
```

Every finding names the file, the line, the statement, why it is dangerous on
*your* server version, and the safe form of the same change.

## Why this exists

`ALTER TABLE` is the one statement in a migration that can take a database down
while succeeding. The failure modes are all quiet until they are not:

- MySQL **silently falls back to a full table rebuild** when it cannot do an
  ALTER in place. Nothing in the statement says so, and the migration that took
  40 ms in staging takes 40 minutes in production.
- `lock_wait_timeout` defaults to **31536000 seconds — a year**. If one
  long-running transaction holds a metadata lock, your migration does not fail,
  it waits, and every query behind it waits too.
- The same `ADD COLUMN` is **metadata-only on MySQL 8.0.29 and a full rebuild on
  5.7**. Whether a change is safe is a property of the server, not of the SQL.
- `UPDATE orders SET status = 'x'` without a WHERE clause is a single
  transaction that holds locks on every row and, on row-based replication,
  sends every changed row to every replica.

Existing tools cover the adjacent problems: `sqlfluff` checks SQL style,
`gh-ost` and `pt-online-schema-change` *perform* an online change, and
`pt-query-digest` looks at queries that already ran. Nothing reviews the
migration before it is applied.

## Install

No third-party dependencies. Python 3.11+ and nothing else.

```bash
pipx install git+https://github.com/zekia0104/mysql-migration-guard
# or, from a checkout
git clone https://github.com/zekia0104/mysql-migration-guard guard
cd guard && python -m pip install .
```

Or run it with no install at all:

```bash
PYTHONPATH=/path/to/mysql-migration-guard/src python -m migration_guard migrations/
```

## Usage

```console
$ migration-guard --help
usage: migration-guard [-h] [--version] [--target-version X.Y.Z] [--exclude GLOB]
                       [--stdin] [-f {text,json,github}] [-o PATH] [-w COLUMNS]
                       [--color] [--no-color] [-q] [--fail-on {warning,critical,never}]
                       [--list-rules]
                       [PATH ...]
```

```bash
migration-guard migrations/                       # review a directory recursively
migration-guard --target-version 5.7 migrations/  # you are still on 5.7
migration-guard --format github migrations/       # annotate a pull request
migration-guard --stdin < migration.sql           # in a pipe
migration-guard --list-rules                      # the 27 rule identifiers
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Clean. `INFO` findings never fail a build. |
| `1` | At least one `WARNING`. |
| `2` | At least one `CRITICAL`. |
| `3` | Usage error, unreadable input, or no SQL files found. |

`--fail-on critical` makes warnings non-fatal; `--fail-on never` always exits `0`.
No output parsing is required to gate a deploy.

### `--target-version` is not optional in spirit

```console
$ migration-guard examples/good --target-version 8.0.29 ; echo $?
0
$ migration-guard examples/good --target-version 5.7 ; echo $?
2
```

Same file, both answers correct. `examples/good` adds a nullable column and asks
for `ALGORITHM=INSTANT`, which MySQL 8.0.29 does without touching the rows and
5.7 does by copying the entire table. Reviewing a migration without saying which
server it is going to is reviewing half the problem.

## As a GitHub Action

The repository ships a composite action, so the check is four lines:

```yaml
- uses: zekia0104/mysql-migration-guard@v1
  with:
    paths: migrations
    target-version: "8.0.29"
    fail-on: warning
```

With `--format github` each finding becomes a workflow annotation attached to the
line of the diff, so a reviewer sees it in context:

```
::error file=migrations/001.sql,line=4,title=ddl.table-rebuild::orders: MODIFY COLUMN rebuilds the whole table -- ...
```

## What it checks

33 rules. The ones that catch the incidents:

| Rule | Severity | What it catches |
| --- | --- | --- |
| `ddl.table-rebuild` | critical | The statement copies every row while holding writes |
| `ddl.drop-table` | critical | `DROP TABLE`, which a rolled-back migration does not undo |
| `ddl.drop-column` | critical | Data loss, and every query naming the column breaks |
| `ddl.change-charset` | critical | `CONVERT TO CHARACTER SET` rewrites and can corrupt data |
| `dml.update-without-where` | critical | One transaction holding a lock on every row |
| `dml.delete-without-where` | critical | The same, and unbounded replication lag |
| `session.foreign-key-checks-off` | critical | Orphan rows accepted silently |
| `sql.unterminated` | critical | SQL that does not lex cannot be reviewed |
| `ddl.blocks-writes` | warning | In-place but stops writes while it runs |
| `ddl.implicit-algorithm` | warning | No `ALGORITHM`/`LOCK`: MySQL falls back silently instead of failing |
| `ddl.instant-unsupported` | warning | `ALGORITHM=INSTANT` on an operation that cannot be instant |
| `ddl.no-lock-wait-timeout` | warning | DDL with the default one-year metadata lock wait |
| `ddl.add-column-not-null` | warning | `NOT NULL` with no default |
| `ddl.foreign-key` | warning | Validates every existing row and serialises writes |
| `ddl.drop-index` | warning | Queries silently become table scans |
| `ddl.truncate` | warning | Implicit commit, no rollback, `AUTO_INCREMENT` reset |
| `ddl.rename-table` | warning | Breaks every view, trigger and consumer naming the table |
| `dml.unbounded-predicate` | warning | `LIKE '%x'`, `!=`, `NOT IN`, functions on the column |
| `file.executable-comment` | warning | Code MySQL runs but a reviewer skims past |
| `session.sql-mode-relaxed` | warning | Strict mode disabled inside the migration |
| `ddl.repeated-alter` | info | Two rebuilding statements where one would do |
| `dml.delete-without-limit` | info | Unbounded DELETE that could be batched |
| `file.use-statement` | info | A migration picking its own schema |
| `file.delimiter-command` | info | `DELIMITER` is a client command, not SQL |
| `directive.unknown-rule` | warning | A suppression that silences nothing |
| `directive.missing-reason` | info | An exception nobody can later justify |
| `directive.empty` | warning | `allow()` with no rule names |

Run `migration-guard --list-rules` for the exact identifiers.

### Design decisions worth knowing

- **The worst plausible outcome wins.** Where MySQL's documentation is
  conditional — "in place, unless the table has a full-text index" — the
  classification takes the worse case. A review tool that understates a lock
  causes an incident; one that overstates it causes an annoying warning.
- **Unrecognised options are reported as unrecognised**, not assumed safe. A
  rule surfaces them so you know what was *not* checked.
- **Executable comments are treated as code.** MySQL runs `/*!80000 ... */`, so
  `ALTER TABLE t ADD COLUMN c INT /*!80000 , ALGORITHM=INSTANT */` really does
  request INSTANT. The option is honoured *and* reported, because a human
  reading the file will not see it.
- **`--` needs whitespace to start a comment.** `SELECT 1--2` is arithmetic.
  A splitter that gets this wrong cuts statements in the wrong place and reports
  confident nonsense.
- **A statement without a terminator is still a statement.** Migration runners
  accept a final statement with no `;`; silently dropping it would be the worst
  failure this tool could have.
- **The scanner cannot hang.** A progress guard forces the index forward if a
  construct ever consumes nothing, because a hang is the worst possible failure
  for a tool that runs in CI.

## Suppressing a finding

Some ALTERs really are fine — a 40-row lookup table can be rebuilt whenever you
like. Say so, in the file, with a reason:

```sql
ALTER TABLE country_codes MODIFY code CHAR(2);  -- migration-guard: allow(ddl.table-rebuild) reason: 249 rows, static
```

```sql
-- migration-guard: allow-file(file.use-statement) reason: generated from the schema dump

USE shop;
```

The `directive.*` rules cannot themselves be suppressed, so a typo in a rule id is
always reported rather than quietly protecting nothing.

## JSON output

```console
$ migration-guard --format json migrations/ | jq '{exit_code, counts}'
{
  "exit_code": 2,
  "counts": { "critical": 2, "warning": 4, "info": 1 }
}
```

Each finding carries `rule`, `severity`, `file`, `line`, `scope` (the table),
`snippet` and `hint`, so a dashboard or a policy gate can act without parsing
prose.

## Limitations

Stated plainly, because a check that overstates itself is worse than none:

- **No database connection.** The review is entirely static, so the tool does not
  know how many rows a table has. `ddl.table-rebuild` on a 50-row table is
  harmless — that is what `allow(...)` is for.
- **The type and index checks need `--schema`.** Without it the six schema-aware
  rules stay silent rather than guessing, and the report says so. The input is a
  `mysqldump --no-data` file, so still no connection, no driver, no credentials:

  ```bash
  mysqldump --no-data --skip-comments --compact shop > schema.sql
  migration-guard --schema schema.sql migrations/
  ```

  With it, `examples/bad` reports six findings it cannot see otherwise, including
  an index over a `TEXT` column and a `MODIFY` that narrows a `DECIMAL(12,2)`
  to `DECIMAL(8,2)`.
- **Predicate analysis is heuristic.** Without the schema there is no way to know
  whether the optimizer can use an index, so `dml.unbounded-predicate` reports
  what a human would notice on a first read and says "verify with EXPLAIN".
- **MySQL, not MariaDB.** The online-DDL matrix and the executable-comment
  handling follow MySQL's documentation. MariaDB's `/*M! */` comments are
  recognised, but the matrix is not tuned for it.
- **It reviews statements, not sequences.** A migration that is only safe when
  applied after another one will not be understood as such.

## Development

```bash
git clone https://github.com/zekia0104/mysql-migration-guard guard
cd guard
python -m unittest discover -s tests -t . -v   # 119 tests, no dependencies
ruff check .                                    # optional lint
```

No install step is needed: `tests/__init__.py` puts `src` on the path, and the
tests build their own SQL as strings.

Two files are worth reading first:

- `src/migration_guard/sqltext.py` — the lexer, and the place where a quiet
  mistake becomes a confident wrong review.
- `src/migration_guard/statements.py` — the online-DDL matrix, which is the
  domain knowledge the whole tool rests on.

The test suite includes a **registry coverage test**: one fixture breaks every
rule at once and the test asserts that each registered rule produced a finding.
It earns its place — during development two rules compared a token kind against
`"NUMBER"` instead of the `"number"` constant, so they could never fire, looked
green in review and protected nobody.

## License

[MIT](LICENSE) © 2026 zekia0104
