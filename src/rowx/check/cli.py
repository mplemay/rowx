import asyncio
import os
import sys
from argparse import Action, ArgumentParser, BooleanOptionalAction, Namespace
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from importlib.metadata import version
from json import dumps
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, override

from rowx_core.backend import UnsupportedCapabilityError

from rowx._backend import BackendMissing, UnsupportedSchemeError, backend_for
from rowx.check.config import Config, ConfigError, database_url, load_config
from rowx.check.embedded import embedded_for, redact_owned
from rowx.check.model import Diagnostic, Span
from rowx.check.render import Report, render_json, render_text, strip_controls
from rowx.check.rules import apply_levels, resolve_levels
from rowx.check.runner import CheckError, Hooks, RunResult, run_check
from rowx.check.suppress import apply_suppressions
from rowx.check.walk import discover_files

if TYPE_CHECKING:
    from argparse import _SubParsersAction
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from rowx_core.backend import EmbeddedService, ScratchBuild, ScratchService

EXIT_CLEAN: Final[int] = 0
EXIT_DIAGNOSTICS: Final[int] = 1
EXIT_FAILURE: Final[int] = 2

SCRATCH_HELP: Final[str] = (
    "build a scratch database from the migrations (the URL is a server URL), check against it, and drop it. "
    "The checker still never imports or executes your Python and never executes your checked queries, "
    "but it does execute the migration files, only in the database it created for the run, as the "
    "connecting role. Migration files are trusted code. The role needs CREATEDB."
)


class _RuleAction(Action):
    @override
    def __call__(
        self, parser: ArgumentParser, namespace: Namespace, values: object, option_string: str | None = None
    ) -> None:
        del parser
        if not isinstance(values, str):
            return
        actions = getattr(namespace, self.dest, None) or []
        actions.append((values, (option_string or "").removeprefix("--")))
        setattr(namespace, self.dest, actions)


# one describe session per cpu, up to four
def default_jobs() -> int:
    return min(4, os.cpu_count() or 1)


def _add_check(subparsers: _SubParsersAction[ArgumentParser]) -> None:
    check = subparsers.add_parser("check")
    check.add_argument("--version", action="version", version=version("rowx"))
    check.add_argument("paths", nargs="*", type=Path, metavar="PATHS")
    check.add_argument("--database-url", metavar="URL")
    check.add_argument("--config", type=Path, metavar="FILE")
    check.add_argument("--output-format", choices=("text", "json"), default="text")
    check.add_argument("--statistics", action="store_true")
    for flag in ("error", "warn", "ignore"):
        check.add_argument(f"--{flag}", dest="rule_actions", action=_RuleAction, metavar="RULE")
    check.add_argument("--exit-zero-on-warning", action="store_true")
    check.add_argument("--respect-ignore-files", action=BooleanOptionalAction, default=None)
    check.add_argument("--force-exclude", action="store_true", default=None)
    check.add_argument("--inspect", action="store_true", help="show described query contracts")
    check.add_argument("--coverage", action="store_true", help="report recognized query sites and comparison coverage")
    check.add_argument(
        "--require-checked", action="store_true", help="require at least one successfully described query"
    )
    check.add_argument("--disallow-unchecked", action="store_true", help="reject recognized query_unchecked calls")
    check.add_argument("--scratch", action="store_true", help=SCRATCH_HELP)
    check.add_argument("--no-drift-check", action="store_true")
    check.add_argument("-j", "--jobs", type=int, default=default_jobs(), metavar="N")


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
    compat = subparsers.add_parser("check-compat")
    compat.add_argument("--against", required=True)
    compat.add_argument("--head", required=True)
    compat.add_argument("--database-url", metavar="URL")
    compat.add_argument("-j", "--jobs", type=int, default=default_jobs(), metavar="N")
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
            lines.append(f"    row contract: {site.row_comparison}")
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


