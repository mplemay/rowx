import asyncio
from time import monotonic
from typing import TYPE_CHECKING, Final, Self, override

from rowx_core.backend import CheckerSession, Provisioner
from rowx_core.errors import DatabaseError, InterfaceError, MigrateError

from rowx_postgres._admin import (
    DatabaseInfo,
    create_database,
    database_exists,
    drop_database,
    list_databases,
    with_database,
)
from rowx_postgres._connection import Connection, connect
from rowx_postgres._core import Connection as ConnectionImpl, connect_checker, describe
from rowx_postgres._migrate import Migrator
from rowx_postgres._options import ConnectOptions
from rowx_postgres._pool import Pool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence
    from os import PathLike
    from types import TracebackType

    from rowx_core.backend import Describe, Failure, MigrationService
    from rowx_core.migrations import Migration, MigrationResult, MigrationStatus


RETRY_FIRST: Final[float] = 0.05
RETRY_MAX: Final[float] = 0.5
MIN_ATTEMPT: Final[float] = 0.001


async def wait_for_server[T](
    attempt: Callable[[ConnectOptions], Awaitable[T]], options: ConnectOptions, seconds: float
) -> T:
    deadline = monotonic() + seconds
    delay = RETRY_FIRST
    while True:
        bounded = options
        if seconds > 0:
            limit = max(deadline - monotonic(), MIN_ATTEMPT)
            if options.connect_timeout is not None and options.connect_timeout > 0:
                limit = min(limit, options.connect_timeout)
            bounded = options.with_connect_timeout(limit)
        try:
            return await attempt(bounded)
        except (InterfaceError, DatabaseError) as error:
            if isinstance(error, DatabaseError) and error.sqlstate != "57P03":
                raise
            if (remaining := deadline - monotonic()) <= 0:
                raise
            await asyncio.sleep(min(delay, remaining))
            if deadline - monotonic() <= 0:
                raise
        delay = min(delay * 2, RETRY_MAX)


class PostgresCheckerSession(CheckerSession):
    def __init__(self, connection: ConnectionImpl) -> None:
        self._connection = connection

    @override
    async def describe(self, sql: str) -> Describe:
        return await describe(self._connection, sql)

    @override
    async def close(self) -> None:
        await self._connection.close()

    @override
    def classify(self, error: Exception) -> Failure:
        if isinstance(error, InterfaceError):
            return "lost"
        if isinstance(error, DatabaseError):
            if error.sqlstate == "57014":
                return "timeout"
            if error.sqlstate == "57P01" or error.sqlstate.startswith("08"):
                return "lost"
        return "other"


async def open_checker(dsn: str, timeout_s: float) -> PostgresCheckerSession:
    return PostgresCheckerSession(await connect_checker(dsn, timeout_s))


class PostgresAdmin:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._options = ConnectOptions.from_dsn(dsn)

    @property
    def database(self) -> str | None:
        return self._options.database

    async def ready(self, seconds: float) -> None:
        async def probe(options: ConnectOptions) -> bool:
            return await database_exists(options)

        await wait_for_server(probe, self._options, seconds)

    async def exists(self, name: str | None = None) -> bool:
        return await database_exists(self._options, name)

    async def create(self, name: str | None = None, *, template: str | None = None, comment: str | None = None) -> None:
        await create_database(self._options, name, template=template, comment=comment)

    async def create_scratch(self, name: str, *, comment: str) -> None:
        try:
            await self.create(name, template="template0", comment=comment)
        except DatabaseError as error:
            if error.sqlstate != "42501":
                raise
            message = f"{error.message}\nhelp: `--scratch` needs a role with CREATEDB"
            raise InterfaceError(message) from error

    async def drop(self, name: str | None = None, *, force: bool = False) -> None:
        await drop_database(self._options, name, force=force)

    async def list(self, prefix: str) -> Sequence[DatabaseInfo]:
        return await list_databases(self._options, prefix)

    def with_database(self, name: str) -> str:
        return with_database(self._dsn, name)


class PostgresMigrationSession:
    def __init__(self, migrator: Migrator, connection: Connection, *, history_table: str | None = None) -> None:
        self._migrator = migrator
        self._connection = connection
        self._history_table = history_table

    async def run(self, *, target: int | None = None, dry_run: bool = False) -> tuple[MigrationResult, ...]:
        return await self._migrator.run(self._connection, target=target, dry_run=dry_run)

    async def revert(
        self,
        *,
        target: int | None = None,
        all: bool = False,  # ruff: ignore[builtin-argument-shadowing]
        dry_run: bool = False,
    ) -> tuple[MigrationResult, ...]:
        return await self._migrator.revert(self._connection, target=target, all=all, dry_run=dry_run)

    async def skip(self, *, target: int | None = None, dry_run: bool = False) -> tuple[MigrationResult, ...]:
        return await self._migrator.skip(self._connection, target=target, dry_run=dry_run)

    async def info(self) -> tuple[MigrationStatus, ...]:
        try:
            return await self._migrator.info(self._connection)
        except (DatabaseError, MigrateError) as error:
            if self._history_table is None or not any(
                isinstance(cause, DatabaseError) and cause.sqlstate == "42501" for cause in (error, error.__cause__)
            ):
                raise
            # The configured table is public diagnostic context; the database error may contain credentials.
            message = (
                "could not read the migration history: permission denied; "
                f"grant SELECT on {self._history_table} to the checker role"
            )
            raise PermissionError(message) from error

    async def close(self) -> None:
        await self._connection.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()


class PostgresMigrations:
    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        dsn: str,
        path: str | PathLike[str],
        /,
        *,
        table: str = "_rowx_migrations",
        create_schemas: Iterable[str] = (),
        allow_out_of_order: bool = False,
        ignore_missing: bool = False,
    ) -> None:
        self._dsn = dsn
        self._table = table
        self._migrator = Migrator(
            path,
            table=table,
            create_schemas=create_schemas,
            allow_out_of_order=allow_out_of_order,
            ignore_missing=ignore_missing,
        )

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return self._migrator.migrations

    async def open(
        self, *, connect_timeout: float = 10.0, readonly: bool = False, statement_timeout: float = 10.0
    ) -> PostgresMigrationSession:
        if readonly:
            connection = Connection(await connect_checker(self._dsn, statement_timeout))
        else:
            connection = await wait_for_server(connect, ConnectOptions.from_dsn(self._dsn), connect_timeout)
        return PostgresMigrationSession(self._migrator, connection, history_table=self._table if readonly else None)

    async def run_on(self, dsn: str) -> tuple[MigrationResult, ...]:
        async with await connect(dsn) as connection:
            return await self._migrator.run(connection)


class PostgresProvisioner(Provisioner):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @override
    async def ensure_template(
        self, migrator: MigrationService | None, name: str, *, keep_stale: bool, project: str
    ) -> Connection:
        from rowx_postgres._testing import provision_template  # ruff: ignore[import-outside-top-level]

        return await provision_template(self._dsn, migrator, name, keep_stale=keep_stale, project=project)

    @override
    async def clone(self, template: str, project: str) -> str:
        from rowx_postgres._testing import clone  # ruff: ignore[import-outside-top-level]

        return await clone(self._dsn, template, project, test_prefix="rowx_test_")

    @override
    async def drop(self, name: str) -> None:
        await drop_database(self._dsn, name, force=True)

    @override
    def with_database(self, name: str) -> str:
        return with_database(self._dsn, name)

    @override
    async def connect(self, dsn: str) -> Connection:
        return await connect(dsn)

    @override
    def pool(self, dsn: str) -> Pool:
        return Pool(dsn)
