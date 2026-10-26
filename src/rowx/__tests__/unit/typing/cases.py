from decimal import Decimal
from typing import TYPE_CHECKING, Any, reveal_type

from rowx import query, query_unchecked

if TYPE_CHECKING:
    from rowx.postgres import Connection, Pool, Transaction

type Pair = tuple[int, str]
type UserId = int


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
