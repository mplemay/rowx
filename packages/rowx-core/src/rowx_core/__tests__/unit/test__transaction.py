import asyncio
import gc
from contextlib import AsyncExitStack

import pytest
from rowx_core.__tests__.support.fake_native import COMPILER, FakeNative, native_shape
from rowx_core._connection import BaseConnection
from rowx_core._transaction import BaseTransaction
from rowx_core.errors import ConnectionBusyError, InterfaceError

type Conn = BaseConnection[FakeNative, object]
type Tx = BaseTransaction[FakeNative, object]


def make(native: FakeNative) -> Conn:
    return BaseConnection(native, compiler=COMPILER, native_shape=native_shape)


def transaction(conn: Conn, native: FakeNative, *args: object, parent: Tx | None = None) -> Tx:
    # the begin arguments belong to the backend, so the base only awaits what it is given
    async def begin(impl: FakeNative) -> None:  # ruff: ignore[unused-async]
        assert impl is native
        impl.log.append("begin " + " ".join(map(str, args)))

    return BaseTransaction(conn, begin, parent=parent)


def run(coro):
    return asyncio.run(coro)


def enter(tx: Tx) -> None:
    # the transaction stays open, so nothing exits the stack
    run(AsyncExitStack().enter_async_context(tx))


async def enter_and_exit(tx: Tx) -> None:
    async with tx:
        pass


def test_the_begin_arguments_are_supplied_by_the_backend() -> None:
    native = FakeNative()
    conn = make(native)

    async def body() -> None:
        async with transaction(conn, native, "immediate"):
            pass

    run(body())
    assert native.log == ["begin immediate", "commit"]


def test_the_compiler_is_the_connections() -> None:
    native = FakeNative()
    conn = make(native)
    assert transaction(conn, native).compiler is COMPILER


def test_a_transaction_must_be_active_to_run_queries() -> None:
    native = FakeNative()
    tx = transaction(make(native), native)
    with pytest.raises(InterfaceError, match="the transaction has not started; use it with `async with`"):
        run(tx._execute("x", []))
    assert tx.closed


def test_queries_run_on_the_connection_and_the_transaction_ends_once() -> None:
    native = FakeNative()
    conn = make(native)

    async def body() -> Tx:
        async with transaction(conn, native) as tx:
            assert await tx._execute("update t", []) == 3
            fetched = await tx._fetch("select 1", [], None, False, False, None, None)  # ruff: ignore[boolean-positional-value-in-call]
            assert fetched == [1]
            stream = tx._stream("select 1", [], None, False, None, 1, None)  # ruff: ignore[boolean-positional-value-in-call]
            assert stream is not native.stream
            assert not tx.closed
        return tx

    tx = run(body())
    assert tx.closed
    assert conn._tx is None
    with pytest.raises(InterfaceError, match="the transaction has ended"):
        run(tx._execute("x", []))
    with pytest.raises(InterfaceError, match="a transaction can only be entered once"):
        run(enter_and_exit(tx))


def test_an_exception_rolls_back() -> None:
    native = FakeNative()
    conn = make(native)

    async def body() -> None:
        async with transaction(conn, native):
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        run(body())
    assert native.log == ["begin ", "rollback"]


def test_a_failed_rollback_is_noted_on_the_body_exception() -> None:
    native = FakeNative()
    conn = make(native)

    async def body() -> None:
        async with transaction(conn, native):
            native.end_failure = InterfaceError("dead")
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom") as info:
        run(body())
    assert info.value.__notes__ == ["the rollback also failed: dead"]


def test_a_busy_connection_leaves_the_transaction_open() -> None:
    native = FakeNative()
    conn = make(native)
    tx = transaction(conn, native)
    enter(tx)
    native.end_failure = ConnectionBusyError("busy")
    with pytest.raises(ConnectionBusyError):
        run(tx.rollback())
    native.end_failure = None
    run(tx.rollback())
    assert native.log == ["begin ", "rollback"]


def test_a_swallowed_failure_is_rolled_back_instead_of_committed() -> None:
    native = FakeNative()
    conn = make(native)

    async def body() -> None:
        async with transaction(conn, native):
            native.failure = RuntimeError("boom")
            with pytest.raises(RuntimeError, match="boom"):
                await conn._execute("x", [])

    with pytest.raises(InterfaceError, match="an error inside the transaction was swallowed"):
        run(body())
    assert native.log == ["begin ", "rollback"]


def test_commit_after_a_swallowed_failure_rolls_back() -> None:
    native = FakeNative()
    conn = make(native)
    tx = transaction(conn, native)
    enter(tx)
    tx._failed = True
    with pytest.raises(InterfaceError, match="an error inside the transaction was swallowed"):
        run(tx.commit())
    assert native.log == ["begin ", "rollback"]


def test_savepoints_nest_and_release() -> None:
    native = FakeNative()
    conn = make(native)

    async def body() -> None:
        async with transaction(conn, native) as outer:
            async with transaction(conn, native, parent=outer) as inner:
                assert conn._tx is inner
                with pytest.raises(InterfaceError, match="end the open savepoint first"):
                    await outer.commit()
                with pytest.raises(InterfaceError, match="a savepoint is already open in this transaction"):
                    await enter_and_exit(transaction(conn, native, parent=outer))
            assert conn._tx is outer

    run(body())
    assert native.log == ["begin ", "savepoint 1", "release 1", "commit"]


