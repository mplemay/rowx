import asyncio
import gc
import inspect
from datetime import date
from typing import TYPE_CHECKING, Final

import pytest

import rowx
from rowx import (
    Connection,
    ConnectionBusyError,
    DecodeError,
    EncodeError,
    InterfaceError,
    QueryResult,
    RowShapeError,
    RowStream,
    query,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

pytestmark = [pytest.mark.integration, pytest.mark.db]

BIG: Final[int] = 1_000_000


def numbers(count: int) -> query[int]:
    return query[int](t"SELECT g FROM generate_series(1, {count}) g")


async def collect[T](rows: RowStream[T], into: list[T]) -> None:
    async for row in rows:
        into.append(row)  # ruff: ignore[manual-list-comprehension]


async def alive(conn: Connection) -> int:
    return await query[int](t"SELECT 1").one(conn)


def run_with_conn(schema: str, body: Callable[[Connection], Coroutine[object, object, object]]) -> object:
    async def run() -> object:
        conn = await rowx.connect(schema)
        try:
            return await body(conn)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_full_stream_yields_every_row_in_order(schema: str) -> None:
    async def body(conn: Connection) -> list[int]:
        return [row async for row in numbers(5000).fetch(conn, batch_size=64)]

    assert run_with_conn(schema, body) == list(range(1, 5001))


def test_result_is_none_midstream_and_rows_affected_when_exhausted(schema: str) -> None:
    async def body(conn: Connection) -> tuple[QueryResult | None, QueryResult | None]:
        stream = numbers(10).fetch(conn, batch_size=2)
        assert stream.result is None
        await anext(stream)
        midway = stream.result
        async for _ in stream:
            pass
        return midway, stream.result

    midway, final = run_with_conn(schema, body)  # ty: ignore[not-iterable]
    assert midway is None
    assert final == QueryResult(rows_affected=10)


def test_break_then_next_query_succeeds_and_result_stays_none(schema: str) -> None:
    async def body(conn: Connection) -> tuple[int, QueryResult | None]:
        # a stream that stays referenced after the break would keep the connection busy
        async for _ in numbers(BIG).fetch(conn):
            break
        ok = await alive(conn)
        async with numbers(BIG).fetch(conn) as rows:
            async for _ in rows:
                break
        return ok, rows.result

    assert run_with_conn(schema, body) == (1, None)


def test_aclose_releases_connection(schema: str) -> None:
    async def body(conn: Connection) -> int:
        stream = numbers(BIG).fetch(conn)
        await anext(stream)
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return await alive(conn)

    assert run_with_conn(schema, body) == 1


def test_async_with_releases_connection(schema: str) -> None:
    async def body(conn: Connection) -> tuple[int, QueryResult | None]:
        async with numbers(BIG).fetch(conn) as rows:
            await anext(rows)
        return await alive(conn), rows.result

    assert run_with_conn(schema, body) == (1, None)


def test_gc_of_unexhausted_stream_releases_connection(schema: str) -> None:
    async def body(conn: Connection) -> int:
        stream = numbers(BIG).fetch(conn)
        await anext(stream)
        del stream
        gc.collect()
        return await alive(conn)

    assert run_with_conn(schema, body) == 1


def test_second_query_while_streaming_raises_busy(schema: str) -> None:
    async def body(conn: Connection) -> int:
        async with numbers(BIG).fetch(conn) as rows:
            await anext(rows)
            with pytest.raises(ConnectionBusyError):
                await alive(conn)
            with pytest.raises(ConnectionBusyError):
                await anext(numbers(3).fetch(conn))
        return await alive(conn)

    assert run_with_conn(schema, body) == 1


def test_close_while_streaming_then_anext_raises_interface_error(schema: str) -> None:
    async def body(conn: Connection) -> None:
        rows = numbers(BIG).fetch(conn, batch_size=1)
        await anext(rows)
        await conn.close()
        with pytest.raises(InterfaceError):
            await anext(rows)

    run_with_conn(schema, body)


def test_close_while_rows_are_buffered_then_anext_raises_interface_error(schema: str) -> None:
    async def body(conn: Connection) -> None:
        rows = numbers(10).fetch(conn)
        await anext(rows)
        await conn.close()
        with pytest.raises(InterfaceError):
            await anext(rows)

    run_with_conn(schema, body)


def test_null_in_non_nullable_column_yields_prior_rows_then_row_shape_error_and_idle(schema: str) -> None:
    async def body(conn: Connection) -> tuple[list[int], int]:
        q = query[int](t"SELECT CASE WHEN g = 3 THEN NULL ELSE g END FROM generate_series(1, 5) g")
        seen: list[int] = []
        with pytest.raises(RowShapeError):
            await collect(q.fetch(conn), seen)
        return seen, await alive(conn)

    assert run_with_conn(schema, body) == ([1, 2], 1)


def test_decode_error_yields_prior_rows_then_decode_error_and_idle(schema: str) -> None:
    async def body(conn: Connection) -> tuple[list[date], int]:
        q = query[date](
            t"SELECT CASE WHEN g = 3 THEN 'infinity'::date ELSE '2020-01-01'::date END FROM generate_series(1, 5) g",
        )
        seen: list[date] = []
        with pytest.raises(DecodeError):
            await collect(q.fetch(conn), seen)
        return seen, await alive(conn)

    seen, ok = run_with_conn(schema, body)  # ty: ignore[not-iterable]
    assert seen == [date(2020, 1, 1)] * 2
    assert ok == 1


def test_batch_size_one_yields_one_row_per_trip(schema: str) -> None:
    async def body(conn: Connection) -> list[int]:
        impl = conn._impl._stream("SELECT g FROM generate_series(1, 20) g", [], [False], True, 1, 1)  # ruff: ignore[boolean-positional-value-in-call]
        sizes: list[int] = []
        while batch := await impl._next():
            sizes.append(len(batch))
        assert impl.rows_affected == 20
        return sizes

    assert run_with_conn(schema, body) == [1] * 20


def test_batch_size_zero_raises_value_error(schema: str) -> None:
    async def body(conn: Connection) -> None:  # ruff: ignore[unused-async]
        with pytest.raises(ValueError, match="batch_size"):
            numbers(3).fetch(conn, batch_size=0)

    run_with_conn(schema, body)


def test_fetch_on_unit_query_raises_interface_error_from_call(schema: str) -> None:
    async def body(conn: Connection) -> None:  # ruff: ignore[unused-async]
        with pytest.raises(InterfaceError):
            query(t"SELECT 1").fetch(conn)

    run_with_conn(schema, body)


def test_arity_and_encode_errors_raise_from_first_anext(schema: str) -> None:
    async def body(conn: Connection) -> int:
        wrong = query[tuple[int, int]](t"SELECT 1").fetch(conn)
        with pytest.raises(RowShapeError):
            await anext(wrong)
        bad = "not a number"
        encoding = query[int](t"SELECT {bad}::int4").fetch(conn)
        with pytest.raises(EncodeError):
            await anext(encoding)
        return await alive(conn)

    assert run_with_conn(schema, body) == 1


def test_anext_is_a_coroutine_function_and_works_in_create_task(schema: str) -> None:
    assert inspect.iscoroutinefunction(RowStream.__anext__)

    async def body(conn: Connection) -> int:
        stream = numbers(3).fetch(conn)
        return await asyncio.create_task(anext(stream))

    assert run_with_conn(schema, body) == 1
