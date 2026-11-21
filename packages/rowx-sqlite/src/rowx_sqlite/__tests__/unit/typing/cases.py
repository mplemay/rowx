from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict, reveal_type
from uuid import UUID

from rowx_sqlite import connect

from rowx import query, query_unchecked

if TYPE_CHECKING:
    from rowx_sqlite import Connection, ConnectOptions, Transaction

type Pair = tuple[int, str]


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
    takes_int(await typed.one(conn))  # error  # ty: ignore[invalid-argument-type]


async def positive(conn: Connection) -> None:
    scalar = query[int](t"SELECT 1")
    reveal_type(await scalar.one(conn))  # reveal: int
    reveal_type(await scalar.optional(conn))  # reveal: int | None
    reveal_type(await scalar.all(conn))  # reveal: list[int]
    reveal_type(await scalar.execute(conn))  # reveal: QueryResult
    reveal_type(scalar.fetch(conn))  # reveal: RowStream[int]
    async for value in scalar.fetch(conn, batch_size=10):
        reveal_type(value)  # reveal: int

    row = query[tuple[int, str]](t"SELECT id, name FROM users")
    reveal_type(await row.one(conn))  # reveal: tuple[int, str]
    reveal_type(await row.all(conn))  # reveal: list[tuple[int, str]]

    nullable_row = query[tuple[int, str | None]](t"SELECT id, name FROM users")
    reveal_type(await nullable_row.one(conn))  # reveal: tuple[int, str | None]

    alias = query[Pair](t"SELECT id, name FROM users")
    reveal_type(await alias.one(conn))  # reveal: tuple[int, str]

    rich = query[tuple[Decimal, UUID, date, datetime, bytes]](t"SELECT a, b, c, d, e FROM v")
    reveal_type(await rich.one(conn))  # reveal: tuple[Decimal, UUID, date, datetime, bytes]

    anything = query[Any](t"SELECT 1")
    reveal_type(await anything.all(conn))  # reveal: list[Any]

    unchecked = query_unchecked[tuple[int, str]](t"SELECT id, name FROM users")
    reveal_type(await unchecked.all(conn))  # reveal: list[tuple[int, str]]


async def transaction_is_an_executor(conn: Connection, tx: Transaction) -> None:
    scalar = query[int](t"SELECT 1")
    reveal_type(await scalar.one(tx))  # reveal: int
    reveal_type(await scalar.all(tx))  # reveal: list[int]
    reveal_type(await scalar.execute(tx))  # reveal: QueryResult
    reveal_type(scalar.fetch(tx))  # reveal: RowStream[int]
    async with conn.transaction(mode="deferred", read_only=True) as opened:
        reveal_type(opened)  # reveal: Transaction
        reveal_type(await scalar.one(opened))  # reveal: int
        async with opened.transaction() as nested:
            reveal_type(nested)  # reveal: Transaction


async def opening(options: ConnectOptions) -> None:
    reveal_type(await connect("sqlite::memory:"))  # reveal: Connection
    reveal_type(await connect(options))  # reveal: Connection


def negative() -> None:
    query("SELECT 1")  # error  # ty: ignore[invalid-argument-type]
    name = "users"
    query(f"SELECT * FROM {name}")  # error  # ruff: ignore[hardcoded-sql-expression]  # ty: ignore[invalid-argument-type]
    query[int]("SELECT 1")  # error  # ty: ignore[invalid-argument-type]


class NotAnExecutor:
    pass


async def negative_executor(conn: NotAnExecutor) -> None:
    await query[int](t"SELECT 1").one(conn)  # error  # ty: ignore[invalid-argument-type]


def negative_mode(conn: Connection) -> None:
    conn.transaction(mode="bogus")  # error  # ty: ignore[invalid-argument-type]
