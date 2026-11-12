import asyncio
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, Protocol

import pytest
from rowx_core.errors import DatabaseError, InterfaceError, MigrateError

from rowx._backend import backend_for
from rowx.check.config import DEFAULT_MIGRATE_TABLE, ConfigError, load_config
from rowx.check.migrate import CheckError, load_migrator, resolve_target
from rowx.check.runner import redact

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable, Iterator, Sequence

    from rowx_core.backend import CacheDirectory, DatabaseConnection, DatabasePool, MigrationService, Provisioner

    from rowx.check.config import Config

TEMPLATE_PREFIX: Final[str] = "rowx_tpl_"
TEST_PREFIX: Final[str] = "rowx_test_"
TMP_MARKER: Final[str] = "_tmp_"
PROJECT_LENGTH: Final[int] = 8
HASH_LENGTH: Final[int] = 16
DATABASE_URL_ENV_INI: Final[str] = "rowx_database_url_env"
MIGRATIONS_INI: Final[str] = "rowx_migrations"
REQUIRE_DATABASE_INI: Final[str] = "rowx_require_database"
INTERRUPT_START_WAIT: Final[float] = 5.0


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


# pytest exits through `sys.exit`, which `atexit` cannot see: tell the embedded engine's exit guard (if the companion is
# loaded) the run's status, so that a hard exit after an abandoned instance keeps it
def pytest_sessionfinish(exitstatus: int) -> None:
    if (api := sys.modules.get("rowx_embedded._api")) is not None:
        api.set_exit_code(int(exitstatus))


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


def template_name(  # ruff: ignore[too-many-arguments]
    migrator: MigrationSource | None,
    *,
    directory: Path,
    table: str = DEFAULT_MIGRATE_TABLE,
    create_schemas: Iterable[str] = (),
    identity: Iterable[str] = (),
    rowx: str | None = None,
) -> str:
    # everything that shapes the template: the rowx version, the path, the history table, the schemas created first, what
    # the backend says its template depends on (an embedded engine and its snapshot format) and the forward migrations
    # (a `.down.sql` never changes what the template holds)
    pairs = sorted((m.version, m.checksum) for m in migrator.migrations) if migrator is not None else []
    lines = [
        f"rowx:{rowx if rowx is not None else rowx_version()}",
        f"path:{directory.resolve()}",
        f"table:{table}",
        *(f"schema:{schema}" for schema in sorted(set(create_schemas))),
        *(f"identity:{line}" for line in identity),
    ]
    lines += [f"{number}:{checksum.hex()}" for number, checksum in pairs]
    digest = sha256("".join(f"{line}\n" for line in lines).encode()).hexdigest()
    return f"{TEMPLATE_PREFIX}{project_prefix(directory)}_{digest[:HASH_LENGTH]}"


# the `<p>` of a template name
def project_of(name: str) -> str:
    return name.removeprefix(TEMPLATE_PREFIX)[:PROJECT_LENGTH]


def fail(message: str, server_dsn: str) -> NoReturn:
    pytest.fail(redact(message, server_dsn), pytrace=False)


@dataclass(slots=True, kw_only=True, frozen=True)
class Session:
    server_dsn: str
    template: str
    project: str
    provisioner: Provisioner


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


# a runner with its cache disabled has no `cache`, and the backend then keeps what it caches for this session only
def cache_directory(config: pytest.Config) -> CacheDirectory | None:
    if isinstance(cache := getattr(config, "cache", None), pytest.Cache):
        return cache.mkdir
    return None