def _inspect(result: RunResult) -> str:
    def quoted(value: str) -> str:
        return strip_controls(dumps(value, ensure_ascii=False))

    lines: list[str] = []
    for site in result.inspection:
        path = site.path
        span = site.span
        lines.append(f"query {quoted(str(path))}:{span.line}:{span.col}-{span.end_line}:{span.end_col}")
        for index, column in enumerate(site.columns, 1):
            nullable = "nullable" if column.nullable else "not nullable"
            mapping = quoted(column.python_type) if column.python_type is not None else "unknown"
            lines.append(
                f"  column {index}: {quoted(column.name)}, SQL {quoted(column.sql_type)}, Python {mapping}, "
                f"{nullable}, {quoted(column.reason)}"
            )
        for index, param in enumerate(site.parameters, 1):
            py = quoted(param.python_type) if param.python_type is not None else "unknown"
            status = (
                "not compared" if param.mismatch is None else "mismatch" if param.mismatch else "no mismatch reported"
            )
            lines.append(f"  parameter ${index}: SQL {quoted(param.sql_type)}, Python {py}, {status}")
    return "\n".join(lines) + ("\n" if lines else "")


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
    # the URLs of the databases that the run owns (an embedded socket DSN), redacted from every message
    owned: list[str] = field(default_factory=list)


def _scratch_hook(config: Config, state: _HookState) -> Callable[[str, int], AbstractAsyncContextManager[ScratchBuild]]:
    def _open(url: str, workers: int) -> AbstractAsyncContextManager[ScratchBuild]:
        from rowx.check.migrate import load_migrator, resolve_target  # ruff: ignore[import-outside-top-level]
        from rowx.check.scratch import Scratch  # ruff: ignore[import-outside-top-level]

        backend = backend_for(url)
        scratch = Scratch(url, backend.admin(url))
        # recorded before the session so an interrupted drop is still swept
        state.built = scratch
        return scratch.session(load_migrator(resolve_target(config, None), url, backend), workers)

    return _open


def _embedded_scratch_hook(
    config: Config, state: _HookState, service: EmbeddedService
) -> Callable[[str, int], AbstractAsyncContextManager[ScratchBuild]]:
    @asynccontextmanager
    async def _open(url: str, workers: int) -> AsyncGenerator[ScratchBuild]:
        from rowx.check.embedded import scratch_session  # ruff: ignore[import-outside-top-level]
        from rowx.check.migrate import load_migrator, resolve_target  # ruff: ignore[import-outside-top-level]

        scratch: ScratchService = service.scratch()
        # recorded before the session so an interrupted close is still swept
        state.built = scratch
        migrator = load_migrator(resolve_target(config, None), url, backend_for(url))
        async with scratch_session(scratch, migrator, workers) as build:
            state.owned.extend(build.urls)
            yield build

    return _open


def _embedded_open_hook(
    state: _HookState, service: EmbeddedService
) -> Callable[[str], AbstractAsyncContextManager[str]]:
    @asynccontextmanager
    async def _open(_url: str) -> AsyncGenerator[str]:
        from rowx.check.embedded import open_instance  # ruff: ignore[import-outside-top-level]

        # recorded before the start so a second Ctrl-C, which cannot wait for a statement, still stops the instance
        state.built = service
        async with open_instance(service) as url:
            state.owned.append(url)
            yield url

    return _open


def _drift_hook(config: Config, state: _HookState) -> Callable[[str], Awaitable[int | None]]:
    async def _gate(url: str) -> int | None:
        from rowx.check.drift import DriftError, check_drift  # ruff: ignore[import-outside-top-level]

        # the class is recorded before check_drift so a DriftError is re-raised without redaction
        state.drift_error = DriftError
        return await check_drift(config, url)

    return _gate


def _check_hooks(
    config: Config,
    state: _HookState,
    *,
    scratch: bool,
    drift: bool,
    embedded: EmbeddedService | None = None,
) -> Hooks:
    if embedded is not None and not embedded.persistent:
        # an in-memory database is built from the migrations, so there is nothing to drift from
        return Hooks(scratch=_embedded_scratch_hook(config, state, embedded))
    if scratch:
        return Hooks(scratch=_scratch_hook(config, state))
    gate = _drift_hook(config, state) if drift and config.migrate is not None else None
    if embedded is not None:
        # the directory's instance is started once, and the gate and every query are described on it
        return Hooks(gate=gate, own=_embedded_open_hook(state, embedded))
    return Hooks(gate=gate)


def _redacted(text: str, dsn: str, state: _HookState) -> str:
    return redact_owned(text, dsn, state.owned)


