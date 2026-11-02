from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from rowx._core.postgres import Migrator as MigratorImpl
from rowx.errors import InterfaceError, MigrationFailedError
from rowx.postgres._connection import Connection
from rowx.postgres._pool import Pool
from rowx.postgres._transaction import Transaction

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from os import PathLike

    from rowx._core.postgres import (
        Connection as ConnectionImpl,
        MigrationState,
        Pool as PoolImpl,
        ResultRow,
        StatusRow,
    )

DEFAULT_TABLE: Final[str] = "_rowx_migrations"
EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

type MigrationKind = Literal["simple", "reversible"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Migration:
    version: int
    description: str
    kind: MigrationKind
    # the simple file, or the `.up.sql`
    path: Path
    # the `.down.sql` of a reversible migration
    down_path: Path | None
    # SHA-384 of the up file's raw bytes; the down checksum is never compared against the database
    checksum: bytes
    down_checksum: bytes | None
    no_transaction: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationResult:
    migration: Migration
    # None for a dry run or a skip
    elapsed: timedelta | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationStatus:
    version: int
    description: str
    state: MigrationState
    # None when the migration is missing locally
    kind: MigrationKind | None = None
    installed_on: datetime | None = None
    execution_time: timedelta | None = None
    local_checksum: bytes | None = None
    applied_checksum: bytes | None = None


def to_timedelta(nanos: int | None) -> timedelta | None:
    # -1 is the placeholder of a row whose time is not known (a skipped or still running migration)
    return None if nanos is None or nanos < 0 else timedelta(microseconds=nanos / 1000)


def status_from(row: StatusRow, local: Migration | None) -> MigrationStatus:
    version, description, state, installed_on, execution_time, applied_checksum = row
    return MigrationStatus(
        version=version,
        description=description,
        state=state,
        kind=None if local is None else local.kind,
        installed_on=None if installed_on is None else EPOCH + timedelta(microseconds=installed_on),
        execution_time=to_timedelta(execution_time),
        local_checksum=None if local is None else local.checksum,
        applied_checksum=applied_checksum,
    )


async def call[T](executor: Connection | Pool, start: Callable[[ConnectionImpl | PoolImpl], Awaitable[T]]) -> T:
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
            except BaseException:
                # a failed cleanup marks the connection broken, so its reset raises; the release still
                # discards it, and the migration's own error is the one the caller sees
                with suppress(Exception):
                    await executor._finish(conn)  # ruff: ignore[private-member-access]
                raise
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
    return rows


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
        # resolved against the cwd now, so a later chdir doesn't change what `path` names
        self._path = Path(path).absolute()
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
    def path(self) -> Path:
        return self._path

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return tuple(
            Migration(
                version=version,
                description=description,
                kind=kind,
                path=path,
                down_path=down_path,
                checksum=checksum,
                down_checksum=down_checksum,
                no_transaction=no_transaction,
            )
            for (
                version,
                description,
                kind,
                path,
                down_path,
                checksum,
                down_checksum,
                no_transaction,
            ) in self._impl.migrations
        )

    def _results(self, rows: Iterable[ResultRow]) -> tuple[MigrationResult, ...]:
        by_version = {m.version: m for m in self.migrations}
        return tuple(
            MigrationResult(migration=by_version[version], elapsed=to_timedelta(nanos)) for version, nanos in rows
        )

    async def _change(
        self, executor: Connection | Pool, start: Callable[[ConnectionImpl | PoolImpl], Awaitable[list[ResultRow]]]
    ) -> tuple[MigrationResult, ...]:
        try:
            rows = await call(executor, start)
        except MigrationFailedError as error:
            # the binding reports what completed before the failure as raw (version, nanoseconds) pairs
            error.applied = self._results(error.applied)  # ty: ignore[invalid-argument-type]
            raise
        return self._results(rows)

    async def run(
        self,
        executor: Connection | Pool,
        /,
        *,
        target: int | None = None,
        dry_run: bool = False,
    ) -> tuple[MigrationResult, ...]:
        return await self._change(executor, lambda impl: self._impl.run(impl, target, dry_run))

    async def revert(
        self,
        executor: Connection | Pool,
        /,
        *,
        target: int | None = None,
        all: bool = False,  # ruff: ignore[builtin-argument-shadowing]
        dry_run: bool = False,
    ) -> tuple[MigrationResult, ...]:
        if all and target is not None:
            msg = "pass either target or all=True, not both"
            raise ValueError(msg)
        return await self._change(executor, lambda impl: self._impl.revert(impl, target, all, dry_run))

    async def skip(
        self,
        executor: Connection | Pool,
        /,
        *,
        target: int | None = None,
        dry_run: bool = False,
    ) -> tuple[MigrationResult, ...]:
        return await self._change(executor, lambda impl: self._impl.skip(impl, target, dry_run))

    async def info(self, executor: Connection | Pool, /) -> tuple[MigrationStatus, ...]:
        rows = await call(executor, self._impl.info)
        local = {m.version: m for m in self.migrations}
        return tuple(status_from(row, local.get(row[0])) for row in rows)