def blocking[T](work: Coroutine[Any, Any, T]) -> T:
    started = threading.Event()
    handle: list[tuple[asyncio.AbstractEventLoop, asyncio.Task[T]]] = []

    # on a thread of its own: a fixture can be set up while this thread runs an event loop (`request.getfixturevalue`
    # from an async test), where `asyncio.run` raises, and the thread's current loop is left as it was
    def run() -> T:
        with asyncio.Runner() as runner:
            loop = runner.get_loop()
            task = loop.create_task(work)
            handle.append((loop, task))
            started.set()
            return runner.run(task)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="rowx-testing") as executor:
        future = executor.submit(run)
        try:
            return future.result()
        except KeyboardInterrupt:
            # an interrupt cancels the work, so its own cleanup runs (a snapshot directory is removed) before the
            # executor joins the thread; left alone, the work would finish into a result nobody collects
            if started.wait(timeout=INTERRUPT_START_WAIT) and not future.done():
                loop, task = handle[0]
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(task.cancel)
            raise


@pytest.fixture(scope="session")
def rowx_template(request: pytest.FixtureRequest) -> Iterator[Session]:
    rowx_config, server_dsn = configured(request.config)
    backend = backend_for(server_dsn)
    try:
        provisioner = backend.testing(server_dsn, cache=cache_directory(request.config))
    except (InterfaceError, ValueError) as error:
        fail(f"rowx: {error}", server_dsn)
    rootdir = Path(request.config.rootpath)
    ini = str(request.config.getini(MIGRATIONS_INI) or "").strip()
    migrator: MigrationService | None = None
    directory = rootdir
    table = DEFAULT_MIGRATE_TABLE
    schemas: tuple[str, ...] = ()
    # with no migrations configured the template is an empty database
    if rowx_config.migrate is not None or ini:
        target = resolve_target(rowx_config, rootdir / ini if ini else None)
        try:
            migrator = load_migrator(target, server_dsn, backend)
        except CheckError as error:
            fail(f"rowx: {error}", server_dsn)
        directory = target.directory
        table = target.migrate.table
        schemas = target.migrate.create_schemas
    template = template_name(
        migrator, directory=directory, table=table, create_schemas=schemas, identity=provisioner.identity
    )
    project = project_prefix(directory)
    try:
        hold = blocking(
            provisioner.ensure_template(
                migrator, template, keep_stale=bool(request.config.getoption("--rowx-keep-templates")), project=project
            )
        )
    except MigrateError as error:
        cause = f": {error.__cause__}" if error.__cause__ is not None else ""
        fail(f"rowx: the migrations failed on the template database: {error}{cause}", server_dsn)
    except (DatabaseError, InterfaceError, OSError, ValueError) as error:
        fail(f"rowx: could not build the template database: {error}", server_dsn)
    try:
        yield Session(server_dsn=server_dsn, template=template, project=project, provisioner=provisioner)
    finally:
        # releases the session's claim on the template, so a later session may drop it as stale
        blocking(hold.close())


@pytest.fixture
def rowx_database_url(rowx_template: Session) -> Iterator[str]:
    server_dsn = rowx_template.server_dsn
    try:
        name = blocking(rowx_template.provisioner.clone(rowx_template.template, rowx_template.project))
    except (DatabaseError, InterfaceError, OSError, ValueError) as error:
        fail(f"rowx: could not clone the template database: {error}", server_dsn)
    try:
        yield rowx_template.provisioner.with_database(name)
    finally:
        try:
            blocking(rowx_template.provisioner.drop(name))
        except (DatabaseError, InterfaceError, OSError) as error:
            fail(f"rowx: could not drop the test database {name}: {error}", server_dsn)


@pytest.fixture
def rowx_connection(rowx_database_url: str, rowx_template: Session) -> Iterator[DatabaseConnection]:
    conn = blocking(rowx_template.provisioner.connect(rowx_database_url))
    try:
        yield conn
    finally:
        blocking(conn.close())


@pytest.fixture
def rowx_pool(rowx_database_url: str, rowx_template: Session) -> Iterator[DatabasePool]:
    pool = rowx_template.provisioner.pool(rowx_database_url)
    blocking(pool.open())
    try:
        yield pool
    finally:
        blocking(pool.close())
