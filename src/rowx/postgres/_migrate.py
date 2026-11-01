from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from rowx._core.postgres import Migrator as MigratorImpl
from rowx.errors import InterfaceError
from rowx.postgres._connection import Connection
from rowx.postgres._pool import Pool
from rowx.postgres._transaction import Transaction

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from os import PathLike

    from rowx._core.postgres import Connection as ConnectionImpl, Pool as PoolImpl

DEFAULT_TABLE: Final[str] = "_rowx_migrations"
EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

type MigrationKind = Literal["simple", "up", "down"]
type MigrationState = Literal["applied", "pending", "modified", "missing", "dirty"]
type Row = tuple[int, str, MigrationState, int | None, int | None]


@dataclass(frozen=True, slots=True, kw_only=True)
class Migration:
    version: int
    description: str
    kind: MigrationKind
    # SHA-384 of the raw file bytes
    checksum: bytes
    no_transaction: bool
    path: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationStatus:
    version: int
    description: str
    state: MigrationState
    installed_on: datetime | None = None
    execution_time: timedelta | None = None


def status_from(row: Row) -> MigrationStatus:
    version, description, state, installed_on, execution_time = row
    return MigrationStatus(
        version=version,
        description=description,
        state=state,
        installed_on=None if installed_on is None else EPOCH + timedelta(microseconds=installed_on),
        # -1 is the placeholder of a row whose time is not known (a skipped or still running migration)
        execution_time=None
        if execution_time is None or execution_time < 0
        else timedelta(microseconds=execution_time / 1000),
    )


async def call(
    executor: Connection | Pool,
    start: Callable[[ConnectionImpl | PoolImpl], Awaitable[list[Row]]],
) -> tuple[MigrationStatus, ...]:
    # each migration manages its own transaction, and a `-- no-transaction` file can't run inside one
    if isinstance(executor, Transaction):
        msg = "a migrator runs on a Connection or a Pool, not a Transaction"
        raise InterfaceError(msg)
    if isinstance(executor, Pool):
        # a pool with hooks must run them on the caller's loop, so Python checks the connection out
        if executor._hooked:  # ruff: ignore[private-member-access]
            conn = await executor._checkout(None)  # ruff: ignore[private-member-access]
            try:
                rows = await start(conn._impl)  # ruff: ignore[private-member-access]
            finally:
                await executor._finish(conn)  # ruff: ignore[private-member-access]
        else:
            rows = await start(executor._impl)  # ruff: ignore[private-member-access]
    elif isinstance(executor, Connection):
        if executor._tx is not None:  # ruff: ignore[private-member-access]
            msg = "a migrator can't run on a connection that is inside a transaction"
            raise InterfaceError(msg)
        rows = await start(executor._impl)  # ruff: ignore[private-member-access]
    else:
        msg = "a migrator runs on a Connection or a Pool"
        raise TypeError(msg)
    return tuple(status_from(row) for row in rows)


class Migrator:
    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        path: str | PathLike[str],
        /,
        *,
        table: str = DEFAULT_TABLE,
        create_schemas: Iterable[str] = (),
        locking: bool = True,
        ignore_missing: bool = False,
        allow_out_of_order: bool = False,
    ) -> None:
        if isinstance(create_schemas, str):
            msg = "create_schemas must be an iterable of schema names, not a string"
            raise TypeError(msg)
        # reads and validates the files and the table name; no database IO
        self._impl = MigratorImpl(
            Path(path),
            table,
            list(create_schemas),
            locking,
            ignore_missing,
            allow_out_of_order,
        )

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return tuple(
            Migration(
                version=version,
                description=description,
                kind=kind,
                checksum=checksum,
                no_transaction=no_transaction,
                path=path,
            )
            for version, description, kind, checksum, no_transaction, path in self._impl.migrations
        )

    async def run(
        self,
        executor: Connection | Pool,
        /,
        *,
        target: int | None = None,
        dry_run: bool = False,
    ) -> tuple[MigrationStatus, ...]:
        return await call(executor, lambda impl: self._impl.run(impl, target, dry_run))

    async def undo(
        self,
        executor: Connection | Pool,
        /,
        *,
        # required; `None` reverts only the latest applied migration, chosen under the operation's lock
        target: int | None,
        dry_run: bool = False,
    ) -> tuple[MigrationStatus, ...]:
        return await call(executor, lambda impl: self._impl.undo(impl, target, dry_run))

    async def skip(
        self,
        executor: Connection | Pool,
        /,
        *,
        target: int | None = None,
    ) -> tuple[MigrationStatus, ...]:
        return await call(executor, lambda impl: self._impl.skip(impl, target))

    async def info(self, executor: Connection | Pool, /) -> tuple[MigrationStatus, ...]:
        return await call(executor, self._impl.info)
