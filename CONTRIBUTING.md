# Contributing to mysql-migration-guard

Thanks for taking a look. This project has a few conventions that are not
obvious from the code, and following them will save us both a review round.

## The four rules

**1. No third-party dependencies.**

Not for the library, not for the tests, and not in CI beyond the linter. The
value of this tool is that it runs in a CI step that has Python and nothing
else. If you reach for `sqlparse`, `click` or `rich`, the answer is to write the
twenty lines yourself.

That extends to the test suite: `python -m unittest` and no install step.
`tests/__init__.py` puts `src` on the path.

**2. Rules must be able to fire, and something must prove it.**

`tests/test_rules.py::RegistryCoverageTests` feeds one fixture that breaks every
rule at once and asserts that each registered rule produced a finding. This is
not ceremony. During development two rules compared a token kind against the
string `"NUMBER"` instead of the `"number"` constant: they could never fire, they
looked correct in review, and they protected nobody. If you add a rule and the
coverage test does not mention it, add it to that fixture.

**3. Every finding carries the safe alternative.**

A reviewer that says "this ALTER rebuilds the table" and stops has moved the work
onto the reader. End each finding with the concrete command or the rewritten
statement. If you cannot think of one, the rule probably is not worth adding.

**4. Where MySQL's documentation is conditional, take the worse case.**

"Depends on the storage engine", "unless the table has a full-text index", "only
if the column is added last" — classify the operation by its worst plausible
outcome. Understating a lock causes a production incident; overstating it causes
an annoying warning. When you genuinely cannot tell, return `UNKNOWN` and let
`ddl.implicit-algorithm` surface it, rather than guessing either way.

## Getting set up

```bash
git clone https://github.com/zekia0104/mysql-migration-guard guard
cd guard

# Tests: no install required
python -m unittest discover -s tests -t . -v

# Optional lint
python -m pip install ruff
ruff check .
```

Running the CLI needs the package importable. Either install it
(`python -m pip install -e .`) or point at the source tree:

```bash
PYTHONPATH=src python -m migration_guard examples/bad
```

## Adding a rule

1. Pick a stable, namespaced id: `subsystem.thing` (`ddl.table-rebuild`,
   `dml.delete-without-where`). These are user-facing — they are what people put
   in `allow(...)` — so they are effectively API.
2. Write the rule in `src/migration_guard/rules.py`, add it to `RULES` in the
   right severity order, and add its id to `RULE_IDS`. The test
   `test_no_rule_invented_an_undocumented_id` will fail if you forget the second.
3. Add a case to `RegistryCoverageTests.EVERYTHING_WRONG` so the rule is proven
   to fire.
4. Write a focused test for the rule in `RuleTests`: one that fires and one that
   stays quiet at a plausible-but-safe value. The second matters more — a rule
   that fires on everything is quickly suppressed and then protects nobody.
5. If the rule needs new statement understanding, put that in `statements.py` as
   a pure function and test it there. Rules should not parse tokens themselves;
   they read the shape that `statements.py` produced.

**If the rule changes a verdict** — a migration that used to be clean is now
flagged, or the other way round — say so explicitly in the commit body. That is
the line people will search for when their pipeline starts failing.

## Changing the online-DDL matrix

`classify_alter_option` and its helpers are the domain knowledge the whole tool
rests on. When you change a classification:

- cite the page of MySQL's "Online DDL Operations" documentation you based it on
  in the docstring or the commit message;
- say which server versions it applies to, and update `classify_add_column` /
  `classify_drop_column` rather than special-casing a version inline;
- add a boundary test. `test_positioned_add_column_only_became_instant_in_8029`
  is the model: it asserts the version on both sides of the change, because the
  boundary is exactly where a plausible mistake lives.

## Changing the lexer

`sqltext.py` is where a quiet mistake becomes a confident wrong review. If you
touch it:

- add a test for the construct you changed, phrased as a property rather than an
  implementation detail (`test_semicolon_inside_a_string_does_not_split`, not
  `test_scanner_index_advances`);
- keep the scanner total: it must never raise, and it must never fail to make
  progress. There is a guard in `run()` for the second one — leave it in place;
- remember that `DELIMITER` changes what ends a statement, and that a delimiter
  made of identifier characters (`$$`) can be glued to the identifier in front
  of it.

## Style

- Ruff with the configuration in `pyproject.toml`; line length 100.
- Type annotations on public functions, `from __future__ import annotations` at
  the top of every module.
- Comments explain *why*, not *what*. The best comment in this codebase is the
  one in `_word()` explaining that `$` is a legal identifier character and would
  otherwise swallow the `$$` delimiter.
- Error messages name the fix, not just the fault.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/):

```
feat(rules): flag ALTER TABLE ... ORDER BY as a rebuild
fix(sqltext): split on a delimiter glued to the preceding identifier
docs(readme): document --target-version with the 5.7 example
test(rules): cover two cheap alters not being reported as repeated
```

## Pull requests

Before opening one:

- [ ] `python -m unittest discover -s tests -t .` passes
- [ ] `ruff check .` is clean
- [ ] New rules are in `RULE_IDS` and in the coverage fixture
- [ ] `examples/good` still exits 0 on 8.0 and `examples/bad` still exits 2
- [ ] No new dependency, and no network access anywhere
- [ ] `README.md` and `CHANGELOG.md` updated for anything a user would notice

## Reporting a bug

Please include the SQL that reproduces it (redact names if you need to), the
output of `migration-guard --version`, and the `--target-version` you used. A
wrong *verdict* is more useful to report than a crash: it usually means a hole in
the DDL matrix, and those are the bugs that matter most here.

## Code of conduct

Be decent to each other.
