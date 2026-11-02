import asyncio
import json
import sys
from argparse import ArgumentTypeError, BooleanOptionalAction, Namespace
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Final

from rowx.check.config import Config, MigrateConfig, database_url, load_config
from rowx.check.runner import CheckError, redact
from rowx.errors import DatabaseError, InterfaceError, MigrateError
from rowx.postgres import Migrator, connect

if TYPE_CHECKING:
    from argparse import ArgumentParser, _SubParsersAction
    from collections.abc import Awaitable, Callable, Coroutine, Iterable

    from rowx.postgres import Connection, Migration, MigrationResult, MigrationStatus

TIMESTAMP_FORMAT: Final[str] = "%Y%m%d%H%M%S"
SEQUENTIAL_WIDTH: Final[int] = 4
SHORT_HEX: Final[int] = 8
MICROS_PER_MILLI: Final[int] = 1000
MICROS_PER_SECOND: Final[int] = 1_000_000
PLACEHOLDERS: Final[dict[str, str]] = {
    "simple": "-- Add migration script here\n",
    "up": "-- Add up migration script here\n",
    "down": "-- Add down migration script here\n",
}
RESERVED_SUFFIXES: Final[tuple[str, ...]] = (".up", ".down")
DEFAULT_CONNECT_TIMEOUT: Final[float] = 10.0
STARTING_UP: Final[str] = "57P03"
RETRY_FIRST: Final[float] = 0.05
RETRY_MAX: Final[float] = 0.5
NANOS_PER_MICRO: Final[int] = 1000
TIP: Final[str] = (
    "tip: add a [tool.rowx.migrate] table to pyproject.toml to set the migrations directory "
    "and turn on the drift gate, `rowx check --scratch` and the pytest plugin\n"
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True, kw_only=True, frozen=True)
class Target:
    directory: Path
    migrate: MigrateConfig


def add_parsers(subparsers: _SubParsersAction[ArgumentParser]) -> None:
    migrate = subparsers.add_parser("migrate")
    commands = migrate.add_subparsers(dest="migrate_command", required=True)

    add = commands.add_parser("add")
    add.add_argument("description")
    kind = add.add_mutually_exclusive_group()
    kind.add_argument("--reversible", action="store_true")
    kind.add_argument("--simple", action="store_true")
    versioning = add.add_mutually_exclusive_group()
    versioning.add_argument("--timestamp", action="store_true")
    versioning.add_argument("--sequential", action="store_true")
    add.add_argument("--config", type=Path, metavar="FILE")
    add.add_argument("--source", type=Path, metavar="DIR")

    run = commands.add_parser("run")
    skip = commands.add_parser("skip")
    for parser in (run, skip):
        parser.add_argument("--target-version", type=int, metavar="V")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--allow-out-of-order", action=BooleanOptionalAction)

    revert = commands.add_parser("revert")
    revert_target = revert.add_mutually_exclusive_group()
    revert_target.add_argument("--target-version", type=int, metavar="V")
    revert_target.add_argument("--all", action="store_true")
    revert.add_argument("--dry-run", action="store_true")

    info = commands.add_parser("info")
    info.add_argument("--output-format", choices=("text", "json"), default="text")

    for parser in (run, revert, skip):
        # a flag beats the config, in both directions
        parser.add_argument("--ignore-missing", action=BooleanOptionalAction)
    for parser in (run, revert, skip, info):
        parser.add_argument("--database-url", metavar="URL")
        parser.add_argument("--config", type=Path, metavar="FILE")
        parser.add_argument("--source", type=Path, metavar="DIR")
        add_connect_timeout(parser)


def connect_timeout_type(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        msg = f"invalid seconds: {value!r}"
        raise ArgumentTypeError(msg) from None
    if not 0 <= seconds < float("inf"):
        msg = "must be zero or more seconds"
        raise ArgumentTypeError(msg)
    return seconds


def add_connect_timeout(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--connect-timeout",
        type=connect_timeout_type,
        default=DEFAULT_CONNECT_TIMEOUT,
        metavar="SECONDS",
        help="retry connection failures and a starting-up server for this long (default 10; 0 tries once)",
    )


# retries `probe` while the server is unreachable or starting up (57P03), until `seconds` seconds have passed
async def wait_for_server(probe: Callable[[], Awaitable[object]], seconds: float) -> None:
    deadline = monotonic() + seconds
    delay = RETRY_FIRST
    while True:
        try:
            await probe()
        except InterfaceError:
            pass
        except DatabaseError as error:
            if error.sqlstate != STARTING_UP:
                raise
        else:
            return
        if (remaining := deadline - monotonic()) <= 0:
            # one last attempt raises the real error
            await probe()
            return
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, RETRY_MAX)


async def probe_connection(dsn: str) -> None:
    async with await connect(dsn):
        pass


