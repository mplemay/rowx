import asyncio
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, Final, NoReturn

import pytest

from rowx import query_unchecked
from rowx.check.config import DEFAULT_MIGRATE_TABLE, ConfigError, load_config
from rowx.check.migrate import CheckError, load_migrator, resolve_target
from rowx.check.runner import redact
from rowx.check.scratch import ORPHAN_AGE_SECONDS, make_tag, tag_age
from rowx.errors import DatabaseError, Error, InterfaceError, MigrateError
from rowx.postgres import Connection, Migrator, Pool, connect
from rowx.postgres._admin import create_database, drop_database, list_databases, with_database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Coroutine, Iterable, Iterator

    from rowx.check.config import Config

TEMPLATE_PREFIX: Final[str] = "rowx_tpl_"
TEST_PREFIX: Final[str] = "rowx_test_"
TMP_MARKER: Final[str] = "_tmp_"
PROJECT_LENGTH: Final[int] = 8
HASH_LENGTH: Final[int] = 16
# the base of every database; `template0` keeps the template free of whatever `template1` holds
BASE_TEMPLATE: Final[str] = "template0"
# advisory locks are per database, so every session takes its locks in this one, whatever database its URL names: two
# sessions of one project whose URLs differ must still build a template once and see each other's claims
LOCK_DATABASE: Final[str] = "postgres"
# `postgres` is missing (3D000) or the role may not connect to it (42501): the URL's own database stands in, never
# `template1`, as a connection held there all session would make every `CREATE DATABASE` from it fail
LOCK_FALLBACK_STATES: Final[frozenset[str]] = frozenset({"3D000", "42501"})
# the first key of the shared lock a session holds on its template, so stale cleanup can tell it is in use; the bytes
# spell "rowx"
LOCK_KEY: Final[int] = 0x726F7778
DATABASE_URL_ENV_INI: Final[str] = "rowx_database_url_env"
MIGRATIONS_INI: Final[str] = "rowx_migrations"
REQUIRE_DATABASE_INI: Final[str] = "rowx_require_database"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        DATABASE_URL_ENV_INI,
        "name of the environment variable that holds the server URL for the rowx_* fixtures; "
        "else the [tool.rowx] database_url_env",
    )
    parser.addini(MIGRATIONS_INI, "migrations directory relative to the rootdir; else [tool.rowx.migrate] dir")
    parser.addini(
        REQUIRE_DATABASE_INI, "fail instead of skipping when there is no database URL", type="bool", default=False
    )
    parser.addoption(
        "--rowx-keep-templates",
        action="store_true",
        default=False,
        help="keep this project's stale rowx_tpl_* template databases instead of dropping them when a new one is built",
    )


def rowx_version() -> str:
    try:
        return version("rowx")
    except PackageNotFoundError:
        return "unknown"


# the first 8 hex characters of SHA-256 over the resolved migrations path: what makes cleanup project-scoped
def project_prefix(directory: Path) -> str:
    return sha256(str(directory.resolve()).encode()).hexdigest()[:PROJECT_LENGTH]


def template_name(
    migrator: Migrator | None,
    *,
    directory: Path,
    table: str = DEFAULT_MIGRATE_TABLE,
    create_schemas: Iterable[str] = (),
    rowx: str | None = None,
) -> str:
    # everything that shapes the template: the rowx version, the path, the history table, the schemas created first and
    # the forward migrations (a `.down.sql` never changes what the template holds)
    pairs = sorted((m.version, m.checksum) for m in migrator.migrations) if migrator is not None else []
    lines = [
        f"rowx:{rowx if rowx is not None else rowx_version()}",
        f"path:{directory.resolve()}",
        f"table:{table}",
        *(f"schema:{schema}" for schema in sorted(set(create_schemas))),
    ]
    lines += [f"{number}:{checksum.hex()}" for number, checksum in pairs]
    digest = sha256("".join(f"{line}\n" for line in lines).encode()).hexdigest()
    return f"{TEMPLATE_PREFIX}{project_prefix(directory)}_{digest[:HASH_LENGTH]}"


# the `<p>` of a template name
def project_of(name: str) -> str:
    return name.removeprefix(TEMPLATE_PREFIX)[:PROJECT_LENGTH]


# the second key of the shared lock a session holds on its template
def template_key(name: str) -> int:
    return int.from_bytes(sha256(name.encode()).digest()[:4], signed=True)


# the build lock, keyed by the template's `<h>`
def build_key(name: str) -> int:
    return int.from_bytes(sha256(f"build:{name}".encode()).digest()[:8], signed=True)


# a connection to the lock database, and the DSN it was opened with, so the session's other lock connections match it
async def connect_lock(server_dsn: str) -> tuple[Connection, str]:
    lock_dsn = with_database(server_dsn, LOCK_DATABASE)
    try:
        return await connect(lock_dsn), lock_dsn
    except DatabaseError as error:
        if error.sqlstate not in LOCK_FALLBACK_STATES:
            raise
    return await connect(server_dsn), server_dsn


