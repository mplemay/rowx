import sys
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rowx._backend import AdminBackend, ConnectTarget, Migrator, backend_for, default_backend
from rowx.check.config import load_config
from rowx.check.migrate import (
    add_connect_timeout,
    connect_options,
    load_migrator,
    require_url,
    resolve_target,
    run_guarded,
    run_pending,
    wait_for_server,
)
from rowx.check.runner import CheckError

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction
    from collections.abc import Coroutine

# the readiness test wraps `database_exists`. A parsed target has no scheme, so the command records the admin
# backend it selected from the URL before the probe starts
_ADMIN: ContextVar[AdminBackend | None] = ContextVar("rowx_database_admin", default=None)


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


def _name(dsn: str) -> str:
    name = connect_options(dsn).database
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


def _admin(target: str | ConnectTarget) -> AdminBackend:
    if isinstance(target, str):
        return backend_for(target).admin
    selected = _ADMIN.get()
    if selected is not None:
        return selected
    return default_backend().admin


# the readiness test wraps this name
async def database_exists(target: str | ConnectTarget, name: str | None = None) -> bool:
    return await _admin(target).database_exists(target, name)


async def _create(dsn: str, name: str) -> None:
    if await database_exists(dsn, name):
        sys.stdout.write(f"Database {name!r} already exists\n")
        return
    await backend_for(dsn).admin.create_database(dsn, name)
    sys.stdout.write(f"Created database {name!r}\n")


async def _drop(dsn: str, name: str, *, force: bool) -> None:
    await backend_for(dsn).admin.drop_database(dsn, name, force=force)
    sys.stdout.write(f"Dropped database {name!r}\n")


# waits for the server (through the maintenance database), then runs `work`
async def _ready(dsn: str, name: str, args: Namespace, work: Coroutine[Any, Any, None]) -> None:
    try:
        token = _ADMIN.set(backend_for(dsn).admin)
    except BaseException:
        work.close()
        raise
    try:
        try:
            # a probe through the maintenance database: the work opens its own connections, and is never retried
            await wait_for_server(
                lambda options: database_exists(options, name), connect_options(dsn), args.connect_timeout
            )
        except BaseException:
            work.close()
            raise
        await work
    finally:
        _ADMIN.reset(token)


def handle(args: Namespace) -> None:
    config = load_config(Path.cwd(), args.config)
    dsn = require_url(config, args.database_url)
    name = _name(dsn)
    match args.database_command:
        case "create":
            run_guarded(dsn, _ready(dsn, name, args, _create(dsn, name)))
        case "drop":
            if _confirmed("drop", name, yes=args.yes):
                run_guarded(dsn, _ready(dsn, name, args, _drop(dsn, name, force=args.force)))
        case "reset":
            # the migrations are read first, so a broken directory fails before anything is dropped
            migrator = load_migrator(resolve_target(config, args.source), dsn)
            if _confirmed("reset", name, yes=args.yes):
                run_guarded(dsn, _ready(dsn, name, args, _reset(migrator, dsn, name, force=args.force)))
        case _:
            migrator = load_migrator(resolve_target(config, args.source), dsn)
            run_guarded(dsn, _ready(dsn, name, args, _setup(migrator, dsn, name)))


async def _reset(migrator: Migrator, dsn: str, name: str, *, force: bool) -> None:
    await _drop(dsn, name, force=force)
    await _create(dsn, name)
    await run_pending(migrator, dsn)


async def _setup(migrator: Migrator, dsn: str, name: str) -> None:
    await _create(dsn, name)
    await run_pending(migrator, dsn)
