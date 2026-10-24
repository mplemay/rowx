import asyncio
from typing import TYPE_CHECKING

import pytest

import rowx
from rowx import query
from rowx.errors import DatabaseError, DecodeError, EncodeError, InterfaceError, RowShapeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[rowx.Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await rowx.connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def test_database_error_syntax_error_has_position(schema: str) -> None:
    with pytest.raises(DatabaseError) as info:
        run_with_conn(schema, query[int](t"SELECT FROM WHERE").fetch_all)
    assert info.value.sqlstate == "42601"
    assert info.value.position == 13


def test_database_error_unique_violation(schema: str) -> None:
    q = query(t"INSERT INTO users (name) VALUES ('a'), ('b')")
    uniq = query(t"CREATE UNIQUE INDEX ON users (name)")

    async def body(conn: rowx.Connection) -> None:
        await uniq.execute(conn)
        await q.execute(conn)
        await q.execute(conn)

    with pytest.raises(DatabaseError) as info:
        run_with_conn(schema, body)
    assert info.value.sqlstate == "23505"
    assert info.value.position is None


def test_encode_error_names_param_and_types(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        run_with_conn(schema, query[int](t"SELECT {'abc'}::int").fetch_one)


def test_decode_error_infinity(schema: str) -> None:
    with pytest.raises(DecodeError):
        run_with_conn(schema, query[object](t"SELECT 'infinity'::date").fetch_one)


def test_decode_error_interval_months(schema: str) -> None:
    with pytest.raises(DecodeError):
        run_with_conn(schema, query[object](t"SELECT '1 month'::interval").fetch_one)


def test_interface_error_closed_connection(schema: str) -> None:
    async def body() -> None:
        conn = await rowx.connect(schema)
        await conn.close()
        assert conn.closed
        await query[int](t"SELECT 1").fetch_one(conn)

    with pytest.raises(InterfaceError):
        asyncio.run(body())


def test_interface_error_param_count_mismatch(schema: str) -> None:
    # the interpolation sits inside a string literal, so the server sees no $1 placeholder
    with pytest.raises(InterfaceError):
        run_with_conn(schema, query[str](t"SELECT '{1}'").fetch_one)


def test_interface_error_absent_row_type_at_fetch(schema: str) -> None:
    with pytest.raises(InterfaceError, match="without a row type"):
        run_with_conn(schema, query(t"SELECT 1").fetch_all)


def test_row_shape_error_arity(schema: str) -> None:
    with pytest.raises(RowShapeError):
        run_with_conn(schema, query[tuple[int, int]](t"SELECT 1").fetch_one)


def test_row_shape_error_arity_on_empty_result(schema: str) -> None:
    with pytest.raises(RowShapeError):
        run_with_conn(schema, query[tuple[int, int]](t"SELECT 1 WHERE false").fetch_all)


def test_row_shape_error_null_in_non_nullable(schema: str) -> None:
    with pytest.raises(RowShapeError):
        run_with_conn(schema, query[int](t"SELECT NULL::int").fetch_one)
