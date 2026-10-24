import asyncio
from typing import TYPE_CHECKING

import pytest

import rowx
from rowx.errors import DecodeError, InterfaceError, RowShapeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx._core import Connection as ConnectionImpl

pytestmark = [pytest.mark.integration, pytest.mark.db]


async def fetch(  # noqa: PLR0913
    conn: ConnectionImpl,
    sql: str,
    *,
    params: list[object] | None = None,
    mask: list[bool] | None = None,
    scalar: bool = False,
    limit_one: bool = False,
    arity: int | None = None,
) -> list[object]:
    return await conn._fetch(sql, params or [], mask, scalar, limit_one, arity)


async def execute(conn: ConnectionImpl, sql: str) -> int:
    return await conn._execute(sql, [])


def run_with_conn[T](dsn: str, body: Callable[[ConnectionImpl], Awaitable[T]]) -> T:
    async def run() -> T:
        conn = await rowx.connect(dsn)
        try:
            return await body(conn._impl)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_fetch_rows(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, "SELECT 1, 'a'", mask=[False, False], arity=2)

    assert run_with_conn(schema, body) == [(1, "a")]


def test_fetch_scalar(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, "SELECT x FROM generate_series(1, 3) x", mask=[False], scalar=True, arity=1)

    assert run_with_conn(schema, body) == [1, 2, 3]


def test_fetch_decodes_supported_types(schema: str) -> None:
    sql = "SELECT true, 2::int2, 4::int4, 8::int8, 'v'::varchar, 't'::text, NULL::int4"

    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, sql, mask=[False, False, False, False, False, False, True], arity=7)

    [row] = run_with_conn(schema, body)
    assert row == (True, 2, 4, 8, "v", "t", None)
    assert isinstance(row, tuple)
    assert type(row[0]) is bool


def test_fetch_void_is_none(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, "SELECT pg_sleep(0)", scalar=True, arity=1)

    assert run_with_conn(schema, body) == [None]


def test_limit_one_leaves_connection_usable(schema: str) -> None:
    async def body(c: ConnectionImpl) -> tuple[list[object], list[object]]:
        first = await fetch(
            c,
            "SELECT * FROM generate_series(1, 100000)",
            mask=[False],
            scalar=True,
            limit_one=True,
            arity=1,
        )
        second = await fetch(c, "SELECT 2", mask=[False], scalar=True, arity=1)
        return first, second

    assert run_with_conn(schema, body) == ([1], [2])


def test_arity_mismatch_without_rows(schema: str) -> None:
    async def body(c: ConnectionImpl) -> None:
        await fetch(c, "SELECT 1 WHERE false", arity=2)

    with pytest.raises(RowShapeError):
        run_with_conn(schema, body)


def test_null_in_non_nullable_position(schema: str) -> None:
    async def body(c: ConnectionImpl) -> None:
        await fetch(c, "SELECT NULL::int4", mask=[False], arity=1)

    with pytest.raises(RowShapeError, match="index 0"):
        run_with_conn(schema, body)


def test_unsupported_type_is_decode_error(schema: str) -> None:
    async def body(c: ConnectionImpl) -> tuple[bool, list[object]]:
        with pytest.raises(DecodeError, match="money"):
            await fetch(c, "SELECT 1::money", scalar=True, arity=1)
        # a decode failure still releases the connection
        return True, await fetch(c, "SELECT 1", scalar=True, arity=1)

    assert run_with_conn(schema, body) == (True, [1])


def test_execute_row_counts(schema: str) -> None:
    async def body(c: ConnectionImpl) -> tuple[int, int]:
        created = await execute(c, "CREATE TEMP TABLE x(i int)")
        inserted = await execute(c, "INSERT INTO x SELECT generate_series(1, 3)")
        return created, inserted

    assert run_with_conn(schema, body) == (0, 3)


def test_params_are_bound(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, "SELECT $1::int4", params=[1], scalar=True, arity=1)

    assert run_with_conn(schema, body) == [1]


def test_sequential_awaits_never_see_busy(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return [await fetch(c, "SELECT 1", scalar=True, arity=1) for _ in range(50)]

    assert len(run_with_conn(schema, body)) == 50


def test_closed_connection_is_interface_error(schema: str) -> None:
    async def run() -> None:
        conn = await rowx.connect(schema)
        await conn.close()
        await fetch(conn._impl, "SELECT 1", scalar=True, arity=1)

    with pytest.raises(InterfaceError):
        asyncio.run(run())
