import asyncio
import os
import sys
from argparse import ArgumentParser, Namespace
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from rowx._backend import BackendMissing, CheckerBackend, UnsupportedSchemeError, backend_for
from rowx.check.config import Config, ConfigError, database_url, load_config
from rowx.check.render import render_json, render_text, strip_controls
from rowx.check.runner import CheckError, Hooks, RunResult, redact, run_check
from rowx.check.walk import discover_files

if TYPE_CHECKING:
    from argparse import _SubParsersAction
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from rowx.check.runner import ScratchBuild

EXIT_CLEAN = 0
EXIT_DIAGNOSTICS = 1
EXIT_FAILURE = 2

SCRATCH_HELP: Final[str] = (
    "build a scratch database from the migrations (the URL is a server URL), check against it, and drop it. "
    "The checker still never imports or executes your Python and never executes your checked queries, "
    "but it does execute the migration files, only in the database it created for the run, as the "
    "connecting role. Migration files are trusted code. The role needs CREATEDB."
)


def _add_check(subparsers: _SubParsersAction[ArgumentParser]) -> None:
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


def _check_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="rowx")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_check(subparsers)
    return parser


def _parser() -> ArgumentParser:
    from rowx.check.database import add_parsers as add_database_parsers  # ruff: ignore[import-outside-top-level]
    from rowx.check.migrate import add_parsers  # ruff: ignore[import-outside-top-level]

    parser = ArgumentParser(prog="rowx")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_check(subparsers)
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


class _Built(Protocol):
    def sweep(self) -> None: ...


def _scratch_hooks(
    config: Config, dsn: str
) -> tuple[_Built, Callable[[str], AbstractAsyncContextManager[ScratchBuild]]]:
    from rowx.check.migrate import load_migrator, resolve_target  # ruff: ignore[import-outside-top-level]
    from rowx.check.scratch import Scratch  # ruff: ignore[import-outside-top-level]

    built = Scratch(dsn)
    migrator = load_migrator(resolve_target(config, None))

    def _open(_opened: str) -> AbstractAsyncContextManager[ScratchBuild]:
        return built.session(migrator)

    return built, _open


def _drift_hooks(config: Config) -> tuple[type[CheckError], Callable[[str], Awaitable[int | None]]]:
    from rowx.check.drift import DriftError, check_drift  # ruff: ignore[import-outside-top-level]

    return DriftError, partial(check_drift, config)


def _check_hooks(
    config: Config,
    dsn: str,
    *,
    scratch: bool,
    drift: bool,
) -> tuple[
    CheckerBackend | None,
    _Built | None,
    Callable[[str], AbstractAsyncContextManager[ScratchBuild]] | None,
    Callable[[str], Awaitable[int | None]] | None,
    type[CheckError] | None,
]:
    use_drift = drift and not scratch and config.migrate is not None
    backend = backend_for(dsn) if scratch or use_drift else None
    if scratch:
        built, session = _scratch_hooks(config, dsn)
        return backend, built, session, None, None
    if use_drift:
        drift_error, gate = _drift_hooks(config)
        return backend, None, None, gate, drift_error
    return None, None, None, None, None


def _check(args: Namespace) -> tuple[RunResult, Path]:
    cwd = Path.cwd()
    config = load_config(cwd, args.config)
    scratch = bool(args.scratch)
    if scratch and config.migrate is None:
        msg = "`--scratch` needs a `[tool.rowx.migrate]` table in pyproject.toml"
        raise CheckError(msg)
    files = discover_files(config, cwd, args.paths)
    dsn = database_url(config, args.database_url)
    backend, built, session, gate, drift_error = _check_hooks(
        config,
        dsn,
        scratch=scratch,
        drift=not args.no_drift_check,
    )
    try:
        checked = asyncio.run(run_check(config, files, dsn, args.jobs, Hooks(gate=gate, scratch=session), backend))
    except CheckError as error:
        if drift_error is not None and isinstance(error, drift_error):
            raise
        raise CheckError(strip_controls(redact(str(error), dsn))) from None
    except BackendMissing, UnsupportedSchemeError:
        raise
    except Exception as error:  # ruff: ignore[blind-except]
        msg = f"internal error: {type(error).__name__}: {strip_controls(redact(str(error), dsn))}"
        raise CheckError(msg) from None
    else:
        return checked, config.root
    finally:
        if built is not None:
            built.sweep()


def _subcommand(handler: Callable[[Namespace], None], args: Namespace) -> int:
    try:
        handler(args)
    except (ConfigError, CheckError) as error:
        print(f"error: {strip_controls(str(error))}", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    return EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    selected = sys.argv[1:] if argv is None else argv
    parser = _check_parser() if selected[:1] == ["check"] else _parser()
    args = parser.parse_args(argv)
    if args.command == "migrate":
        from rowx.check.migrate import handle as handle_migrate  # ruff: ignore[import-outside-top-level]

        return _subcommand(handle_migrate, args)
    if args.command == "database":
        from rowx.check.database import handle as handle_database  # ruff: ignore[import-outside-top-level]

        return _subcommand(handle_database, args)
    if args.jobs < 1:
        parser.error("-j must be at least 1")
    try:
        result, root = _check(args)
    except (ConfigError, CheckError, BackendMissing, UnsupportedSchemeError) as error:
        print(f"error: {strip_controls(str(error))}", file=sys.stderr)  # ruff: ignore[print]
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
