from decimal import Decimal
from typing import Any, reveal_type

from rowx import Connection, query, query_unchecked

type Pair = tuple[int, str]
type UserId = int


async def positive(conn: Connection) -> None:
    scalar = query[int](t"SELECT 1")
    reveal_type(scalar)  # reveal: query[int]
    reveal_type(await scalar.fetch_one(conn))  # reveal: int
    reveal_type(await scalar.fetch_all(conn))  # reveal: list[int]
    reveal_type(await scalar.execute(conn))  # reveal: int

    text = query[str](t"SELECT name FROM users")
    reveal_type(await text.fetch_one(conn))  # reveal: str
    reveal_type(await text.fetch_all(conn))  # reveal: list[str]

    money = query[Decimal](t"SELECT 1.0")
    reveal_type(await money.fetch_one(conn))  # reveal: Decimal

    maybe = query[str | None](t"SELECT name FROM users")
    reveal_type(await maybe.fetch_one(conn))  # reveal: str | None

    row = query[tuple[int, str]](t"SELECT id, name FROM users")
    reveal_type(row)  # reveal: query[tuple[int, str]]
    reveal_type(await row.fetch_one(conn))  # reveal: tuple[int, str]
    reveal_type(await row.fetch_all(conn))  # reveal: list[tuple[int, str]]

    nullable_row = query[tuple[int, str | None]](t"SELECT id, name FROM users")
    reveal_type(await nullable_row.fetch_one(conn))  # reveal: tuple[int, str | None]

    arrays = query[list[int]](t"SELECT ARRAY[1]")
    reveal_type(await arrays.fetch_one(conn))  # reveal: list[int]

    alias = query[Pair](t"SELECT id, name FROM users")
    reveal_type(await alias.fetch_one(conn))  # reveal: tuple[int, str]
    reveal_type(await alias.fetch_all(conn))  # reveal: list[tuple[int, str]]

    scalar_alias = query[UserId](t"SELECT id FROM users")
    reveal_type(await scalar_alias.fetch_one(conn))  # reveal: int

    default = query(t"DELETE FROM users")
    reveal_type(default)  # reveal: query[None]
    reveal_type(await default.execute(conn))  # reveal: int

    explicit_none = query[None](t"DELETE FROM users")
    reveal_type(explicit_none)  # reveal: query[None]

    anything = query[Any](t"SELECT 1")
    reveal_type(anything)  # reveal: query[Any]
    reveal_type(await anything.fetch_one(conn))  # reveal: Any
    reveal_type(await anything.fetch_all(conn))  # reveal: list[Any]

    unchecked = query_unchecked[tuple[int, str]](t"SELECT id, name FROM users")
    reveal_type(unchecked)  # reveal: query_unchecked[tuple[int, str]]
    reveal_type(await unchecked.fetch_all(conn))  # reveal: list[tuple[int, str]]

    unchecked_default = query_unchecked(t"SELECT 1")
    reveal_type(unchecked_default)  # reveal: query_unchecked[None]


def negative() -> None:
    query("SELECT 1")  # error
    name = "users"
    query(f"SELECT * FROM {name}")  # error  # noqa: S608
    query[int]("SELECT 1")  # error
    query[int](f"SELECT {name}")  # error
