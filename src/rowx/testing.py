import asyncio
import os
import secrets
from contextlib import asynccontextmanager, suppress
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import pytest

from rowx import query_unchecked
from rowx.check.config import ConfigError, database_url, load_config
from rowx.check.migrate import CheckError, load_migrator, resolve_target
from rowx.check.runner import redact
from rowx.errors import DatabaseError, InterfaceError, MigrateError
from rowx.postgres import Connection, Migrator, Pool, connect
from rowx.postgres._admin import create_database, drop_database, with_database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from rowx.check.config import Config

TEMPLATE_PREFIX: Final[str] = "rowx_tpl_"
TEST_PREFIX: Final[str] = "rowx_test_"
HASH_LENGTH: Final[int] = 16
# the base of every database; `template0` keeps the template free of whatever `template1` holds
BASE_TEMPLATE: Final[str] = "template0"
# held while a template is checked or built and while one is cloned, because Postgres refuses to clone a database that
# another session is cloning or connected to; the bytes spell "rowx"
LOCK_KEY: Final[int] = 0x726F7778
COMPLETE_MARK: Final[str] = "rowx template complete"
REQUIRE_DB: Final[str] = "ROWX_REQUIRE_DB"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "rowx_database_url", "server URL for the rowx_* fixtures; else the [tool.rowx] database_url_env variable"
    )
    parser.addoption(
        "--rowx-keep-templates",
        action="store_true",
        default=False,
        help="keep stale rowx_tpl_* template databases instead of dropping them when a new template is built",
    )


def template_name(migrator: Migrator) -> str:
    # over the forward migrations only, since a `.down.sql` never changes what the template holds
    pairs = sorted((m.version, m.checksum) for m in migrator.migrations if m.kind != "down")
    digest = sha256("".join(f"{version}:{checksum.hex()}\n" for version, checksum in pairs).encode()).hexdigest()
    return f"{TEMPLATE_PREFIX}{digest[:HASH_LENGTH]}"


@asynccontextmanager
async def locked(server_dsn: str) -> AsyncGenerator[None]:
    # a session lock on its own connection, released by closing it, so a crashed process never leaves it behind
    async with await connect(server_dsn) as conn:
        await query_unchecked[tuple[()]](t"SELECT pg_advisory_lock({LOCK_KEY})").execute(conn)
        yield


async def is_complete(conn: Connection, name: str) -> bool:
    rows = await query_unchecked[tuple[str | None]](
        t"SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = {name}"
    ).all(conn)
    return any(comment == COMPLETE_MARK for (comment,) in rows)


async def stale_templates(conn: Connection, keep: str) -> list[str]:
    rows = await query_unchecked[tuple[str]](
        t"SELECT datname FROM pg_database WHERE datname LIKE 'rowx\\_tpl\\_%' AND datname <> {keep} ORDER BY datname"
    ).all(conn)
    return [name for (name,) in rows]


async def build(server_dsn: str, migrator: Migrator, name: str) -> None:
    await create_database(server_dsn, name, template=BASE_TEMPLATE)
    try:
        # closed before the mark is written, so nothing is connected to the template when it is cloned
        async with await connect(with_database(server_dsn, name)) as conn:
            await migrator.run(conn)
        async with await connect(server_dsn) as conn:
            # utility statements take no parameters, so the mark (COMPLETE_MARK) is spelled out
            await query_unchecked[tuple[()]](t"COMMENT ON DATABASE {name:ident} IS 'rowx template complete'").execute(
                conn
            )
    except BaseException:
        with suppress(DatabaseError, InterfaceError, OSError):
            await drop_database(server_dsn, name, force=True)
        raise


async def ensure_template(server_dsn: str, migrator: Migrator, *, keep_stale: bool) -> str:
    name = template_name(migrator)
    async with locked(server_dsn), await connect(server_dsn) as conn:
        (exists,) = await query_unchecked[tuple[bool]](
            t"SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = {name})"
        ).one(conn)
        # reused when complete; a build that died part-way is replaced
        if exists and await is_complete(conn, name):
            return name
        if exists:
            await drop_database(server_dsn, name, force=True)
        await build(server_dsn, migrator, name)
        if not keep_stale:
            for stale in await stale_templates(conn, name):
                # best effort: a template of another project may be busy
                with suppress(DatabaseError):
                    await drop_database(server_dsn, stale, force=True)
    return name


async def clone(server_dsn: str, template: str) -> str:
    name = f"{TEST_PREFIX}{secrets.token_hex(8)}"
    async with locked(server_dsn):
        await create_database(server_dsn, name, template=template)
    return name


def fail(message: str, server_dsn: str) -> NoReturn:
    pytest.fail(redact(message, server_dsn), pytrace=False)


def configured(config: pytest.Config) -> tuple[Config, str]:
    try:
        rowx_config = load_config(Path(config.rootpath))
    except ConfigError as error:
        pytest.fail(f"rowx: {error}", pytrace=False)
    value = str(config.getini("rowx_database_url") or "").strip()
    dsn = database_url(rowx_config, value or None).strip()
    if not dsn:
        message = f"no database URL; set the rowx_database_url ini option or ${rowx_config.database_url_env}"
        if os.environ.get(REQUIRE_DB) == "1":
            pytest.fail(f"{REQUIRE_DB}=1 but {message}", pytrace=False)
        pytest.skip(message)
    return rowx_config, dsn


@pytest.fixture(scope="session")
def rowx_template(request: pytest.FixtureRequest) -> tuple[str, str]:
    rowx_config, server_dsn = configured(request.config)
    try:
        migrator = load_migrator(resolve_target(rowx_config, None))
    except CheckError as error:
        fail(f"rowx: {error}", server_dsn)
    keep = bool(request.config.getoption("--rowx-keep-templates"))
    try:
        template = asyncio.run(ensure_template(server_dsn, migrator, keep_stale=keep))
    except MigrateError as error:
        cause = f": {error.__cause__}" if error.__cause__ is not None else ""
        fail(f"rowx: the migrations failed on the template database: {error}{cause}", server_dsn)
    except (DatabaseError, InterfaceError, OSError) as error:
        fail(f"rowx: could not build the template database: {error}", server_dsn)
    return server_dsn, template


@pytest.fixture
def rowx_database_url(rowx_template: tuple[str, str]) -> Iterator[str]:
    server_dsn, template = rowx_template
    try:
        name = asyncio.run(clone(server_dsn, template))
    except (DatabaseError, InterfaceError, OSError) as error:
        fail(f"rowx: could not clone the template database: {error}", server_dsn)
    try:
        yield with_database(server_dsn, name)
    finally:
        asyncio.run(drop_database(server_dsn, name, force=True))


@pytest.fixture
def rowx_connection(rowx_database_url: str) -> Iterator[Connection]:
    conn = asyncio.run(connect(rowx_database_url))
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


@pytest.fixture
def rowx_pool(rowx_database_url: str) -> Iterator[Pool]:
    pool = Pool(rowx_database_url)
    asyncio.run(pool.open())
    try:
        yield pool
    finally:
        asyncio.run(pool.close())