def test_a_rolled_back_savepoint_is_released_after() -> None:
    native = FakeNative()
    conn = make(native)

    async def body() -> None:
        async with transaction(conn, native) as outer:
            inner = transaction(conn, native, parent=outer)
            await AsyncExitStack().enter_async_context(inner)
            await inner.rollback()

    run(body())
    assert native.log == ["begin ", "savepoint 1", "rollback to 1", "release 1", "commit"]


def test_ending_with_a_savepoint_open_rolls_back_and_raises() -> None:
    native = FakeNative()
    conn = make(native)

    async def body() -> None:
        async with transaction(conn, native) as outer:
            await AsyncExitStack().enter_async_context(transaction(conn, native, parent=outer))

    with pytest.raises(InterfaceError, match="the transaction ended while a savepoint was still open"):
        run(body())
    assert native.log == ["begin ", "savepoint 1", "rollback"]


def test_a_begin_cancelled_after_it_opened_the_transaction_rolls_it_back() -> None:
    native = FakeNative()
    conn = make(native)

    # the native side handed the transaction over (the depth rose), then the awaiting call was cancelled
    async def begin(impl: FakeNative) -> None:  # ruff: ignore[unused-async]
        impl._txn_depth += 1
        impl.log.append("begin")
        raise asyncio.CancelledError

    tx = BaseTransaction(conn, begin)
    with pytest.raises(asyncio.CancelledError):
        run(enter_and_exit(tx))
    assert native.log == ["begin", "rollback"]
    assert native._txn_depth == 0
    assert conn._tx is None


def test_a_begin_cancelled_before_it_opened_the_transaction_ends_nothing() -> None:
    native = FakeNative()
    conn = make(native)

    async def begin(impl: FakeNative) -> None:  # ruff: ignore[unused-async]
        impl.log.append("begin")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(enter_and_exit(BaseTransaction(conn, begin)))
    assert native.log == ["begin"]


def test_a_failed_undo_is_noted_on_the_cancellation() -> None:
    native = FakeNative()
    conn = make(native)

    async def begin(impl: FakeNative) -> None:  # ruff: ignore[unused-async]
        impl._txn_depth += 1
        impl.end_failure = InterfaceError("the connection is closed")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError) as caught:
        run(enter_and_exit(BaseTransaction(conn, begin)))
    assert caught.value.__notes__ == [
        "undoing the transaction it opened also failed: the connection is closed",
    ]


def test_a_begin_closed_after_it_opened_the_transaction_still_rolls_it_back() -> None:
    native = FakeNative()
    conn = make(native)

    async def begin(impl: FakeNative) -> None:
        impl._txn_depth += 1
        impl.log.append("begin")
        await asyncio.get_running_loop().create_future()

    async def body() -> None:
        entering = enter_and_exit(BaseTransaction(conn, begin))
        entering.send(None)
        # closing the coroutine throws GeneratorExit at the await; it cannot await the undo, which runs on
        entering.close()
        await asyncio.gather(*(task for task in asyncio.all_tasks() if task is not asyncio.current_task()))

    run(body())
    assert native.log == ["begin", "rollback"]


def test_an_undo_that_fails_after_a_second_cancellation_is_retrieved() -> None:
    native = FakeNative()
    conn = make(native)
    unretrieved: list[dict[str, object]] = []

    async def begin(impl: FakeNative) -> None:  # ruff: ignore[unused-async]
        impl._txn_depth += 1
        raise asyncio.CancelledError

    async def body() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
        started = asyncio.Event()
        release = asyncio.Event()

        async def end() -> None:
            started.set()
            await release.wait()
            msg = "the connection is closed"
            raise InterfaceError(msg)

        native._end_transaction = lambda _commit: end()  # ty: ignore[invalid-assignment]
        entering = asyncio.create_task(enter_and_exit(BaseTransaction(conn, begin)))
        await started.wait()
        # the second cancellation stops the wait on the shielded undo, which then fails with nobody awaiting it
        entering.cancel()
        with pytest.raises(asyncio.CancelledError):
            await entering
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        gc.collect()

    run(body())
    assert unretrieved == []


def test_a_savepoint_cancelled_after_it_opened_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    native = FakeNative()
    conn = make(native)
    opened = native._savepoint

    async def savepoint() -> int:
        await opened()
        raise asyncio.CancelledError

    async def body() -> None:
        async with transaction(conn, native) as outer:
            # the test's begin leaves the depth alone; the outermost transaction counts as one
            native._txn_depth = 1
            monkeypatch.setattr(native, "_savepoint", savepoint)
            with pytest.raises(asyncio.CancelledError):
                await enter_and_exit(transaction(conn, native, parent=outer))
            assert outer._child is None
            assert conn._tx is outer

    run(body())
    assert native.log == ["begin ", "savepoint 1", "release 2", "commit"]
