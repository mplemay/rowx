import asyncio
import inspect
from typing import TYPE_CHECKING

import pytest

import rowx
from rowx import query
from rowx.errors import InterfaceError, RowNotFound

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[rowx.Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await rowx.connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def test_scalar_fetch_one(schema: str) -> None:
    assert run_with_conn(schema, query[int](t"SELECT 1").fetch_one) == 1


def test_tuple_fetch_one(schema: str) -> None:
    assert run_with_conn(schema, query[tuple[int, str]](t"SELECT 1, 'a'").fetch_one) == (1, "a")


def test_fetch_all(schema: str) -> None:
    q = query[int](t"SELECT x FROM generate_series(1, 3) x")
    assert run_with_conn(schema, q.fetch_all) == [1, 2, 3]


def test_no_rows_raises_row_not_found(schema: str) -> None:
    with pytest.raises(RowNotFound):
        run_with_conn(schema, query[int](t"SELECT 1 WHERE false").fetch_one)


def test_str_argument_is_type_error() -> None:
    with pytest.raises(TypeError):
        query("SELECT 1")  # ty: ignore[invalid-argument-type]


def test_fetch_without_row_type_is_interface_error(schema: str) -> None:
    with pytest.raises(InterfaceError, match="without a row type"):
        run_with_conn(schema, query(t"SELECT 1").fetch_one)


def test_execute_returns_row_count(schema: str) -> None:
    assert run_with_conn(schema, query(t"SELECT 1").execute) == 1


def test_orig_class_is_present() -> None:
    q = query[int](t"SELECT 1")
    assert q.__orig_class__.__args__ == (int,)  # ty: ignore[unresolved-attribute]


def test_methods_are_coroutine_functions() -> None:
    assert inspect.iscoroutinefunction(query.fetch_one)
    assert inspect.iscoroutinefunction(query.fetch_all)
    assert inspect.iscoroutinefunction(query.execute)
