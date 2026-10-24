import asyncio
import os
import sys
from argparse import ArgumentParser
from importlib.metadata import version
from pathlib import Path

from rowx.check.config import ConfigError, load_config
from rowx.check.render import render_json, render_text
from rowx.check.runner import CheckError, RunResult, redact, run_check
from rowx.check.walk import discover_files

EXIT_CLEAN = 0
EXIT_DIAGNOSTICS = 1
EXIT_FAILURE = 2


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
    check.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1), metavar="N")
    return parser


def _statistics(result: RunResult) -> str:
    stats = result.statistics
    lines = [
        f"files scanned: {stats.files}",
        f"call sites found: {stats.call_sites}",
        f"call sites checked: {stats.checked}",
        f"distinct SQL strings described: {stats.distinct_sql}",
        f"query_unchecked calls skipped: {stats.unchecked}",
        f"diagnostics: {sum(stats.by_rule.values())}",
    ]
    lines.extend(f"  {rule}: {count}" for rule, count in sorted(stats.by_rule.items()))
    return "\n".join(lines) + "\n"


def _check(paths: list[Path], config_file: Path | None, url: str | None, jobs: int) -> tuple[RunResult, Path]:
    cwd = Path.cwd()
    config = load_config(cwd, config_file)
    files = discover_files(config, cwd, paths)
    dsn = url or os.environ.get(config.database_url_env) or ""
    try:
        return asyncio.run(run_check(config, files, dsn, jobs)), config.root
    except CheckError as error:
        raise CheckError(redact(str(error), dsn)) from None
    except Exception as error:  # ruff: ignore[blind-except]
        msg = f"internal error: {type(error).__name__}: {redact(str(error), dsn)}"
        raise CheckError(msg) from None


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("-j must be at least 1")
    try:
        result, root = _check(args.paths, args.config, args.database_url, args.jobs)
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
