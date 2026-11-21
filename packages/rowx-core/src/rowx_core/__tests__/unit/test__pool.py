import asyncio
from typing import TYPE_CHECKING, Final, Self, override

import pytest
from rowx_core.__tests__.support.fake_native import COMPILER, FakeNative, native_shape
from rowx_core._connection import BaseConnection
from rowx_core._pool import BaseAcquire, BasePool, BasePooledStream, BasePoolTransaction, Hook, validate
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


class NativeConn(FakeNative):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.init_pending = True

    async def _settle(self) -> None:
        self.log.append("settle")

    def _clear_init_pending(self) -> None:
        self.init_pending = False


class NativePool:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.count = 0
        self.min_size = 1
        self.max_size = 2
        self.size = 0
        self.idle = 0
        self.waiting = 0
        self.closed = False
        self.last: NativeConn | None = None

    async def open(self) -> None:
        self.events.append("open")

    async def close(self, limit: float | None = None) -> None:
        self.events.append(f"close {limit}")

    def terminate(self) -> None:
        self.events.append("terminate")

    def expire_connections(self) -> None:
        self.events.append("expire")

    async def acquire(self, limit: float | None = None) -> NativeConn:
        self.events.append(f"acquire {limit}")
        self.count += 1
        self.last = NativeConn(f"n{self.count}")
        return self.last

    async def release(self, conn: NativeConn, reset_done: bool = False) -> None:  # ruff: ignore[boolean-type-hint-positional-argument, boolean-default-value-positional-argument]
        self.events.append(f"release {conn.name} {reset_done}")

    def _release_nowait(self, conn: NativeConn) -> None:
        self.events.append(f"nowait {conn.name}")

    async def discard(self, conn: NativeConn) -> None:
        self.events.append(f"discard {conn.name}")

    async def _fetch(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],  # ruff: ignore[unused-method-argument]
        mask: Sequence[int] | None,  # ruff: ignore[unused-method-argument]
        scalar: bool,  # ruff: ignore[unused-method-argument, boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[unused-method-argument, boolean-type-hint-positional-argument]
        arity: int | None,  # ruff: ignore[unused-method-argument]
        shape: object,
    ) -> list[object]:
        self.events.append(f"fetch {sql} {shape}")
        return [1]

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],  # ruff: ignore[unused-method-argument]
        mask: Sequence[int] | None,  # ruff: ignore[unused-method-argument]
        scalar: bool,  # ruff: ignore[unused-method-argument, boolean-type-hint-positional-argument]
        arity: int | None,  # ruff: ignore[unused-method-argument]
        batch_size: int,  # ruff: ignore[unused-method-argument]
        shape: object,  # ruff: ignore[unused-method-argument]
    ) -> FakeStream:
        self.events.append(f"stream {sql}")
        return FakeStream("native", ())

    async def _execute(self, sql: str, params: list[object]) -> int:  # ruff: ignore[unused-method-argument]
        self.events.append(f"execute {sql}")
        return 1


type Conn = BaseConnection[NativeConn, object]
type Impl = BasePool[Conn, NativeConn, object, NativePool, str]


class FakeBackendPool(BasePool[Conn, NativeConn, object, NativePool, str]):
    @override
    def _wrap(self, impl: NativeConn, /, *, pool: Impl | None) -> Conn:
        return BaseConnection(impl, compiler=COMPILER, native_shape=native_shape, pool=pool)

    @override
    def _native_shape(self, shape: object, /) -> object:
        return native_shape(shape)

    @override
    async def _reset_native(self, impl: NativeConn, /) -> bool:
        impl.log.append("reset")
        return True


FETCH_ARGS: Final[tuple[str, list[object], None, bool, bool, None, None]] = ("f", [], None, False, False, None, None)
STREAM_ARGS: Final[tuple[str, list[object], None, bool, None, int, None]] = ("s", [], None, False, None, 1, None)


def recorder(calls: list[str], name: str, failure: BaseException | None = None) -> Hook[Conn]:
    async def hook(conn: Conn) -> None:  # ruff: ignore[unused-async]
        calls.append(name)
        assert not conn._released
        if failure is not None:
            raise failure

    return hook


def make_pool(
    *,
    init: Hook[Conn] | None = None,
    setup: Hook[Conn] | None = None,
    reset: Hook[Conn] | None = None,
) -> tuple[FakeBackendPool, NativePool]:
    native = NativePool()
    opened: list[str] = []

    def build(options: str) -> NativePool:
        opened.append(options)
        return native

    pool = FakeBackendPool("target", build, acquire_timeout=5.0, init=init, setup=setup, reset=reset)
    assert opened == ["target"]
    return pool, native


def check(
    *, min_size: int = 0, max_size: int = 1, idle_timeout: float | None = None, max_queries: int | None = None
) -> None:
    validate(
        min_size=min_size,
        max_size=max_size,
        acquire_timeout=1.0,
        idle_timeout=idle_timeout,
        max_lifetime=None,
        max_queries=max_queries,
        ping_after=None,
    )


def test_validate_rejects_bad_options() -> None:
    check()
    with pytest.raises(ValueError, match="max_size must be at least 1"):
        check(max_size=0)
    with pytest.raises(ValueError, match="min_size must be between 0 and max_size"):
        check(min_size=2)
    with pytest.raises(ValueError, match="idle_timeout must not be negative"):
        check(idle_timeout=-1.0)
    with pytest.raises(ValueError, match="max_queries must be at least 1"):
        check(max_queries=0)


