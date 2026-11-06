import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, Protocol

import pytest

from rowx._backend import DatabaseConnection, DatabasePool, Migrator, TemplateMaterialize, backend_for
from rowx.check.config import DEFAULT_MIGRATE_TABLE, ConfigError, load_config
from rowx.check.migrate import CheckError, load_migrator, resolve_target
from rowx.check.runner import redact
from rowx.errors import DatabaseError, InterfaceError, MigrateError

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable, Iterator, Sequence

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


class MigrationIdentity(Protocol):
    @property
    def version(self) -> int: ...
    @property
    def checksum(self) -> bytes: ...


class MigrationSource(Protocol):
    @property
    def migrations(self) -> Sequence[MigrationIdentity]: ...


def template_name(
    migrator: MigrationSource | None,
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


# the xdist test wraps this name; it forwards to the backend call that runs the template SQL
async def build(
    server_dsn: str,
    lock: DatabaseConnection,
    migrator: Migrator | None,
    name: str,
    materialize: TemplateMaterialize,
    /,
) -> None:
    await materialize(server_dsn, lock, migrator, name)


async def ensure_template(
    server_dsn: str, migrator: Migrator | None, name: str, *, keep_stale: bool
) -> DatabaseConnection:
    return await backend_for(server_dsn).testing.ensure_template(
        server_dsn,
        migrator,
        name,
        keep_stale=keep_stale,
        lock_database=LOCK_DATABASE,
        lock_fallback_states=LOCK_FALLBACK_STATES,
        lock_key=LOCK_KEY,
        template_key=template_key,
        build_key=build_key(name),
        project=project_of(name),
        test_prefix=TEST_PREFIX,
        template_prefix=TEMPLATE_PREFIX,
        tmp_marker=TMP_MARKER,
        base_template=BASE_TEMPLATE,
        build=build,
    )


async def clone(server_dsn: str, template: str, project: str) -> str:
    return await backend_for(server_dsn).testing.clone(server_dsn, template, project, test_prefix=TEST_PREFIX)


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
            migrator = load_migrator(target, server_dsn)
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
        yield backend_for(server_dsn).admin.with_database(server_dsn, name)
    finally:
        try:
            blocking(backend_for(server_dsn).admin.drop_database(server_dsn, name, force=True))
        except (DatabaseError, InterfaceError, OSError) as error:
            fail(f"rowx: could not drop the test database {name}: {error}", server_dsn)


@pytest.fixture
def rowx_connection(rowx_database_url: str) -> Iterator[DatabaseConnection]:
    conn = blocking(backend_for(rowx_database_url).migrate.connect(rowx_database_url))
    try:
        yield conn
    finally:
        blocking(conn.close())


@pytest.fixture
def rowx_pool(rowx_database_url: str) -> Iterator[DatabasePool]:
    pool = backend_for(rowx_database_url).testing.pool(rowx_database_url)
    blocking(pool.open())
    try:
        yield pool
    finally:
        blocking(pool.close())
