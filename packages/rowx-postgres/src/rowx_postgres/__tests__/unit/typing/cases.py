from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypedDict, reveal_type

from rowx import query, query_unchecked

if TYPE_CHECKING:
    from rowx_postgres import Connection, Migrator, Pool, Transaction

type Pair = tuple[int, str]
type UserId = int


@dataclass
class User:
    id: int
    name: str


class UserRow(NamedTuple):
    id: int
    name: str


class UserDict(TypedDict):
    id: int
    name: str


def takes_int(value: int) -> None: ...


def takes_user_row(value: UserRow) -> None: ...


def takes_user_dict(value: UserDict) -> None: ...


def takes_user_dicts(value: list[UserDict]) -> None: ...


async def named_rows(conn: Connection) -> None:
    user = query[User](t"SELECT id, name FROM users")
    reveal_type(user)  # reveal: query[User]
    reveal_type(await user.one(conn))  # reveal: User
    reveal_type(await user.optional(conn))  # reveal: User | None
    reveal_type(await user.all(conn))  # reveal: list[User]
    reveal_type(user.fetch(conn))  # reveal: RowStream[User]

    # mypy spells a NamedTuple and a TypedDict out structurally, so these are checked by assignment
    row = query[UserRow](t"SELECT id, name FROM users")
    takes_user_row(await row.one(conn))
    takes_int(await row.one(conn))  # error  # ty: ignore[invalid-argument-type]

    typed = query[UserDict](t"SELECT id, name FROM users")
    takes_user_dict(await typed.one(conn))
    takes_user_dicts(await typed.all(conn))
    takes_int(await typed.one(conn))  # error  # ty: ignore[invalid-argument-type]

    unchecked = query_unchecked[User](t"SELECT id, name FROM users")
    reveal_type(await unchecked.one(conn))  # reveal: User


async def positive(conn: Connection) -> None:
    scalar = query[int](t"SELECT 1")
    reveal_type(scalar)  # reveal: query[int]
    reveal_type(await scalar.one(conn))  # reveal: int
    reveal_type(await scalar.optional(conn))  # reveal: int | None
    reveal_type(await scalar.all(conn))  # reveal: list[int]
    reveal_type(await scalar.execute(conn))  # reveal: QueryResult
    reveal_type(scalar.fetch(conn))  # reveal: RowStream[int]
    async for value in scalar.fetch(conn, batch_size=10):
        reveal_type(value)  # reveal: int

    text = query[str](t"SELECT name FROM users")
    reveal_type(await text.one(conn))  # reveal: str
    reveal_type(await text.all(conn))  # reveal: list[str]

    money = query[Decimal](t"SELECT 1.0")
    reveal_type(await money.one(conn))  # reveal: Decimal

    maybe = query[str | None](t"SELECT name FROM users")
    reveal_type(await maybe.one(conn))  # reveal: str | None

    row = query[tuple[int, str]](t"SELECT id, name FROM users")
    reveal_type(row)  # reveal: query[tuple[int, str]]
    reveal_type(await row.one(conn))  # reveal: tuple[int, str]
    reveal_type(await row.optional(conn))  # reveal: tuple[int, str] | None
    reveal_type(await row.all(conn))  # reveal: list[tuple[int, str]]
    reveal_type(row.fetch(conn))  # reveal: RowStream[tuple[int, str]]
    async for pair in row.fetch(conn):
        reveal_type(pair)  # reveal: tuple[int, str]

    nullable_row = query[tuple[int, str | None]](t"SELECT id, name FROM users")
    reveal_type(await nullable_row.one(conn))  # reveal: tuple[int, str | None]

    arrays = query[list[int]](t"SELECT ARRAY[1]")
    reveal_type(await arrays.one(conn))  # reveal: list[int]

    alias = query[Pair](t"SELECT id, name FROM users")
    reveal_type(await alias.one(conn))  # reveal: tuple[int, str]
    reveal_type(await alias.optional(conn))  # reveal: tuple[int, str] | None
    reveal_type(await alias.all(conn))  # reveal: list[tuple[int, str]]

    scalar_alias = query[UserId](t"SELECT id FROM users")
    reveal_type(await scalar_alias.one(conn))  # reveal: int

    default = query(t"DELETE FROM users")
    reveal_type(default)  # reveal: query[None]
    reveal_type(await default.execute(conn))  # reveal: QueryResult

    explicit_none = query[None](t"DELETE FROM users")
    reveal_type(explicit_none)  # reveal: query[None]

    anything = query[Any](t"SELECT 1")
    reveal_type(anything)  # reveal: query[Any]
    reveal_type(await anything.one(conn))  # reveal: Any
    reveal_type(await anything.all(conn))  # reveal: list[Any]

    unchecked = query_unchecked[tuple[int, str]](t"SELECT id, name FROM users")
    reveal_type(unchecked)  # reveal: query_unchecked[tuple[int, str]]
    reveal_type(await unchecked.all(conn))  # reveal: list[tuple[int, str]]

    unchecked_default = query_unchecked(t"SELECT 1")
    reveal_type(unchecked_default)  # reveal: query_unchecked[None]


