"""Command-line interface.

Exit codes are the contract, and deliberately identical to sysprobe's:

===  ==========================================================
  0  no findings at or above ``--fail-on`` (INFO never fails)
  1  at least one WARNING
  2  at least one CRITICAL
  3  usage error, unreadable input, or no SQL files found
===  ==========================================================

A CI step needs no parsing of the output to gate on a migration, which is the
whole point of shipping this as a check rather than as a report.
"""

from __future__ import annotations

import argparse
import sys
from fnmatch import fnmatch
from pathlib import Path

from . import __version__
from .analyze import review
from .models import (
    EXIT_CRITICAL,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_WARNING,
    Severity,
)
from .report import FORMAT_CHOICES, render
from .report.text import DEFAULT_WIDTH, render as render_text, safe_write
from .rules import RULE_IDS
from .schema import parse_schema
from .statements import parse_version

PROG = "migration-guard"
FAIL_ON_CHOICES = ("warning", "critical", "never")
DEFAULT_TARGET = "8.0.29"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Review MySQL migration SQL and find the statements that will lock your "
            "production tables."
        ),
        epilog=(
            "exit codes: 0 clean, 1 warning, 2 critical, 3 error. "
            "Examples: migration-guard migrations/   ·   "
            "migration-guard --format github migrations/*.sql   ·   "
            "migration-guard --target-version 5.7 migrations/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="SQL files, or directories to search recursively for *.sql",
    )
    parser.add_argument(
        "--target-version",
        default=DEFAULT_TARGET,
        metavar="X.Y.Z",
        help=(
            f"MySQL version to judge online DDL against (default: {DEFAULT_TARGET}). "
            "Why it matters: ADD COLUMN is metadata-only on 8.0.12+ and a full rebuild on 5.7."
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching this fnmatch pattern (repeatable), e.g. '*/generated/*'",
    )
    parser.add_argument(
        "--schema",
        metavar="PATH",
        help=(
            "an existing schema to judge the migration against, as produced by "
            "'mysqldump --no-data'. Enables the type, index and column checks; without it "
            "they stay silent rather than guessing."
        ),
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="read SQL from standard input instead of files (useful in a pipe)",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "-f",
        "--format",
        choices=FORMAT_CHOICES,
        default="text",
        help="output format (default: text)",
    )
    output.add_argument("-o", "--output", metavar="PATH", help="write the report to PATH")
    output.add_argument(
        "-w", "--width", type=int, default=DEFAULT_WIDTH, metavar="COLUMNS",
        help=f"wrap width for the text format (default: {DEFAULT_WIDTH})",
    )
    output.add_argument("--color", dest="color", action="store_true", default=None)
    output.add_argument("--no-color", dest="color", action="store_false")
    output.add_argument("-q", "--quiet", action="store_true", help="print nothing; exit code only")
    output.add_argument(
        "--fail-on",
        choices=FAIL_ON_CHOICES,
        default="warning",
        help="lowest severity that produces a non-zero exit code (default: warning)",
    )
    output.add_argument(
        "--list-rules", action="store_true", help="print every rule id and exit"
    )
    return parser


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #


def discover(paths: list[str], excludes: list[str]) -> list[Path]:
    """Expand directories to their ``*.sql`` files, newest name order last.

    Sorted output matters: a review whose findings move around between runs is
    impossible to diff, and migration files are conventionally name-ordered.
    """
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(path.rglob("*.sql")))
        elif path.is_file():
            found.append(path)
        else:
            raise FileNotFoundError(f"no such file or directory: {raw}")

    kept: list[Path] = []
    seen: set[str] = set()
    for path in found:
        text = path.as_posix()
        if any(fnmatch(text, pattern) or fnmatch(path.name, pattern) for pattern in excludes):
            continue
        if text in seen:
            continue
        seen.add(text)
        kept.append(path)
    return kept


def read_text(path: Path) -> str:
    """Read a file as UTF-8, tolerating a BOM and invalid bytes.

    A byte-order mark left by an editor would otherwise become part of the first
    statement; ``errors="replace"`` keeps one bad byte from failing the review.
    """
    return path.read_text(encoding="utf-8-sig", errors="replace")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _apply_fail_on(report_status: Severity, fail_on: str) -> int:
    if fail_on == "never":
        return EXIT_OK
    if fail_on == "critical":
        return EXIT_CRITICAL if report_status is Severity.CRITICAL else EXIT_OK
    if report_status is Severity.WARNING:
        return EXIT_WARNING
    if report_status is Severity.CRITICAL:
        return EXIT_CRITICAL
    return EXIT_OK


def _emit(text: str, output: str | None) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        return
    safe_write(sys.stdout, text)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code rather than calling ``sys.exit``."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_rules:
        lines = ["Rules that can be suppressed with 'migration-guard: allow(...)':"]
        lines += [f"  {rule}" for rule in RULE_IDS]
        _emit("\n".join(lines) + "\n", args.output)
        return EXIT_OK

    if args.paths and args.stdin:
        sys.stderr.write(f"{PROG}: give either PATH arguments or --stdin, not both\n")
        return EXIT_ERROR

    try:
        if args.stdin:
            files = [("<stdin>", sys.stdin.read())]
        else:
            if not args.paths:
                sys.stderr.write(
                    f"{PROG}: no input. Pass SQL files or a directory, or use --stdin.\n"
                )
                return EXIT_ERROR
            paths = discover(args.paths, args.exclude)
            if not paths:
                sys.stderr.write(f"{PROG}: no .sql files found under the given paths\n")
                return EXIT_ERROR
            files = [(path.as_posix(), read_text(path)) for path in paths]
    except FileNotFoundError as exc:
        sys.stderr.write(f"{PROG}: {exc}\n")
        return EXIT_ERROR
    except OSError as exc:
        sys.stderr.write(f"{PROG}: cannot read input: {exc}\n")
        return EXIT_ERROR

    version = parse_version(args.target_version)

    schema = None
    if args.schema:
        try:
            schema = parse_schema(read_text(Path(args.schema)), source=args.schema)
        except OSError as exc:
            sys.stderr.write(f"{PROG}: cannot read --schema {args.schema}: {exc}\n")
            return EXIT_ERROR
        if not len(schema):
            sys.stderr.write(
                f"{PROG}: --schema {args.schema} contains no CREATE TABLE statements\n"
            )
            return EXIT_ERROR

    report = review(
        files,
        version=version,
        version_label=args.target_version,
        schema=schema,
        schema_source=args.schema or "",
    )

    if not args.quiet:
        if args.format == "text":
            # A file is always written as UTF-8; stdout may be a console whose
            # code page cannot represent every glyph.
            target = "utf-8" if args.output else getattr(sys.stdout, "encoding", None)
            text = render_text(report, width=args.width, color=args.color, encoding=target)
        else:
            text = render(report, args.format)
        _emit(text, args.output)

    return _apply_fail_on(report.status, args.fail_on)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
