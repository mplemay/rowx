import asyncio
from typing import TYPE_CHECKING, Self

import pytest
from rowx_core._pool import BaseAcquire, BasePooledStream, BasePoolTransaction
from rowx_core.errors import InterfaceError

if TYPE_CHECKING:
    from collections.abc import Sequence


class FakeConn:
    def __init__(self, name: str) -> None:
        self.name = name
        self.streams: list[FakeStream] = []

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
        shape: object,
    ) -> FakeStream:
        stream = FakeStream(self.name, (sql, params, mask, scalar, arity, batch_size, shape))
        self.streams.append(stream)
        return stream


class FakeStream:
    rows_affected = 4

    def __init__(self, name: str, args: tuple[object, ...]) -> None:
        self.name = name
        self.args = args
        self.batches: list[list[object]] = [[1, 2], []]
        self.closed = False
        self.failure: BaseException | None = None

    async def _next(self) -> list[object]:
        if self.failure is not None:
            raise self.failure
        return self.batches.pop(0)

    def close(self) -> None:
        self.closed = True


class FakePool:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.checkout_failure: BaseException | None = None

    async def _checkout(self, limit: float | None) -> FakeConn:
        self.events.append(f"checkout {limit}")
        if self.checkout_failure is not None:
            raise self.checkout_failure
        return FakeConn("c")

    async def _finish(self, conn: FakeConn) -> None:
        self.events.append(f"finish {conn.name}")

    def _release_soon(self, conn: FakeConn) -> None:
        self.events.append(f"soon {conn.name}")


class FakeTx:
    def __init__(self, conn: FakeConn, enter_failure: BaseException | None = None) -> None:
        self.conn = conn
        self.enter_failure = enter_failure
        self.exited: object = None

    async def __aenter__(self) -> Self:
        if self.enter_failure is not None:
            raise self.enter_failure
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = exc_type


def test_acquire_as_a_context_manager_finishes_the_connection() -> None:
    pool = FakePool()

    async def body() -> None:
        async with BaseAcquire(pool, 2.0) as conn:
            assert conn.name == "c"

    asyncio.run(body())
    assert pool.events == ["checkout 2.0", "finish c"]


def test_acquire_awaited_leaves_the_connection_out() -> None:
    pool = FakePool()

    async def body() -> FakeConn:
        return await BaseAcquire(pool, None)

    assert asyncio.run(body()).name == "c"
    assert pool.events == ["checkout None"]


def test_a_transaction_begins_through_the_supplied_callable() -> None:
    pool = FakePool()
    made: list[FakeTx] = []

    def begin(conn: FakeConn) -> FakeTx:
        made.append(FakeTx(conn))
        return made[-1]

    async def body() -> None:
        async with BasePoolTransaction(pool, begin) as tx:
            assert tx is made[0]

    asyncio.run(body())
    assert pool.events == ["checkout None", "finish c"]
    assert made[0].exited is None


def test_a_transaction_passes_the_body_exception_and_still_finishes() -> None:
    pool = FakePool()
    made: list[FakeTx] = []

    def begin(conn: FakeConn) -> FakeTx:
        made.append(FakeTx(conn))
        return made[-1]

    async def body() -> None:
        async with BasePoolTransaction(pool, begin):
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(body())
    assert made[0].exited is RuntimeError
    assert pool.events == ["checkout None", "finish c"]


def test_a_failed_begin_does_not_leak_the_checkout() -> None:
    pool = FakePool()

    async def body() -> None:
        async with BasePoolTransaction(pool, lambda conn: FakeTx(conn, RuntimeError("begin failed"))):
            pass

    with pytest.raises(RuntimeError, match="begin failed"):
        asyncio.run(body())
    assert pool.events == ["checkout None", "finish c"]


def test_a_transaction_can_only_be_entered_once() -> None:
    pool = FakePool()
    block = BasePoolTransaction(pool, FakeTx)

    async def body() -> None:
        async with block:
            pass
        async with block:
            pass

    with pytest.raises(InterfaceError, match="a transaction can only be entered once"):
        asyncio.run(body())
    assert pool.events == ["checkout None", "finish c"]


def test_a_pooled_stream_checks_out_lazily_and_gives_back_at_the_end() -> None:
    pool = FakePool()
    args = ("select 1", [], None, False, None, 10, None)
    stream = BasePooledStream(pool, args)
    assert stream.rows_affected is None

    async def body() -> list[list[object]]:
        return [await stream._next(), await stream._next(), await stream._next()]

    assert asyncio.run(body()) == [[1, 2], [], []]
    assert pool.events == ["checkout None", "finish c"]
    assert stream.rows_affected == 4


def test_a_pooled_stream_gives_back_on_failure() -> None:
    pool = FakePool()
    stream = BasePooledStream(pool, ("q", [], None, False, None, 1, None))

    async def body() -> None:
        await stream._next()
        assert isinstance(stream._inner, FakeStream)
        stream._inner.failure = RuntimeError("boom")
        await stream._next()

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(body())
    assert pool.events == ["checkout None", "finish c"]


def test_closing_a_pooled_stream_releases_in_the_background() -> None:
    pool = FakePool()
    stream = BasePooledStream(pool, ("q", [], None, False, None, 1, None))

    async def body() -> None:
        await stream._next()
        stream.close()
        assert await stream._next() == []

    asyncio.run(body())
    assert isinstance(stream._inner, FakeStream)
    assert stream._inner.closed
    assert pool.events == ["checkout None", "soon c"]


def test_closing_before_the_first_batch_checks_nothing_out() -> None:
    pool = FakePool()
    stream = BasePooledStream(pool, ("q", [], None, False, None, 1, None))
    stream.close()
    assert asyncio.run(stream._next()) == []
    assert pool.events == []
