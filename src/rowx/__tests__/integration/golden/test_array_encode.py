import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Final
from uuid import UUID

import pytest

import rowx
from rowx import query
from rowx.errors import EncodeError
from rowx.postgres import Connection, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

AWKWARD: Final[list[str]] = [
    'a"b',
    "a\\b",
    "a,b",
    "{x}",
    "NULL",
    "null",
    "",
    " lead",
    "trail ",
    "two  spaces",
    "é☃",
    "{",
    "}",
]


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def fetch[T](dsn: str, q: query[T]) -> T:
    return run_with_conn(dsn, q.one)


def fetch_all[T](dsn: str, q: query[T]) -> list[T]:
    return run_with_conn(dsn, q.all)


def test_any_with_list(schema: str) -> None:
    ids = [1, 2, 3]
    got = fetch_all(schema, query[int](t"SELECT n FROM generate_series(1, 5) AS n WHERE n = ANY({ids}) ORDER BY n"))
    assert got == [1, 2, 3]


def test_any_with_tuple(schema: str) -> None:
    ids = (4, 5)
    got = fetch_all(schema, query[int](t"SELECT n FROM generate_series(1, 5) AS n WHERE n = ANY({ids}) ORDER BY n"))
    assert got == [4, 5]


def test_int_array_round_trip(schema: str) -> None:
    value = [1, None, 3]
    assert fetch(schema, query[list[int | None]](t"SELECT {value}::int[]")) == value


def test_two_dimensions(schema: str) -> None:
    value = [[1, 2], [3, 4], [5, 6]]
    assert fetch(schema, query[list[list[int]]](t"SELECT {value}::int[]")) == value


@pytest.mark.parametrize("value", AWKWARD)
def test_awkward_text_binary(schema: str, value: str) -> None:
    assert fetch(schema, query[list[str]](t"SELECT {[value]}::text[]")) == [value]


def test_all_awkward_strings_together(schema: str) -> None:
    assert fetch(schema, query[list[str | None]](t"SELECT {[*AWKWARD, None]}::text[]")) == [*AWKWARD, None]


@pytest.mark.parametrize("value", AWKWARD)
def test_awkward_text_literal(schema: str, value: str) -> None:
    # jsonb elements travel as text, so the array becomes a text literal
    assert fetch(schema, query[list[str]](t"SELECT {[value]}::jsonb[]")) == [value]


def test_text_literal_matrix_with_null(schema: str) -> None:
    value = [['a"b', None], ["NULL", "\\"]]
    assert fetch(schema, query[list[list[str | None]]](t"SELECT {value}::jsonb[]")) == value


def test_decimal_array(schema: str) -> None:
    value = [Decimal("1.50"), Decimal("-0.001"), None, Decimal("12345678901234567890.123456789")]
    assert fetch(schema, query[list[Decimal | None]](t"SELECT {value}::numeric[]")) == value


def test_decimal_matrix(schema: str) -> None:
    value = [[Decimal("1.5"), Decimal(2)], [Decimal(3), Decimal("4.25")]]
    assert fetch(schema, query[list[list[Decimal]]](t"SELECT {value}::numeric[]")) == value


def test_empty_list(schema: str) -> None:
    assert fetch(schema, query[list[int]](t"SELECT {[]}::int[]")) == []


def test_empty_decimal_list(schema: str) -> None:
    assert fetch(schema, query[list[Decimal]](t"SELECT {[]}::numeric[]")) == []


def test_list_with_none(schema: str) -> None:
    value = [1, None]
    assert fetch(schema, query[list[int | None]](t"SELECT {value}::int[]")) == value


def test_all_null_numeric(schema: str) -> None:
    value = [None, None]
    assert fetch(schema, query[list[Decimal | None]](t"SELECT {value}::numeric[]")) == value


def test_domain_element_array(schema: str) -> None:
    value = [1, 2, 3]
    assert fetch(schema, query[list[int]](t"SELECT {value}::positive_int[]")) == value


def test_domain_element_violation(schema: str) -> None:
    with pytest.raises(rowx.errors.DatabaseError):
        fetch(schema, query[list[int]](t"SELECT {[0, 1]}::positive_int[]"))


def test_none_array(schema: str) -> None:
    assert fetch(schema, query[list[int] | None](t"SELECT {None}::int[]")) is None


def test_uuid_list_mixing_uuid_and_str(schema: str) -> None:
    u = UUID("12345678-1234-5678-1234-567812345678")
    v = UUID("87654321-4321-8765-4321-876543218765")
    value = [u, str(v), None]
    assert fetch(schema, query[list[UUID | None]](t"SELECT {value}::uuid[]")) == [u, v, None]


@pytest.mark.parametrize(
    "value",
    [[[1, 2], [3]], [1, [2]], [[1], 2], [[1, 2], [3, 4], [5]], [[], [1]], [[], 42], [[1], []]],
)
def test_ragged_raises(schema: str, value: list[object]) -> None:
    with pytest.raises(EncodeError):
        fetch(schema, query[list[list[int]]](t"SELECT {value}::int[]"))


def test_wrong_element_type_raises(schema: str) -> None:
    with pytest.raises(EncodeError):
        fetch(schema, query[list[int]](t"SELECT {['x']}::int[]"))


def test_str_is_not_a_sequence(schema: str) -> None:
    with pytest.raises(EncodeError):
        fetch(schema, query[list[str]](t"SELECT {'abc'}::text[]"))


def test_json_list_still_encodes(schema: str) -> None:
    value = [1, "a", {"k": [None]}]
    assert fetch(schema, query[list[object]](t"SELECT {value}::jsonb")) == value