@asynccontextmanager
async def locked(lock_dsn: str, key: int) -> AsyncGenerator[Connection]:
    # a session lock on its own connection, released by closing it, so a crashed process never leaves it behind
    async with await connect(lock_dsn) as conn:
        await query_unchecked[tuple[()]](t"SELECT pg_catalog.pg_advisory_lock({key}::pg_catalog.int8)").execute(conn)
        yield conn


async def exists(conn: Connection, name: str) -> bool:
    (found,) = await query_unchecked[tuple[bool]](
        t"SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname OPERATOR(pg_catalog.=) {name})"
    ).one(conn)
    return found


async def stale_templates(conn: Connection, project: str, keep: str) -> list[str]:
    prefix = f"{TEMPLATE_PREFIX}{project}_"
    rows = await query_unchecked[tuple[str]](
        t"SELECT datname FROM pg_catalog.pg_database WHERE pg_catalog.starts_with(datname, {prefix}) "
        t"AND datname OPERATOR(pg_catalog.<>) {keep} "
        t"AND position({TMP_MARKER} IN datname) OPERATOR(pg_catalog.=) 0 ORDER BY datname"
    ).all(conn)
    return [name for (name,) in rows]


# a database left by a session that is gone (or killed): tagged over an hour ago, with no sessions. Best effort, as any
# error (a listing that is refused, a database another role owns) is ignored
async def sweep_orphans(server_dsn: str, project: str) -> None:
    now = time()
    with suppress(Error, OSError):
        found = [
            *await list_databases(server_dsn, f"{TEST_PREFIX}{project}_"),
            *(
                info
                for info in await list_databases(server_dsn, f"{TEMPLATE_PREFIX}{project}_")
                if TMP_MARKER in info.name
            ),
        ]
        for info in found:
            age = tag_age(info.comment, now)
            if age is None or age <= ORPHAN_AGE_SECONDS or info.sessions > 0:
                continue
            with suppress(Error, OSError):
                await drop_database(server_dsn, info.name)


async def build(server_dsn: str, lock: Connection, migrator: Migrator | None, name: str) -> None:
    tmp = f"{name}{TMP_MARKER}{secrets.token_hex(8)}"
    await create_database(server_dsn, tmp, template=BASE_TEMPLATE, comment=make_tag())
    try:
        if migrator is not None:
            # closed before the rename, so nothing is connected to the template when it is cloned
            async with await connect(with_database(server_dsn, tmp)) as conn:
                await migrator.run(conn)
        # utility statements take no parameters, so the names are spliced as identifiers
        await query_unchecked[tuple[()]](t"ALTER DATABASE {tmp:ident} RENAME TO {name:ident}").execute(lock)
    except BaseException:
        with suppress(Error, OSError):
            await drop_database(server_dsn, tmp, force=True)
        raise
    # the name now exists, so a failure from here on is cleaned up under it
    try:
        await query_unchecked[tuple[()]](t"COMMENT ON DATABASE {name:ident} IS NULL").execute(lock)
        await query_unchecked[tuple[()]](
            t"ALTER DATABASE {name:ident} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false"
        ).execute(lock)
    except BaseException:
        with suppress(Error, OSError):
            await drop_database(server_dsn, name, force=True)
        raise


async def drop_stale(server_dsn: str, lock: Connection, project: str, keep: str) -> None:
    for stale in await stale_templates(lock, project, keep):
        key = template_key(stale)
        # a template another live session holds (its shared lock) is still in use, even while no clone is running
        (free,) = await query_unchecked[tuple[bool]](
            t"SELECT pg_catalog.pg_try_advisory_lock({LOCK_KEY}::pg_catalog.int4, {key}::pg_catalog.int4)"
        ).one(lock)
        if not free:
            continue
        try:
            # best effort and never FORCE: a template of another role, or one in use, stays
            with suppress(Error, OSError):
                await query_unchecked[tuple[()]](t"ALTER DATABASE {stale:ident} WITH IS_TEMPLATE false").execute(lock)
                await drop_database(server_dsn, stale)
        finally:
            await query_unchecked[tuple[()]](
                t"SELECT pg_catalog.pg_advisory_unlock({LOCK_KEY}::pg_catalog.int4, {key}::pg_catalog.int4)"
            ).execute(lock)


# reuses the template or builds it, under the build lock (`lock`) taken before the lookup
async def prepare(server_dsn: str, lock: Connection, migrator: Migrator | None, name: str, *, keep_stale: bool) -> None:
    # a database under the final name is complete by construction: it is renamed there only once migrated
    if await exists(lock, name):
        return
    await build(server_dsn, lock, migrator, name)
    if not keep_stale:
        await drop_stale(server_dsn, lock, project_of(name), name)


