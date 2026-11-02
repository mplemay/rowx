import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NamedTuple, NotRequired, TypedDict

import pytest

from rowx import RowStream, query, query_unchecked
from rowx.errors import RowConstructError, RowShapeError
from rowx.postgres import Interval, Pool, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx import Executor

pytestmark = [pytest.mark.integration, pytest.mark.db]

SQL: Final[str] = "SELECT 1::int4 AS id, 'a'::text AS name"


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


@dataclass
class Partial:
    id: int
    name: str | None = None


class PartialRow(NamedTuple):
    id: int
    name: str | None = None


class PartialDict(TypedDict):
    id: int
    name: NotRequired[str]


@dataclass
class Spans:
    span: Interval
    plain: str


@dataclass
class Strict:
    id: int

    def __init__(self, **_row: object) -> None:
        msg = "boom"
        raise TypeError(msg)


FORMS: Final[list[tuple[type, object]]] = [
    (User, User(id=1, name="a")),
    (UserRow, UserRow(1, "a")),
    (UserDict, {"id": 1, "name": "a"}),
]
EXECUTORS: Final[list[str]] = ["connection", "pool", "transaction"]


def run_on[T](dsn: str, kind: str, body: Callable[[Executor], Awaitable[T]]) -> T:
    async def run() -> T:
        if kind == "pool":
            async with Pool(dsn, max_size=1) as pool:
                return await body(pool)
        async with await connect(dsn) as conn:
            if kind == "transaction":
                async with conn.transaction() as tx:
                    return await body(tx)
            return await body(conn)

    return asyncio.run(run())


async def collect[T](stream: RowStream[T]) -> list[T]:
    return [row async for row in stream]


@pytest.mark.parametrize("kind", EXECUTORS)
@pytest.mark.parametrize(("row_type", "expected"), FORMS, ids=[f[0].__name__ for f in FORMS])
def test_each_form_builds_rows_through_one_optional_all_and_fetch(
    schema: str, kind: str, row_type: type, expected: object
) -> None:
    q = query[row_type](t"SELECT g::int4 AS id, 'a'::text AS name FROM generate_series(1, 2) g")  # ty: ignore[invalid-type-form]
    first = query[row_type](t"SELECT 1::int4 AS id, 'a'::text AS name")  # ty: ignore[invalid-type-form]
    none = query[row_type](t"SELECT 1::int4 AS id, 'a'::text AS name WHERE false")  # ty: ignore[invalid-type-form]

    async def body(conn: Executor) -> tuple[object, ...]:
        return (
            await first.one(conn),
            await first.optional(conn),
            await none.optional(conn),
            await q.all(conn),
            await collect(q.fetch(conn, batch_size=1)),
        )

    one, optional, missing, every, streamed = run_on(schema, kind, body)
    assert one == expected
    assert optional == expected
    assert missing is None
    assert isinstance(every, list)
    assert len(every) == 2
    assert every == streamed
    assert type(one) is type(expected)


def test_a_dataclass_row_is_an_instance_of_the_class(schema: str) -> None:
    row = run_on(schema, "connection", query[User](t"SELECT 3::int4 AS id, 'z'::text AS name").one)
    assert isinstance(row, User)
    assert (row.id, row.name) == (3, "z")


def test_by_name_forms_ignore_the_column_order(schema: str) -> None:
    sql = t"SELECT 'a'::text AS name, 1::int4 AS id"

    async def body(conn: Executor) -> tuple[object, object]:
        return await query[User](sql).one(conn), await query[UserDict](sql).one(conn)

    assert run_on(schema, "connection", body) == (User(id=1, name="a"), {"name": "a", "id": 1})


@pytest.mark.parametrize("override", ["!", "?"])
def test_override_suffixes_map_to_the_plain_field_name(schema: str, override: str) -> None:
    from string.templatelib import Template  # ruff: ignore[import-outside-top-level]

    sql = Template(f'SELECT 1::int4 AS "id{override}", \'a\'::text AS "name{override}"')

    async def body(conn: Executor) -> tuple[object, object, object]:
        return (
            await query[User](sql).one(conn),
            await query[UserDict](sql).one(conn),
            await query[UserRow](sql).one(conn),
        )

    assert run_on(schema, "connection", body) == (User(id=1, name="a"), {"id": 1, "name": "a"}, UserRow(1, "a"))


def test_a_field_with_a_default_may_have_no_column(schema: str) -> None:
    sql = t"SELECT 1::int4 AS id"

    async def body(conn: Executor) -> tuple[object, object, object]:
        return (
            await query[Partial](sql).one(conn),
            await query[PartialRow](sql).one(conn),
            await query[PartialDict](sql).one(conn),
        )

    assert run_on(schema, "connection", body) == (Partial(id=1), PartialRow(1), {"id": 1})


def test_an_interval_field_reads_a_lossless_interval(schema: str) -> None:
    q = query[Spans](t"SELECT interval '1 month 2 days' AS span, 'x'::text AS plain")
    row = run_on(schema, "connection", q.one)
    assert isinstance(row.span, Interval)


# an empty result proves that the mapping fails before execution, not on the first row
@pytest.mark.parametrize(
    ("row_type", "sql", "message"),
    [
        (User, "SELECT 1::int4 AS id WHERE false", 'field "name" of `User` has no column'),
        (
            User,
            "SELECT 1::int4 AS id, 'a'::text AS name, 2::int4 AS age WHERE false",
            'column 3 "age" matches no field',
        ),
        (UserDict, "SELECT 1::int4 AS id WHERE false", 'field "name" of `UserDict` has no column'),
        (UserDict, "SELECT 1::int4 AS id, 'a'::text AS name, 2 AS age WHERE false", 'column 3 "age" matches no field'),
        (
            UserRow,
            "SELECT 'a'::text AS name, 1::int4 AS id WHERE false",
            'column 1 "name" faces field "id" of `UserRow`',
        ),
    ],
)
def test_a_shape_mismatch_is_a_row_shape_error_before_execution(
    schema: str, row_type: type, sql: str, message: str
) -> None:
    from string.templatelib import Template  # ruff: ignore[import-outside-top-level]

    q = query[row_type](Template(sql))  # ty: ignore[invalid-type-form]
    with pytest.raises(RowShapeError, match=message):
        run_on(schema, "connection", q.all)


def test_a_hand_written_init_that_raises_is_a_row_construct_error(schema: str) -> None:
    with pytest.raises(RowConstructError) as caught:
        run_on(schema, "connection", query[Strict](t"SELECT 1::int4 AS id").one)
    assert caught.value.type_name == "Strict"
    assert caught.value.row_index == 0
    assert isinstance(caught.value.__cause__, TypeError)


def test_query_unchecked_builds_named_rows(schema: str) -> None:
    table = "users"
    q = query_unchecked[UserRow](t"SELECT id, name FROM {table:ident} ORDER BY id LIMIT 1")
    assert run_on(schema, "connection", q.one) == UserRow(1, "alice")
