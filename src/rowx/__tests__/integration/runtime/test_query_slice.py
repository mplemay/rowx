import asyncio
import inspect
from typing import TYPE_CHECKING

import pytest

from rowx import QueryResult, query
from rowx.errors import InterfaceError, RowNotFound
from rowx.postgres import Connection, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def test_scalar_one(schema: str) -> None:
    assert run_with_conn(schema, query[int](t"SELECT 1").one) == 1


def test_tuple_one(schema: str) -> None:
    assert run_with_conn(schema, query[tuple[int, str]](t"SELECT 1, 'a'").one) == (1, "a")


def test_all(schema: str) -> None:
    q = query[int](t"SELECT x FROM generate_series(1, 3) x")
    assert run_with_conn(schema, q.all) == [1, 2, 3]


def test_no_rows_raises_row_not_found(schema: str) -> None:
    with pytest.raises(RowNotFound):
        run_with_conn(schema, query[int](t"SELECT 1 WHERE false").one)


def test_str_argument_is_type_error() -> None:
    with pytest.raises(TypeError):
        query("SELECT 1")  # ty: ignore[invalid-argument-type]


def test_fetch_without_row_type_is_interface_error(schema: str) -> None:
    with pytest.raises(InterfaceError, match="without a row type"):
        run_with_conn(schema, query(t"SELECT 1").one)


def test_execute_returns_row_count(schema: str) -> None:
    assert run_with_conn(schema, query(t"SELECT 1").execute) == QueryResult(rows_affected=1)


def test_execute_update_rows_affected(schema: str) -> None:
    q = query(t"UPDATE users SET name = name WHERE id > {0}")
    assert run_with_conn(schema, q.execute).rows_affected == 3


def test_execute_ddl_rows_affected_is_zero(schema: str) -> None:
    assert run_with_conn(schema, query(t"CREATE TEMP TABLE rowx_ddl (id int)").execute).rows_affected == 0


def test_optional_no_rows_is_none(schema: str) -> None:
    assert run_with_conn(schema, query[int](t"SELECT 1 WHERE false").optional) is None


def test_optional_many_rows_returns_first(schema: str) -> None:
    q = query[int](t"SELECT x FROM generate_series(1, 3) x ORDER BY x")
    assert run_with_conn(schema, q.optional) == 1


def test_optional_without_row_type_is_interface_error(schema: str) -> None:
    with pytest.raises(InterfaceError, match="without a row type"):
        run_with_conn(schema, query(t"SELECT 1").optional)


def test_orig_class_is_present() -> None:
    q = query[int](t"SELECT 1")
    assert q.__orig_class__.__args__ == (int,)  # ty: ignore[unresolved-attribute]


def test_methods_are_coroutine_functions() -> None:
    assert inspect.iscoroutinefunction(query.one)
    assert inspect.iscoroutinefunction(query.optional)
    assert inspect.iscoroutinefunction(query.all)
    assert inspect.iscoroutinefunction(query.execute)
