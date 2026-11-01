import asyncio
import os
import sys
from argparse import ArgumentParser, Namespace
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final

from rowx.check.config import ConfigError, database_url, load_config
from rowx.check.database import add_parsers as add_database_parsers, handle as handle_database
from rowx.check.drift import DriftError, check_drift
from rowx.check.migrate import add_parsers, handle as handle_migrate, load_migrator, resolve_target
from rowx.check.render import render_json, render_text
from rowx.check.runner import CheckError, Hooks, RunResult, redact, run_check
from rowx.check.scratch import Scratch
from rowx.check.walk import discover_files

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from rowx.check.runner import ScratchBuild
    from rowx.postgres import Migrator

EXIT_CLEAN = 0
EXIT_DIAGNOSTICS = 1
EXIT_FAILURE = 2

SCRATCH_HELP: Final[str] = (
    "build a scratch database from the migrations (the URL is a server URL), check against it, and drop it. "
    "The checker still never imports or executes your Python and never executes your checked queries, "
    "but it does execute the migration files, only in the database it created for the run, as the "
    "connecting role. Migration files are trusted code. The role needs CREATEDB."
)


def _parser() -> ArgumentParser:
    parser = ArgumentParser(prog="rowx")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--version", action="version", version=version("rowx"))
    check.add_argument("paths", nargs="*", type=Path, metavar="PATHS")
    check.add_argument("--database-url", metavar="URL")
    check.add_argument("--config", type=Path, metavar="FILE")
    check.add_argument("--output-format", choices=("text", "json"), default="text")
    check.add_argument("--statistics", action="store_true")
    check.add_argument("--scratch", action="store_true", help=SCRATCH_HELP)
    check.add_argument("--no-drift-check", action="store_true")
    check.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1), metavar="N")
    add_parsers(subparsers)
    add_database_parsers(subparsers)
    return parser


def _statistics(result: RunResult) -> str:
    stats = result.statistics
    lines = [
        f"files scanned: {stats.files}",
        f"call sites found: {stats.call_sites}",
        f"call sites checked: {stats.checked}",
        f"distinct SQL strings described: {stats.distinct_sql}",
        f"query_unchecked calls skipped: {stats.unchecked}",
    ]
    if stats.migrations is not None:
        lines.append(f"migrations checked: {stats.migrations}")
    if stats.scratch_seconds is not None:
        lines.append(f"scratch database built in: {stats.scratch_seconds:.2f}s")
    lines.append(f"diagnostics: {sum(stats.by_rule.values())}")
    lines.extend(f"  {rule}: {count}" for rule, count in sorted(stats.by_rule.items()))
    return "\n".join(lines) + "\n"


def _session(built: Scratch, migrator: Migrator, _dsn: str) -> AbstractAsyncContextManager[ScratchBuild]:
    return built.session(migrator)


def _check(args: Namespace) -> tuple[RunResult, Path]:
    cwd = Path.cwd()
    config = load_config(cwd, args.config)
    scratch = bool(args.scratch)
    if scratch and config.migrate is None:
        msg = "`--scratch` needs a `[tool.rowx.migrate]` table in pyproject.toml"
        raise CheckError(msg)
    files = discover_files(config, cwd, args.paths)
    dsn = database_url(config, args.database_url)
    # the migrations are read first, so a broken directory fails before a database is created
    built = Scratch(dsn) if scratch else None
    migrator = load_migrator(resolve_target(config, None)) if scratch else None
    # the drift gate doesn't apply, because the database is built from the files
    gate = (
        partial(check_drift, config) if not args.no_drift_check and not scratch and config.migrate is not None else None
    )
    try:
        session = partial(_session, built, migrator) if built is not None and migrator is not None else None
        return asyncio.run(run_check(config, files, dsn, args.jobs, Hooks(gate=gate, scratch=session))), config.root
    except DriftError:
        raise
    except CheckError as error:
        raise CheckError(redact(str(error), dsn)) from None
    except Exception as error:  # ruff: ignore[blind-except]
        msg = f"internal error: {type(error).__name__}: {redact(str(error), dsn)}"
        raise CheckError(msg) from None
    finally:
        if built is not None:
            built.sweep()


def _subcommand(handler: Callable[[Namespace], None], args: Namespace) -> int:
    try:
        handler(args)
    except (ConfigError, CheckError) as error:
        print(f"error: {error}", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    return EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "migrate":
        return _subcommand(handle_migrate, args)
    if args.command == "database":
        return _subcommand(handle_database, args)
    if args.jobs < 1:
        parser.error("-j must be at least 1")
    try:
        result, root = _check(args)
    except (ConfigError, CheckError) as error:
        print(f"error: {error}", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    if args.output_format == "json":
        sys.stdout.write(render_json(result.reports, root))
        if args.statistics:
            sys.stderr.write(_statistics(result))
    else:
        sys.stdout.write(render_text(result.reports, result.sources, root))
        if args.statistics:
            sys.stdout.write(_statistics(result))
    return EXIT_DIAGNOSTICS if result.reports else EXIT_CLEAN