def test_the_native_pool_is_built_at_construction_and_lifecycle_calls_pass_through() -> None:
    pool, native = make_pool()

    async def body() -> None:
        async with pool:
            pass
        await pool.close(timeout=2.0)

    asyncio.run(body())
    pool.terminate()
    pool.expire_connections()
    assert native.events == ["open", "close None", "close 2.0", "terminate", "expire"]
    assert (pool.size, pool.idle, pool.waiting, pool.min_size, pool.max_size, pool.closed) == (0, 0, 0, 1, 2, False)


def test_a_deferred_native_pool_raises_until_it_exists() -> None:
    native = NativePool()

    class Deferred(FakeBackendPool):
        @override
        def _native(self, options: str, /) -> NativePool | None:
            return None

    pool = Deferred("t", lambda options: native, acquire_timeout=1.0)  # ruff: ignore[unused-lambda-argument]
    with pytest.raises(InterfaceError, match="the pool has not started"):
        pool.terminate()
    pool._inner = pool._build("t")
    pool.terminate()
    assert native.events == ["terminate"]


def test_without_hooks_a_lease_is_given_back_through_the_native_pool() -> None:
    pool, native = make_pool()

    async def body() -> None:
        async with BaseAcquire(pool, 3.0) as conn:
            assert conn._pool is pool

    asyncio.run(body())
    assert not pool._hooked
    assert native.events == ["acquire 3.0", "nowait n1"]


def test_release_with_hooks_settles_resets_runs_the_hook_and_reports_the_reset() -> None:
    seen: list[str] = []
    pool, native = make_pool(reset=recorder(seen, "hook"))

    async def body() -> None:
        conn = await pool.acquire()
        impl = conn._impl
        await pool.release(conn)
        assert conn._released
        assert isinstance(impl, NativeConn)
        assert impl.log == ["settle", "reset"]

    asyncio.run(body())
    assert seen == ["hook"]
    assert native.events == ["acquire None", "release n1 True"]


def test_the_view_a_hook_receives_is_invalid_once_it_returns() -> None:
    views: list[Conn] = []

    async def setup(conn: Conn) -> None:  # ruff: ignore[unused-async]
        views.append(conn)

    pool, _ = make_pool(setup=setup)

    async def body() -> None:
        async with pool.acquire():
            pass

    asyncio.run(body())
    assert views[0]._released


def test_init_runs_once_per_native_connection_and_setup_every_checkout() -> None:
    calls: list[str] = []
    pool, native = make_pool(init=recorder(calls, "init"), setup=recorder(calls, "setup"))

    async def body() -> None:
        conn = await pool._checkout(None)
        assert native.last is not None
        assert not native.last.init_pending
        await pool.release(conn)

    asyncio.run(body())
    assert calls == ["init", "setup"]


def test_a_failing_hook_on_checkout_discards_the_connection() -> None:
    pool, native = make_pool(setup=recorder([], "setup", RuntimeError("setup failed")))

    async def body() -> None:
        with pytest.raises(RuntimeError, match="setup failed"):
            await pool._checkout(None)

    asyncio.run(body())
    assert native.events == ["acquire None", "discard n1"]


def test_a_failing_reset_hook_discards_the_connection_and_raises() -> None:
    pool, native = make_pool(reset=recorder([], "reset", RuntimeError("reset failed")))

    async def body() -> None:
        conn = await pool._checkout(None)
        with pytest.raises(RuntimeError, match="reset failed"):
            await pool.release(conn)

    asyncio.run(body())
    assert native.events == ["acquire None", "discard n1"]


def test_releasing_a_connection_of_another_pool_is_refused() -> None:
    pool, _ = make_pool()
    other, _ = make_pool()

    async def body() -> None:
        conn = await other._checkout(None)
        with pytest.raises(InterfaceError, match="not checked out of this pool"):
            await pool.release(conn)

    asyncio.run(body())


def test_finish_ignores_a_connection_that_was_already_given_back() -> None:
    pool, native = make_pool()

    async def body() -> None:
        conn = await pool._checkout(None)
        await pool._finish(conn)
        await pool._finish(conn)

    asyncio.run(body())
    assert native.events == ["acquire None", "nowait n1"]


def test_release_soon_runs_the_hooks_in_the_background_and_without_a_loop_releases_directly() -> None:
    pool, native = make_pool(reset=recorder([], "reset"))

    async def body() -> None:
        pool._release_soon(await pool._checkout(None))
        await asyncio.gather(*pool._tasks)

    asyncio.run(body())
    assert native.events == ["acquire None", "release n1 True"]
    assert not pool._tasks

    bare, bare_native = make_pool(reset=recorder([], "reset"))
    conn = asyncio.run(bare._checkout(None))
    bare._release_soon(conn)
    assert bare_native.events == ["acquire None", "nowait n1"]


def test_queries_go_straight_to_the_native_pool_without_hooks() -> None:
    pool, native = make_pool()

    async def body() -> None:
        assert await pool._fetch(*FETCH_ARGS) == [1]
        assert await pool._execute("e", []) == 1
        pool._stream(*STREAM_ARGS)

    asyncio.run(body())
    assert native.events == ["fetch f None", "execute e", "stream s"]


def test_queries_go_through_a_checked_out_connection_with_hooks() -> None:
    pool, native = make_pool(setup=recorder([], "setup"))
    assert pool._via_python

    async def body() -> None:
        assert await pool._fetch(*FETCH_ARGS) == [1]
        # the result comes from the checked-out connection (`FakeNative`), not from the native pool, which would give 1
        assert await pool._execute("e", []) == FakeNative.EXECUTE_RESULT
        assert isinstance(pool._stream(*STREAM_ARGS), BasePooledStream)

    asyncio.run(body())
    # a stream checks out only on its first batch
    assert native.events == ["acquire None", "release n1 True", "acquire None", "release n2 True"]
