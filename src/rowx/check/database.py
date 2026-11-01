import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rowx.check.config import Config, load_config
from rowx.check.migrate import require_url, resolve_target, run_guarded, run_pending
from rowx.check.runner import CheckError
from rowx.postgres import ConnectOptions
from rowx.postgres._admin import create_database, database_exists, drop_database

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


def add_parsers(subparsers: _SubParsersAction[ArgumentParser]) -> None:
    database = subparsers.add_parser("database")
    commands = database.add_subparsers(dest="database_command", required=True)

    create = commands.add_parser("create")
    drop = commands.add_parser("drop")
    reset = commands.add_parser("reset")
    setup = commands.add_parser("setup")

    for parser in (drop, reset):
        parser.add_argument("--force", action="store_true")
        parser.add_argument("-y", dest="yes", action="store_true")
    for parser in (create, drop, reset, setup):
        parser.add_argument("--database-url", metavar="URL")
        parser.add_argument("--config", type=Path, metavar="FILE")
    for parser in (reset, setup):
        parser.add_argument("--source", type=Path, metavar="DIR")


def _name(dsn: str) -> str:
    try:
        name = ConnectOptions.from_dsn(dsn).database
    except Exception as error:
        # the parser's message can quote the DSN, so it is not repeated
        msg = "the database URL could not be parsed"
        raise CheckError(msg) from error
    if name is None or not name.strip():
        msg = "the database URL names no database"
        raise CheckError(msg)
    return name


def _confirmed(verb: str, name: str, *, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        msg = f"refusing to {verb} database {name!r} without a terminal to confirm on; pass -y"
        raise CheckError(msg)
    sys.stdout.write(f"{verb.capitalize()} database {name!r}? [y/N] ")
    sys.stdout.flush()
    if sys.stdin.readline().strip().lower() in {"y", "yes"}:
        return True
    sys.stdout.write("Aborted\n")
    return False


async def _create(dsn: str, name: str) -> None:
    if await database_exists(dsn, name):
        sys.stdout.write(f"Database {name!r} already exists\n")
        return
    await create_database(dsn, name)
    sys.stdout.write(f"Created database {name!r}\n")


async def _drop(dsn: str, name: str, *, force: bool) -> None:
    await drop_database(dsn, name, force=force)
    sys.stdout.write(f"Dropped database {name!r}\n")


def handle(args: Namespace) -> None:
    config = load_config(Path.cwd(), args.config)
    dsn = require_url(config, args.database_url)
    name = _name(dsn)
    match args.database_command:
        case "create":
            run_guarded(dsn, _create(dsn, name))
        case "drop":
            if _confirmed("drop", name, yes=args.yes):
                run_guarded(dsn, _drop(dsn, name, force=args.force))
        case "reset":
            if _confirmed("reset", name, yes=args.yes):
                run_guarded(dsn, _reset(config, args, dsn, name))
        case _:
            run_guarded(dsn, _setup(config, args, dsn, name))


async def _reset(config: Config, args: Namespace, dsn: str, name: str) -> None:
    await _drop(dsn, name, force=args.force)
    await _create(dsn, name)
    await run_pending(resolve_target(config, args.source), dsn)


async def _setup(config: Config, args: Namespace, dsn: str, name: str) -> None:
    await _create(dsn, name)
    await run_pending(resolve_target(config, args.source), dsn)