def resolve_target(config: Config, source: Path | None) -> Target:
    migrate = config.migrate or MigrateConfig()
    # `--source` is relative to the current directory, the configured `dir` to pyproject.toml
    directory = source if source is not None else config.root / migrate.dir
    return Target(directory=directory, migrate=migrate)


def require_url(config: Config, url: str | None) -> str:
    dsn = database_url(config, url)
    if not dsn.strip():
        msg = f"no database URL; pass --database-url or set ${config.database_url_env}"
        raise CheckError(msg)
    return dsn


# runs `work` and maps every database-side failure to a redacted `CheckError`
def run_guarded(dsn: str, work: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(work)
    except CheckError as error:
        raise CheckError(redact(str(error), dsn)) from None
    except MigrateError as error:
        raise CheckError(redact(_describe(error), dsn)) from None
    except ValueError as error:
        # an unknown or too new target
        raise CheckError(redact(str(error), dsn)) from None
    except (InterfaceError, DatabaseError, OSError) as error:
        msg = f"could not reach the database: {redact(str(error), dsn)}"
        raise CheckError(msg) from None


def handle(args: Namespace) -> None:
    config = load_config(Path.cwd(), args.config)
    target = resolve_target(config, args.source)
    if args.migrate_command == "add":
        add(target, args)
        if config.migrate is None:
            sys.stderr.write(TIP)
        return
    dsn = require_url(config, args.database_url)
    run_guarded(dsn, _database_command(target, args, dsn))


# `migrate run` with no flags, split so a caller can validate the files before any database IO
def load_migrator(target: Target) -> Migrator:
    return _load(target, Namespace())


async def run_pending(migrator: Migrator, dsn: str) -> None:
    async with await connect(dsn) as conn:
        await _run(migrator, conn, Namespace(target_version=None, dry_run=False))


def _describe(error: MigrateError) -> str:
    cause = error.__cause__
    return f"{error}: {cause}" if cause is not None else str(error)


def _load(target: Target, args: Namespace) -> Migrator:
    migrate = target.migrate
    try:
        return Migrator(
            target.directory,
            table=migrate.table,
            create_schemas=migrate.create_schemas,
            # a flag beats the config
            allow_out_of_order=_policy(args, "allow_out_of_order", default=migrate.allow_out_of_order),
            ignore_missing=_policy(args, "ignore_missing", default=migrate.ignore_missing),
        )
    except MigrateError as error:
        raise CheckError(str(error)) from None
    except ValueError as error:
        raise CheckError(str(error)) from None


def _policy(args: Namespace, name: str, *, default: bool) -> bool:
    value = getattr(args, name, None)
    return default if value is None else bool(value)


def _forward(migrations: Iterable[Migration]) -> list[Migration]:
    return sorted(migrations, key=lambda m: m.version)


def _is_sequential(versions: list[int]) -> bool:
    # sqlx's rule: the last two versions differ by 1, or the only version is 0 or 1
    match versions:
        case [*_, previous, last]:
            return last - previous == 1
        case [only]:
            return only in {0, 1}
        case _:
            return False


def add(target: Target, args: Namespace) -> None:
    description = args.description.strip()
    if not description or any(char in description for char in ("/", "\\", "\0")):
        msg = "the description must be non-empty and must not contain a path separator or a NUL byte"
        raise CheckError(msg)
    existing = _forward(_load(target, args).migrations) if target.directory.is_dir() else []
    versions = [m.version for m in existing]

    versioning = "timestamp" if args.timestamp else "sequential" if args.sequential else target.migrate.versioning
    if versioning == "inferred":
        versioning = "sequential" if _is_sequential(versions) else "timestamp"
    kind = "reversible" if args.reversible else "simple" if args.simple else target.migrate.kind
    if kind == "inferred":
        kind = "reversible" if existing and existing[-1].kind == "reversible" else "simple"

    version = (
        int(utc_now().strftime(TIMESTAMP_FORMAT))
        if versioning == "timestamp"
        else (versions[-1] + 1 if versions else 1)
    )
    if version in versions:
        msg = f"refusing to overwrite: migration version {version} already exists in {target.directory}"
        raise CheckError(msg)

    # the file name must read back as the kind it was written as: `x.up` + `.sql` is half of a pair
    if kind == "simple" and description.endswith(RESERVED_SUFFIXES):
        msg = f"a simple migration's description must not end in {' or '.join(RESERVED_SUFFIXES)}"
        raise CheckError(msg)
    stem = f"{version:0{SEQUENTIAL_WIDTH}d}" if versioning == "sequential" else str(version)
    name = f"{stem}_{description.replace(' ', '_')}"
    files = (
        [(f"{name}.up.sql", "up"), (f"{name}.down.sql", "down")]
        if kind == "reversible"
        else [(f"{name}.sql", "simple")]
    )
    paths = [target.directory / filename for filename, _ in files]
    if existing_path := next((path for path in paths if path.exists()), None):
        msg = f"refusing to overwrite {existing_path}"
        raise CheckError(msg)
    try:
        target.directory.mkdir(parents=True, exist_ok=True)
        for path, (_, file_kind) in zip(paths, files, strict=True):
            _create(path, PLACEHOLDERS[file_kind])
    except OSError as error:
        msg = f"cannot write the migration: {error}"
        raise CheckError(msg) from None


def _create(path: Path, content: str) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except FileExistsError:
        msg = f"refusing to overwrite {path}"
        raise CheckError(msg) from None
    sys.stdout.write(f"Created {_shown(path)}\n")


def _shown(path: Path) -> Path:
    try:
        return path.relative_to(Path.cwd().resolve())
    except ValueError:
        return path


def format_duration(value: timedelta | None) -> str:
    if value is None:
        return "n/a"
    micros = value / timedelta(microseconds=1)
    if micros >= MICROS_PER_SECOND:
        return f"{micros / MICROS_PER_SECOND:.1f}s"
    if micros >= MICROS_PER_MILLI:
        return f"{micros / MICROS_PER_MILLI:.1f}ms"
    return f"{micros:.1f}us"


def _line(
    verb: str, result: MigrationResult, label: str, elapsed: timedelta | None = None, *, timed: bool = True
) -> str:
    suffix = f" ({format_duration(elapsed)})" if timed else ""
    return f"{verb} {result.migration.version}/{label} {result.migration.description}{suffix}\n"


async def _database_command(target: Target, args: Namespace, dsn: str) -> None:
    migrator = _load(target, args)
    await wait_for_server(partial(probe_connection, dsn), args.connect_timeout)
    async with await connect(dsn) as conn:
        match args.migrate_command:
            case "run":
                await _run(migrator, conn, args)
            case "revert":
                await _revert(migrator, conn, args)
            case "skip":
                await _skip(migrator, conn, args)
            case _:
                await _info(migrator, conn, args.output_format)


async def _run(migrator: Migrator, conn: Connection, args: Namespace) -> None:
    for result in await migrator.run(conn, target=args.target_version, dry_run=args.dry_run):
        if args.dry_run:
            sys.stdout.write(_line("Would apply", result, "migrate", timed=False))
        else:
            sys.stdout.write(_line("Applied", result, "migrate", result.elapsed))


async def _revert(migrator: Migrator, conn: Connection, args: Namespace) -> None:
    # one revert, so the versions are chosen and reverted under one lock; without a target or `--all`, only the
    # latest applied migration is reverted, and `--target-version V` reverts every version above V
    for result in await migrator.revert(conn, target=args.target_version, all=args.all, dry_run=args.dry_run):
        if args.dry_run:
            sys.stdout.write(_line("Would revert", result, "revert", timed=False))
        else:
            sys.stdout.write(_line("Reverted", result, "revert", result.elapsed))


async def _skip(migrator: Migrator, conn: Connection, args: Namespace) -> None:
    for result in await migrator.skip(conn, target=args.target_version, dry_run=args.dry_run):
        verb = "Would skip" if args.dry_run else "Skipped"
        sys.stdout.write(_line(verb, result, "skip", timed=False))


def _status_json(status: MigrationStatus) -> dict[str, object]:
    return {
        "version": status.version,
        "description": status.description,
        "state": status.state,
        "kind": status.kind,
        "installed_on": status.installed_on.isoformat() if status.installed_on is not None else None,
        # nanoseconds, from the microsecond-resolution timedelta
        "execution_time": (
            status.execution_time // timedelta(microseconds=1) * NANOS_PER_MICRO
            if status.execution_time is not None
            else None
        ),
        "local_checksum": status.local_checksum.hex() if status.local_checksum is not None else None,
        "applied_checksum": status.applied_checksum.hex() if status.applied_checksum is not None else None,
    }


async def _info(migrator: Migrator, conn: Connection, output_format: str) -> None:
    statuses = await migrator.info(conn)
    if output_format == "json":
        sys.stdout.write(json.dumps([_status_json(status) for status in statuses], indent=2) + "\n")
        return
    for status in statuses:
        line = f"{status.version}/{status.state}"
        if status.description:
            line += f" {status.description}"
        if status.state == "modified" and status.local_checksum is not None and status.applied_checksum is not None:
            line += f" (local {status.local_checksum.hex()[:SHORT_HEX]}, applied {status.applied_checksum.hex()[:SHORT_HEX]})"
        elif status.execution_time is not None:
            line += f" ({format_duration(status.execution_time)})"
        sys.stdout.write(line + "\n")