async def pool_is_an_executor(pool: Pool) -> None:
    scalar = query[int](t"SELECT 1")
    reveal_type(await scalar.one(pool))  # reveal: int
    reveal_type(await scalar.all(pool))  # reveal: list[int]
    reveal_type(await scalar.execute(pool))  # reveal: QueryResult
    reveal_type(scalar.fetch(pool))  # reveal: RowStream[int]


async def transaction_is_an_executor(tx: Transaction) -> None:
    scalar = query[int](t"SELECT 1")
    reveal_type(await scalar.one(tx))  # reveal: int
    reveal_type(await scalar.all(tx))  # reveal: list[int]
    reveal_type(await scalar.execute(tx))  # reveal: QueryResult
    reveal_type(scalar.fetch(tx))  # reveal: RowStream[int]


async def pool_transaction_is_an_executor(pool: Pool) -> None:
    async with pool.transaction() as tx:
        reveal_type(tx)  # reveal: Transaction
        reveal_type(await query[int](t"SELECT 1").one(tx))  # reveal: int


def negative() -> None:
    query("SELECT 1")  # error  # ty: ignore[invalid-argument-type]
    name = "users"
    query(f"SELECT * FROM {name}")  # error  # ruff: ignore[hardcoded-sql-expression]  # ty: ignore[invalid-argument-type]
    query[int]("SELECT 1")  # error  # ty: ignore[invalid-argument-type]
    query[int](f"SELECT {name}")  # error  # ty: ignore[invalid-argument-type]


class NotAnExecutor:
    pass


async def negative_executor(conn: NotAnExecutor) -> None:
    await query[int](t"SELECT 1").one(conn)  # error  # ty: ignore[invalid-argument-type]


def takes_kind(kind: Literal["simple", "reversible"]) -> None: ...


def takes_optional_kind(kind: Literal["simple", "reversible"] | None) -> None: ...


def takes_legacy_kind(kind: Literal["up", "down"]) -> None: ...


async def migrator_runs_on_connection_or_pool(migrator: Migrator, conn: Connection, pool: Pool) -> None:
    reveal_type(migrator.migrations)  # reveal: tuple[Migration, ...]
    reveal_type(migrator.path)  # reveal: Path
    reveal_type(await migrator.info(conn))  # reveal: tuple[MigrationStatus, ...]
    reveal_type(await migrator.info(pool))  # reveal: tuple[MigrationStatus, ...]
    reveal_type(await migrator.run(pool, target=1))  # reveal: tuple[MigrationResult, ...]
    reveal_type(await migrator.revert(conn))  # reveal: tuple[MigrationResult, ...]
    reveal_type(await migrator.revert(conn, target=1))  # reveal: tuple[MigrationResult, ...]
    reveal_type(await migrator.revert(conn, all=True))  # reveal: tuple[MigrationResult, ...]
    reveal_type(await migrator.run(conn, dry_run=True))  # reveal: tuple[MigrationResult, ...]
    reveal_type(await migrator.revert(pool, target=None, dry_run=True))  # reveal: tuple[MigrationResult, ...]
    reveal_type(await migrator.skip(conn))  # reveal: tuple[MigrationResult, ...]
    reveal_type(await migrator.skip(conn, dry_run=True))  # reveal: tuple[MigrationResult, ...]
    # `target` with `all` is accepted by the type checker and rejected at runtime with a ValueError
    reveal_type(await migrator.revert(conn, target=1, all=True))  # reveal: tuple[MigrationResult, ...]
    for result in await migrator.run(conn):
        reveal_type(result.migration)  # reveal: Migration
        takes_kind(result.migration.kind)
        takes_legacy_kind(result.migration.kind)  # error  # ty: ignore[invalid-argument-type]
        reveal_type(result.elapsed)  # reveal: timedelta | None
        reveal_type(result.migration.down_path)  # reveal: Path | None
    for status in await migrator.info(conn):
        takes_optional_kind(status.kind)
        takes_kind(status.kind)  # error  # ty: ignore[invalid-argument-type]
        reveal_type(status.applied_checksum)  # reveal: bytes | None


async def negative_migrator(migrator: Migrator, tx: Transaction) -> None:
    await migrator.run(tx)  # error  # ty: ignore[invalid-argument-type]
    await migrator.info(tx)  # error  # ty: ignore[invalid-argument-type]


async def negative_migrator_arguments(migrator: Migrator, conn: Connection) -> None:
    await migrator.undo(conn)  # error  # ty: ignore[missing-attribute]
    await migrator.revert(conn, 1)  # error  # ty: ignore[too-many-positional-arguments]
    await migrator.info(conn, dry_run=True)  # error  # ty: ignore[unknown-argument]
