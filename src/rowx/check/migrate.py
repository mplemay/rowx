import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from rowx import query_unchecked
from rowx.check.config import MigrateConfig, database_url, load_config
from rowx.check.runner import CheckError, redact
from rowx.errors import (
    DatabaseError,
    DirtyMigrationError,
    InterfaceError,
    MigrateError,
    MigrationChecksumError,
    MigrationMissingError,
    MigrationOrderError,
    MigrationSourceError,
)
from rowx.postgres import Migrator, connect

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction
    from collections.abc import Iterable

    from rowx.postgres import Connection, Migration, MigrationStatus

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
LABELS: Final[dict[str, str]] = {"simple": "migrate", "up": "migrate", "down": "revert"}


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
    run.add_argument("--target-version", type=int, metavar="V")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--allow-out-of-order", action="store_true")
    run.add_argument("--ignore-missing", action="store_true")

    revert = commands.add_parser("revert")
    revert.add_argument("--target-version", type=int, metavar="V")
    revert.add_argument("--dry-run", action="store_true")

    info = commands.add_parser("info")

    for parser in (run, revert, info):
        parser.add_argument("--database-url", metavar="URL")
        parser.add_argument("--config", type=Path, metavar="FILE")
        parser.add_argument("--source", type=Path, metavar="DIR")


def handle(args: Namespace) -> None:
    config = load_config(Path.cwd(), args.config)
    migrate = config.migrate or MigrateConfig()
    # `--source` is relative to the current directory, the configured `dir` to pyproject.toml
    directory = args.source if args.source is not None else config.root / migrate.dir
    target = Target(directory=directory, migrate=migrate)
    if args.migrate_command == "add":
        add(target, args)
        return
    dsn = database_url(config, args.database_url)
    if not dsn.strip():
        msg = f"no database URL; pass --database-url or set ${config.database_url_env}"
        raise CheckError(msg)
    try:
        asyncio.run(_database_command(target, args, dsn))
    except CheckError as error:
        raise CheckError(redact(str(error), dsn)) from None
    except MigrateError as error:
        raise CheckError(redact(_describe(error), dsn)) from None
    except (InterfaceError, DatabaseError, OSError) as error:
        msg = f"could not reach the database: {redact(str(error), dsn)}"
        raise CheckError(msg) from None


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
            allow_out_of_order=getattr(args, "allow_out_of_order", False),
            ignore_missing=getattr(args, "ignore_missing", False),
        )
    except MigrateError as error:
        raise CheckError(str(error)) from None
    except ValueError as error:
        raise CheckError(str(error)) from None


def _forward(migrations: Iterable[Migration]) -> list[Migration]:
    return sorted((m for m in migrations if m.kind != "down"), key=lambda m: m.version)


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
    if not description or "/" in description or "\\" in description:
        msg = "the description must be non-empty and must not contain a path separator"
        raise CheckError(msg)
    existing = _forward(_load(target, args).migrations) if target.directory.is_dir() else []
    versions = [m.version for m in existing]

    versioning = "timestamp" if args.timestamp else "sequential" if args.sequential else target.migrate.versioning
    if versioning == "inferred":
        versioning = "sequential" if _is_sequential(versions) else "timestamp"
    kind = "reversible" if args.reversible else "simple" if args.simple else target.migrate.kind
    if kind == "inferred":
        kind = "reversible" if existing and existing[-1].kind == "up" else "simple"

    version = (
        int(utc_now().strftime(TIMESTAMP_FORMAT))
        if versioning == "timestamp"
        else (versions[-1] + 1 if versions else 1)
    )
    if version in versions:
        msg = f"refusing to overwrite: migration version {version} already exists in {target.directory}"
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
    target.directory.mkdir(parents=True, exist_ok=True)
    for path, (_, file_kind) in zip(paths, files, strict=True):
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(PLACEHOLDERS[file_kind])
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
    verb: str, status: MigrationStatus, label: str, elapsed: timedelta | None = None, *, timed: bool = True
) -> str:
    suffix = f" ({format_duration(elapsed)})" if timed else ""
    return f"{verb} {status.version}/{label} {status.description}{suffix}\n"


async def _database_command(target: Target, args: Namespace, dsn: str) -> None:
    migrator = _load(target, args)
    async with await connect(dsn) as conn:
        match args.migrate_command:
            case "run":
                await _run(migrator, conn, args)
            case "revert":
                await _revert(migrator, conn, args)
            case _:
                await _info(migrator, conn, target)


def _description_of(migrator: Migrator, version: int) -> str:
    return next((m.description for m in migrator.migrations if m.version == version), "")


