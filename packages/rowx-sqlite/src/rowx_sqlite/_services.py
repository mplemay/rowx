from typing import TYPE_CHECKING, Self

from rowx_sqlite._connection import Connection, connect
from rowx_sqlite._core import connect_checker
from rowx_sqlite._migrate import Migrator
from rowx_sqlite._options import ConnectOptions

if TYPE_CHECKING:
    from collections.abc import Iterable
    from os import PathLike
    from types import TracebackType

    from rowx_core.migrations import Migration, MigrationResult, MigrationStatus


def in_memory(dsn: str) -> bool:
    return ConnectOptions.from_dsn(dsn).memory


class SqliteMigrationSession:
    def __init__(self, migrator: Migrator, connection: Connection) -> None:
        self._migrator = migrator
        self._connection = connection

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
        return await self._migrator.info(self._connection)

    async def close(self) -> None:
        await self._connection.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()


class SqliteMigrations:
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
    ) -> SqliteMigrationSession:
        # a read-only session opens the file with `mode=ro` and `PRAGMA query_only = ON`
        if readonly:
            connection = Connection(await connect_checker(self._dsn, statement_timeout))
        else:
            # `connect_timeout` is how long a statement waits for another writer's lock (SQLite's busy timeout)
            connection = await connect(ConnectOptions.from_dsn(self._dsn).with_connect_timeout(connect_timeout))
        return SqliteMigrationSession(self._migrator, connection)

    async def run_on(self, dsn: str) -> tuple[MigrationResult, ...]:
        async with await connect(dsn) as connection:
            return await self._migrator.run(connection)
