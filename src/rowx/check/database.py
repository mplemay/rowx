import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from rowx_core import InterfaceError

from rowx._backend import backend_for
from rowx.check.config import load_config
from rowx.check.embedded import embedded_for
from rowx.check.migrate import add_connect_timeout, load_migrator, require_url, resolve_target, run_guarded, run_pending
from rowx.check.runner import CheckError

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction
    from collections.abc import Awaitable, Callable

    from rowx_core.backend import AdminService, MigrationService


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
        add_connect_timeout(parser)
    for parser in (reset, setup):
        parser.add_argument("--source", type=Path, metavar="DIR")


def _name(admin: AdminService) -> str:
    name = admin.database
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


async def _create(admin: AdminService, name: str) -> None:
    if await admin.exists(name):
        sys.stdout.write(f"Database {name!r} already exists\n")
        return
    await admin.create(name)
    sys.stdout.write(f"Created database {name!r}\n")


async def _drop(admin: AdminService, name: str, *, force: bool) -> None:
    await admin.drop(name, force=force)
    sys.stdout.write(f"Dropped database {name!r}\n")


async def _ready(admin: AdminService, args: Namespace, work: Callable[[], Awaitable[None]]) -> None:
    await admin.ready(args.connect_timeout)
    await work()


def handle(args: Namespace) -> None:
    config = load_config(Path.cwd(), args.config)
    dsn = require_url(config, args.database_url)
    if embedded_for(dsn) is not None:
        msg = "`rowx database` is not supported for embedded URLs"
        raise CheckError(msg)
    backend = backend_for(dsn)
    try:
        admin = backend.admin(dsn)
    except InterfaceError, ValueError:
        msg = "the database URL could not be parsed"
        raise CheckError(msg) from None
    name = _name(admin)
    match args.database_command:
        case "create":
            run_guarded(dsn, _ready(admin, args, partial(_create, admin, name)))
        case "drop":
            if _confirmed("drop", name, yes=args.yes):
                run_guarded(dsn, _ready(admin, args, partial(_drop, admin, name, force=args.force)))
        case "reset":
            migrator = load_migrator(resolve_target(config, args.source), dsn, backend)
            if _confirmed("reset", name, yes=args.yes):
                run_guarded(dsn, _ready(admin, args, partial(_reset, migrator, admin, dsn, name, force=args.force)))
        case _:
            migrator = load_migrator(resolve_target(config, args.source), dsn, backend)
            run_guarded(dsn, _ready(admin, args, partial(_setup, migrator, admin, dsn, name)))


async def _reset(migrator: MigrationService, admin: AdminService, dsn: str, name: str, *, force: bool) -> None:
    await _drop(admin, name, force=force)
    await _create(admin, name)
    await run_pending(migrator, dsn)


async def _setup(migrator: MigrationService, admin: AdminService, dsn: str, name: str) -> None:
    await _create(admin, name)
    await run_pending(migrator, dsn)
