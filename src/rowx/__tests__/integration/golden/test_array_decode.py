import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

import rowx
from rowx import query

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[rowx.Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await rowx.connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def fetch[T](dsn: str, q: query[T]) -> T:
    return run_with_conn(dsn, q.one)


def test_one_dimension_with_null(schema: str) -> None:
    assert fetch(schema, query[list[int | None]](t"SELECT ARRAY[1,NULL,3]")) == [1, None, 3]


def test_two_dimensions(schema: str) -> None:
    got = fetch(schema, query[list[list[int]]](t"SELECT '{{{{1,2}},{{3,4}}}}'::int[]"))
    assert got == [[1, 2], [3, 4]]


def test_three_dimensions(schema: str) -> None:
    got = fetch(schema, query[list[list[list[int]]]](t"SELECT '{{{{{{1,2}},{{3,4}}}},{{{{5,6}},{{7,8}}}}}}'::int[]"))
    assert got == [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]


def test_empty_array(schema: str) -> None:
    assert fetch(schema, query[list[str]](t"SELECT '{{}}'::text[]")) == []


def test_all_null_array(schema: str) -> None:
    assert fetch(schema, query[list[int | None]](t"SELECT ARRAY[NULL, NULL]::int[]")) == [None, None]


def test_null_array_is_none(schema: str) -> None:
    assert fetch(schema, query[list[int] | None](t"SELECT NULL::int[]")) is None


def test_text_elements_keep_special_characters(schema: str) -> None:
    got = fetch(schema, query[list[str | None]](t"""SELECT ARRAY['a,b', 'q"r', '', 'NULL', NULL]"""))
    assert got == ["a,b", 'q"r', "", "NULL", None]


def test_non_default_lower_bound_is_ignored(schema: str) -> None:
    got = fetch(schema, query[list[int]](t"SELECT '[5:7]={{1,2,3}}'::int[]"))
    assert got == [1, 2, 3]


def test_enum_array(schema: str) -> None:
    got = fetch(schema, query[list[str]](t"SELECT ARRAY['sad','happy']::mood[]"))
    assert got == ["sad", "happy"]


def test_domain_array(schema: str) -> None:
    got = fetch(schema, query[list[int]](t"SELECT ARRAY[1,2]::positive_int[]"))
    assert got == [1, 2]


def test_domain_over_array(schema: str) -> None:
    got = fetch(schema, query[list[int]](t"SELECT ARRAY[3,4]::positive_ints"))
    assert got == [3, 4]


def test_uuid_array(schema: str) -> None:
    text = "123e4567-e89b-12d3-a456-426614174000"
    got = fetch(schema, query[list[UUID]](t"SELECT ARRAY[{text}::uuid]"))
    assert got == [UUID(text)]


def test_timestamptz_array(schema: str) -> None:
    got = fetch(schema, query[list[datetime]](t"SELECT ARRAY['2024-02-29 12:00:00+00'::timestamptz]"))
    assert got == [datetime(2024, 2, 29, 12, tzinfo=UTC)]
