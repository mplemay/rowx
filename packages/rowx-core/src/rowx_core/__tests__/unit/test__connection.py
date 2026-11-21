import asyncio
from contextlib import AsyncExitStack

import pytest
from rowx_core import FieldPlan, ShapeSpec
from rowx_core.__tests__.support.fake_native import COMPILER, FakeNative, native_shape
from rowx_core._connection import BaseConnection
from rowx_core._transaction import BaseTransaction, TrackedStream
from rowx_core.errors import ConnectionBusyError, InterfaceError


class FakePool:
    def __init__(self) -> None:
        self.released: list[object] = []

    async def release(self, conn, /) -> None:
        self.released.append(conn)


type Conn = BaseConnection[FakeNative, object]


def make(native: FakeNative, pool: FakePool | None = None) -> Conn:
    return BaseConnection(native, compiler=COMPILER, native_shape=native_shape, pool=pool)


def spec() -> ShapeSpec:
    return ShapeSpec(
        ctor=dict,
        form="keyword",
        matching="by_name",
        type_name="Row",
        fields=(FieldPlan(key="a", names=("a",), nullable=False, flags=0, required=True),),
    )


def test_the_compiler_is_the_injected_one() -> None:
    assert make(FakeNative()).compiler is COMPILER


def test_fetch_converts_the_shape_through_the_injected_function() -> None:
    native = FakeNative()
    shape = spec()
    fetched = asyncio.run(make(native)._fetch("select 1", [], None, False, False, None, shape))  # ruff: ignore[boolean-positional-value-in-call]
    assert fetched == [1]
    assert native.calls == [("fetch", ("select 1", [], ("native", shape)))]


def test_execute_and_ping_go_to_the_native_connection() -> None:
    native = FakeNative()
    conn = make(native)
    assert asyncio.run(conn._execute("update t", [1])) == 3
    asyncio.run(conn.ping())
    assert [name for name, _ in native.calls] == ["execute", "ping"]


def test_a_plain_connection_keeps_the_bare_stream() -> None:
    native = FakeNative()
    stream = make(native)._stream("select 1", [], None, False, None, 10, None)  # ruff: ignore[boolean-positional-value-in-call]
    assert stream is native.stream


def test_a_stream_inside_a_transaction_is_tracked() -> None:
    native = FakeNative()
    conn = make(native)
    tx = _open_transaction(conn)
    stream = conn._stream("select 1", [], None, False, None, 10, None)  # ruff: ignore[boolean-positional-value-in-call]
    assert isinstance(stream, TrackedStream)
    native.stream.failure = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(stream._next())
    assert tx._failed


def _open_transaction(conn: Conn) -> BaseTransaction[FakeNative, object]:
    async def begin(_impl) -> None:  # ruff: ignore[unused-async]
        return None

    tx = BaseTransaction(conn, begin)
    # the transaction stays open, so nothing exits the stack
    asyncio.run(AsyncExitStack().enter_async_context(tx))
    return tx


def test_a_failure_marks_the_open_transaction_but_a_busy_connection_does_not() -> None:
    native = FakeNative()
    conn = make(native)
    tx = _open_transaction(conn)
    native.failure = ConnectionBusyError("busy")
    with pytest.raises(ConnectionBusyError):
        asyncio.run(conn._execute("x", []))
    assert not tx._failed
    native.failure = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(conn._fetch("x", [], None, False, False, None, None))  # ruff: ignore[boolean-positional-value-in-call]
    assert tx._failed


def test_take_invalidates_the_connection() -> None:
    native = FakeNative()
    conn = make(native, FakePool())
    assert conn._take() is native
    assert conn._released
    assert conn.closed
    with pytest.raises(InterfaceError, match="the connection was released to the pool; acquire another one"):
        conn._impl  # ruff: ignore[useless-expression]


def test_closing_a_pooled_connection_gives_it_back() -> None:
    native = FakeNative()
    pool = FakePool()
    conn = make(native, pool)
    asyncio.run(conn.close())
    assert pool.released == [conn]
    assert not native.closed


def test_closing_a_plain_connection_closes_the_native_one() -> None:
    native = FakeNative()

    async def run() -> None:
        async with make(native):
            assert not native.closed

    asyncio.run(run())
    assert native.closed


def test_detach_forgets_the_pool() -> None:
    native = FakeNative()
    pool = FakePool()
    conn = make(native, pool)
    conn.detach()
    asyncio.run(conn.close())
    assert pool.released == []
    assert native.closed