# returns a connection that holds the session's claim on the template until it is closed. The claim is taken under the
# build lock, so no other session's cleanup can drop the template between the lookup and the claim
async def ensure_template(server_dsn: str, migrator: Migrator | None, name: str, *, keep_stale: bool) -> Connection:
    await sweep_orphans(server_dsn, project_of(name))
    hold, lock_dsn = await connect_lock(server_dsn)
    key = template_key(name)
    try:
        async with locked(lock_dsn, build_key(name)) as lock:
            await query_unchecked[tuple[()]](
                t"SELECT pg_catalog.pg_advisory_lock_shared({LOCK_KEY}::pg_catalog.int4, {key}::pg_catalog.int4)"
            ).execute(hold)
            await prepare(server_dsn, lock, migrator, name, keep_stale=keep_stale)
    except BaseException:
        await hold.close()
        raise
    return hold


async def clone(server_dsn: str, template: str, project: str) -> str:
    name = f"{TEST_PREFIX}{project}_{secrets.token_hex(8)}"
    await create_database(server_dsn, name, template=template, comment=make_tag())
    return name


def fail(message: str, server_dsn: str) -> NoReturn:
    pytest.fail(redact(message, server_dsn), pytrace=False)


@dataclass(slots=True, kw_only=True, frozen=True)
class Session:
    server_dsn: str
    template: str
    project: str


def configured(config: pytest.Config) -> tuple[Config, str]:
    try:
        rowx_config = load_config(Path(config.rootpath))
    except ConfigError as error:
        pytest.fail(f"rowx: {error}", pytrace=False)
    env = str(config.getini(DATABASE_URL_ENV_INI) or "").strip() or rowx_config.database_url_env
    dsn = os.environ.get(env, "").strip()
    if not dsn:
        message = f"no database URL; set the ${env} environment variable"
        if config.getini(REQUIRE_DATABASE_INI):
            pytest.fail(f"{REQUIRE_DATABASE_INI} is set but {message}", pytrace=False)
        pytest.skip(message)
    return rowx_config, dsn


def blocking[T](work: Coroutine[Any, Any, T]) -> T:
    # on a thread of its own: a fixture can be set up while this thread runs an event loop (`request.getfixturevalue`
    # from an async test), where `asyncio.run` raises, and the thread's current loop is left as it was
    def run() -> T:
        return asyncio.run(work)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="rowx-testing") as executor:
        return executor.submit(run).result()


@pytest.fixture(scope="session")
def rowx_template(request: pytest.FixtureRequest) -> Iterator[Session]:
    rowx_config, server_dsn = configured(request.config)
    rootdir = Path(request.config.rootpath)
    ini = str(request.config.getini(MIGRATIONS_INI) or "").strip()
    migrator: Migrator | None = None
    directory = rootdir
    table = DEFAULT_MIGRATE_TABLE
    schemas: tuple[str, ...] = ()
    # with no migrations configured the template is an empty database
    if rowx_config.migrate is not None or ini:
        target = resolve_target(rowx_config, rootdir / ini if ini else None)
        try:
            migrator = load_migrator(target)
        except CheckError as error:
            fail(f"rowx: {error}", server_dsn)
        directory = target.directory
        table = target.migrate.table
        schemas = target.migrate.create_schemas
    template = template_name(migrator, directory=directory, table=table, create_schemas=schemas)
    project = project_prefix(directory)
    keep = bool(request.config.getoption("--rowx-keep-templates"))
    try:
        hold = blocking(ensure_template(server_dsn, migrator, template, keep_stale=keep))
    except MigrateError as error:
        cause = f": {error.__cause__}" if error.__cause__ is not None else ""
        fail(f"rowx: the migrations failed on the template database: {error}{cause}", server_dsn)
    except (DatabaseError, InterfaceError, OSError, ValueError) as error:
        fail(f"rowx: could not build the template database: {error}", server_dsn)
    try:
        yield Session(server_dsn=server_dsn, template=template, project=project)
    finally:
        # releases the session's claim on the template, so a later session may drop it as stale
        blocking(hold.close())


@pytest.fixture
def rowx_database_url(rowx_template: Session) -> Iterator[str]:
    server_dsn = rowx_template.server_dsn
    try:
        name = blocking(clone(server_dsn, rowx_template.template, rowx_template.project))
    except (DatabaseError, InterfaceError, OSError, ValueError) as error:
        fail(f"rowx: could not clone the template database: {error}", server_dsn)
    try:
        yield with_database(server_dsn, name)
    finally:
        try:
            blocking(drop_database(server_dsn, name, force=True))
        except (DatabaseError, InterfaceError, OSError) as error:
            fail(f"rowx: could not drop the test database {name}: {error}", server_dsn)


@pytest.fixture
def rowx_connection(rowx_database_url: str) -> Iterator[Connection]:
    conn = blocking(connect(rowx_database_url))
    try:
        yield conn
    finally:
        blocking(conn.close())


@pytest.fixture
def rowx_pool(rowx_database_url: str) -> Iterator[Pool]:
    pool = Pool(rowx_database_url)
    blocking(pool.open())
    try:
        yield pool
    finally:
        blocking(pool.close())
