import asyncio
import os
import sys
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from rowx_core.backend import UnsupportedCapabilityError

from rowx._backend import BackendMissing, UnsupportedSchemeError, backend_for
from rowx.check.config import Config, ConfigError, database_url, load_config
from rowx.check.model import Diagnostic, Span
from rowx.check.render import Report, render_json, render_text, strip_controls
from rowx.check.runner import CheckError, Hooks, RunResult, redact, run_check
from rowx.check.walk import discover_files

if TYPE_CHECKING:
    from argparse import _SubParsersAction
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from rowx.check.runner import ScratchBuild

EXIT_CLEAN: Final[int] = 0
EXIT_DIAGNOSTICS: Final[int] = 1
EXIT_FAILURE: Final[int] = 2

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
    check.add_argument("--coverage", action="store_true", help="report recognized query sites and comparison coverage")
    check.add_argument(
        "--require-checked", action="store_true", help="require at least one successfully described query"
    )
    check.add_argument("--disallow-unchecked", action="store_true", help="reject recognized query_unchecked calls")
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


def _coverage(result: RunResult, root: Path) -> str:
    lines = [f"recognized query sites: {len(result.coverage)}"]
    if not result.coverage:
        lines.append("no recognized query sites in selected files")
    for site in result.coverage:
        try:
            path = site.path.relative_to(root)
        except ValueError:
            path = site.path
        span = site.span
        location = f"{strip_controls(str(path))}:{span.line}:{span.col}-{span.end_line}:{span.end_col}"
        state = "unchecked" if site.kind == "query_unchecked" else "described" if site.described else "not described"
        lines.append(f"  {location}: {state}")
        if site.described:
            lines.append(f"    row contract compared: {'yes' if site.row_compared else 'no'}")
            if not site.parameters_aligned:
                lines.append("    parameters: not aligned or inference unavailable")
            for index, param in enumerate(site.parameters, 1):
                py = param.python_type.name if param.python_type is not None else "unknown"
                status = (
                    "not compared"
                    if param.mismatch is None
                    else "mismatch"
                    if param.mismatch
                    else "no mismatch reported"
                )
                lines.append(f"    parameter ${index}: SQL {strip_controls(param.sql_type)}, Python {py}; {status}")
    return "\n".join(lines) + "\n"


def _policies(result: RunResult, root: Path, *, require_checked: bool, disallow_unchecked: bool) -> None:
    if require_checked and not any(site.described for site in result.coverage):
        result.reports.append(
            Report(
                diagnostic=Diagnostic(
                    rule="coverage-require-checked",
                    message="no query was successfully described",
                    path=root,
                    span=Span(line=1, col=1, end_line=1, end_col=2),
                )
            )
        )
    if disallow_unchecked:
        for site in result.coverage:
            if site.kind == "query_unchecked":
                result.reports.append(
                    Report(
                        diagnostic=Diagnostic(
                            rule="coverage-unchecked",
                            message="query_unchecked is disallowed",
                            path=site.path,
                            span=site.span,
                        )
                    )
                )


class _Built(Protocol):
    def sweep(self) -> None: ...


@dataclass(slots=True, kw_only=True)
class _HookState:
    built: _Built | None = None
    drift_error: type[CheckError] | None = None


def _scratch_hook(config: Config, state: _HookState) -> Callable[[str], AbstractAsyncContextManager[ScratchBuild]]:
    def _open(url: str) -> AbstractAsyncContextManager[ScratchBuild]:
        from rowx.check.migrate import load_migrator, resolve_target  # ruff: ignore[import-outside-top-level]
        from rowx.check.scratch import Scratch  # ruff: ignore[import-outside-top-level]

        backend = backend_for(url)
        scratch = Scratch(url, backend.admin(url))
        # recorded before the session so an interrupted drop is still swept
        state.built = scratch
        return scratch.session(load_migrator(resolve_target(config, None), url, backend))

    return _open


def _drift_hook(config: Config, state: _HookState) -> Callable[[str], Awaitable[int | None]]:
    async def _gate(url: str) -> int | None:
        from rowx.check.drift import DriftError, check_drift  # ruff: ignore[import-outside-top-level]

        # the class is recorded before check_drift so a DriftError is re-raised without redaction
        state.drift_error = DriftError
        return await check_drift(config, url)

    return _gate


def _check_hooks(config: Config, state: _HookState, *, scratch: bool, drift: bool) -> Hooks:
    if scratch:
        return Hooks(scratch=_scratch_hook(config, state))
    if drift and config.migrate is not None:
        return Hooks(gate=_drift_hook(config, state))
    return Hooks()


def _check(args: Namespace) -> tuple[RunResult, Path]:
    cwd = Path.cwd()
    config = load_config(cwd, args.config)
    scratch = bool(args.scratch)
    if scratch and config.migrate is None:
        msg = "`--scratch` needs a `[tool.rowx.migrate]` table in pyproject.toml"
        raise CheckError(msg)
    files = discover_files(config, cwd, args.paths)
    dsn = database_url(config, args.database_url)
    state = _HookState()
    try:
        checked = asyncio.run(
            run_check(
                config,
                files,
                dsn,
                args.jobs,
                _check_hooks(config, state, scratch=scratch, drift=not args.no_drift_check),
            ),
        )
    except CheckError as error:
        if state.drift_error is not None and isinstance(error, state.drift_error):
            raise
        raise CheckError(strip_controls(redact(str(error), dsn))) from None
    except BackendMissing, UnsupportedSchemeError, UnsupportedCapabilityError:
        raise
    except Exception as error:  # ruff: ignore[blind-except]
        msg = f"internal error: {type(error).__name__}: {strip_controls(redact(str(error), dsn))}"
        raise CheckError(msg) from None
    else:
        return checked, config.root
    finally:
        if state.built is not None:
            state.built.sweep()


def _subcommand(handler: Callable[[Namespace], None], args: Namespace) -> int:
    try:
        handler(args)
    except (ConfigError, CheckError, BackendMissing, UnsupportedSchemeError, UnsupportedCapabilityError) as error:
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
    except (ConfigError, CheckError, BackendMissing, UnsupportedSchemeError, UnsupportedCapabilityError) as error:
        print(f"error: {strip_controls(str(error))}", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    _policies(result, root, require_checked=args.require_checked, disallow_unchecked=args.disallow_unchecked)
    result.statistics.by_rule.clear()
    result.statistics.by_rule.update(report.diagnostic.rule for report in result.reports)
    _print_result(result, root, args)
    return EXIT_DIAGNOSTICS if result.reports else EXIT_CLEAN


def _print_result(result: RunResult, root: Path, args: Namespace) -> None:
    if args.output_format == "json":
        sys.stdout.write(render_json(result.reports, root))
        if args.statistics:
            sys.stderr.write(_statistics(result))
        if args.coverage:
            sys.stderr.write(_coverage(result, root))
    else:
        sys.stdout.write(render_text(result.reports, result.sources, root))
        if args.statistics:
            sys.stdout.write(_statistics(result))
        if args.coverage:
            sys.stdout.write(_coverage(result, root))