def _plan_validate(statuses: tuple[MigrationStatus, ...], *, ignore_missing: bool) -> None:
    # the same order as the migrator: dirty, missing, then modified
    problems = (
        (
            "dirty",
            True,
            DirtyMigrationError,
            "migration {} is dirty: it did not finish; fix the database and its history row",
        ),
        (
            "missing",
            not ignore_missing,
            MigrationMissingError,
            "migration {} is applied but missing from the migration directory",
        ),
        ("modified", True, MigrationChecksumError, "migration {} was modified after it was applied"),
    )
    for state, enabled, error, message in problems:
        if enabled and (found := next((s for s in statuses if s.state == state), None)):
            raise error(message.format(found.version), found.version)


def _plan_run(
    migrator: Migrator,
    statuses: tuple[MigrationStatus, ...],
    args: Namespace,
) -> list[MigrationStatus]:
    _plan_validate(statuses, ignore_missing=args.ignore_missing)
    local = {m.version for m in migrator.migrations if m.kind != "down"}
    latest = max((s.version for s in statuses if s.state != "pending"), default=None)
    if (version := args.target_version) is not None:
        if version not in local:
            msg = f"target migration {version} does not exist"
            raise MigrationOrderError(msg, version)
        if latest is not None and version < latest:
            msg = f"migration {version} is older than the latest applied migration {latest}"
            raise MigrationOrderError(msg, version)
    steps = [
        s
        for s in sorted(statuses, key=lambda s: s.version)
        if s.state == "pending" and (args.target_version is None or s.version <= args.target_version)
    ]
    if (
        not args.allow_out_of_order
        and latest is not None
        and (stale := next((s for s in steps if s.version < latest), None))
    ):
        msg = f"migration {stale.version} is older than the latest applied migration {latest}"
        raise MigrationOrderError(msg, stale.version)
    return steps


async def _run(migrator: Migrator, conn: Connection, args: Namespace) -> None:
    if args.dry_run:
        statuses = await migrator.info(conn)
        for status in _plan_run(migrator, statuses, args):
            sys.stdout.write(_line("Would apply", status, "migrate", timed=False))
        return
    for status in await migrator.run(conn, target=args.target_version):
        sys.stdout.write(_line("Applied", status, "migrate", status.execution_time))


async def _revert(migrator: Migrator, conn: Connection, args: Namespace) -> None:
    statuses = await migrator.info(conn)
    _plan_validate(statuses, ignore_missing=False)
    applied = sorted((s for s in statuses if s.state != "pending"), key=lambda s: s.version, reverse=True)
    # without a target, only the latest migration is reverted
    floor = args.target_version if args.target_version is not None else (applied[0].version - 1 if applied else 0)
    steps = [s for s in applied if s.version > floor]
    downs = {m.version for m in migrator.migrations if m.kind == "down"}
    if missing := next((s for s in steps if s.version not in downs), None):
        msg = f"migration {missing.version} has no .down.sql file to revert it"
        raise MigrationSourceError(msg, missing.version)
    for status in steps:
        if args.dry_run:
            sys.stdout.write(_line("Would revert", status, "revert", timed=False))
            continue
        started = asyncio.get_running_loop().time()
        # one version at a time, so each line has its own elapsed time
        await migrator.undo(conn, target=status.version - 1)
        elapsed = timedelta(seconds=asyncio.get_running_loop().time() - started)
        sys.stdout.write(_line("Reverted", status, "revert", elapsed))


async def _applied_checksums(conn: Connection, table: str) -> dict[int, bytes]:
    schema, dot, name = table.rpartition(".")
    ident = (schema, name) if dot else name
    rows = await query_unchecked[tuple[int, bytes]](t"SELECT version, checksum FROM {ident:ident}").all(conn)
    return dict(rows)


async def _info(migrator: Migrator, conn: Connection, target: Target) -> None:
    statuses = await migrator.info(conn)
    local = {m.version: m.checksum for m in migrator.migrations if m.kind != "down"}
    applied = (
        await _applied_checksums(conn, target.migrate.table) if any(s.state == "modified" for s in statuses) else {}
    )
    for status in statuses:
        line = f"{status.version}/{status.state}"
        if status.description:
            line += f" {status.description}"
        if status.state == "modified":
            line += f" (local {local[status.version].hex()[:SHORT_HEX]}, applied {applied[status.version].hex()[:SHORT_HEX]})"
        elif status.execution_time is not None:
            line += f" ({format_duration(status.execution_time)})"
        sys.stdout.write(line + "\n")