# whether the run builds a scratch database, or a usage error
def _scratch_wanted(config: Config, embedded: EmbeddedService | None, *, requested: bool) -> bool:
    if embedded is not None and embedded.persistent and requested:
        msg = "`--scratch` builds a database from the migrations; an embedded directory URL is checked as it is"
        raise CheckError(msg)
    # an in-memory database only exists to be built from the migrations
    implied = embedded is not None and not embedded.persistent
    if (requested or implied) and config.migrate is None:
        reason = "an in-memory embedded URL implies `--scratch`, which" if implied else "`--scratch`"
        msg = f"{reason} needs a `[tool.rowx.migrate]` table in pyproject.toml"
        raise CheckError(msg)
    return requested or implied


def _check(args: Namespace) -> tuple[RunResult, Path]:
    cwd = Path.cwd()
    config = load_config(cwd, args.config)
    args.levels = resolve_levels(config.rules, args.rule_actions or [])
    args.error_on_warning = config.error_on_warning and not args.exit_zero_on_warning
    config = replace(
        config,
        respect_ignore_files=config.respect_ignore_files
        if args.respect_ignore_files is None
        else args.respect_ignore_files,
        force_exclude=config.force_exclude if args.force_exclude is None else args.force_exclude,
    )
    dsn = database_url(config, args.database_url)
    embedded = embedded_for(dsn)
    scratch = _scratch_wanted(config, embedded, requested=bool(args.scratch))
    files = discover_files(config, cwd, args.paths)
    state = _HookState()
    try:
        checked = asyncio.run(
            run_check(
                config,
                files,
                dsn,
                args.jobs,
                _check_hooks(config, state, scratch=scratch, drift=not args.no_drift_check, embedded=embedded),
                inspect=args.inspect,
            ),
        )
    except CheckError as error:
        if state.drift_error is not None and isinstance(error, state.drift_error):
            raise
        raise CheckError(strip_controls(_redacted(str(error), dsn, state))) from None
    except BackendMissing, UnsupportedSchemeError, UnsupportedCapabilityError:
        raise
    except Exception as error:  # ruff: ignore[blind-except]
        msg = f"internal error: {type(error).__name__}: {strip_controls(_redacted(str(error), dsn, state))}"
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


def _compat(args: Namespace) -> int:
    from rowx.check.compat import handle  # ruff: ignore[import-outside-top-level]

    try:
        return handle(args)
    except (ConfigError, CheckError, BackendMissing, UnsupportedSchemeError, UnsupportedCapabilityError) as error:
        print(f"error: {strip_controls(str(error))}", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)  # ruff: ignore[print]
        return EXIT_FAILURE


def main(argv: list[str] | None = None) -> int:
    selected = sys.argv[1:] if argv is None else argv
    parser = _check_parser() if selected[:1] == ["check"] else _parser()
    args = parser.parse_args(argv)
    if args.command == "check-compat":
        if args.jobs < 1:
            parser.error("-j must be at least 1")
        return _compat(args)
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
    result.reports = apply_levels(result.reports, args.levels)
    result.reports = apply_suppressions(result.reports, result.sources, args.levels)
    result.reports = apply_levels(result.reports, args.levels)
    result.statistics.by_rule.clear()
    result.statistics.by_rule.update(report.diagnostic.rule for report in result.reports)
    _print_result(result, root, args)
    return _exit_code(result, error_on_warning=args.error_on_warning)


def _exit_code(result: RunResult, *, error_on_warning: bool) -> int:
    if any(report.operational for report in result.reports):
        return EXIT_FAILURE
    return (
        EXIT_DIAGNOSTICS
        if any(report.severity == "error" or error_on_warning for report in result.reports)
        else EXIT_CLEAN
    )


def _print_result(result: RunResult, root: Path, args: Namespace) -> None:
    if args.output_format == "json":
        sys.stdout.write(render_json(result.reports, root))
        if args.statistics:
            sys.stderr.write(_statistics(result))
        if args.coverage:
            sys.stderr.write(_coverage(result, root))
        if args.inspect:
            sys.stderr.write(_inspect(result))
    else:
        sys.stdout.write(render_text(result.reports, result.sources, root))
        if args.statistics:
            sys.stdout.write(_statistics(result))
        if args.coverage:
            sys.stdout.write(_coverage(result, root))
        if args.inspect:
            sys.stdout.write(_inspect(result))
