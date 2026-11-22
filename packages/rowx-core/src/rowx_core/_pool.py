import asyncio
from typing import TYPE_CHECKING, Any, Final, Protocol, Self

from rowx_core.errors import InterfaceError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator, Sequence
    from contextlib import AbstractAsyncContextManager
    from types import TracebackType

    from rowx_core._connection import BaseConnection
    from rowx_core._executor import StreamHandle
    from rowx_core._rowplan import ShapeSpec

type StreamArgs[S] = tuple[str, list[object], Sequence[int] | None, bool, int | None, int, ShapeSpec | S | None]
type Hook[C] = Callable[[C], Awaitable[None]]

DEFAULT_MAX_SIZE: Final[int] = 10
DEFAULT_ACQUIRE_TIMEOUT: Final[float] = 30.0
DEFAULT_IDLE_TIMEOUT: Final[float] = 600.0
DEFAULT_MAX_LIFETIME: Final[float] = 1800.0
DEFAULT_PING_AFTER: Final[float] = 30.0


def validate(  # ruff: ignore[too-many-arguments]
    *,
    min_size: int,
    max_size: int,
    acquire_timeout: float,
    idle_timeout: float | None,
    max_lifetime: float | None,
    max_queries: int | None,
    ping_after: float | None,
) -> None:
    if max_size < 1:
        msg = "max_size must be at least 1"
        raise ValueError(msg)
    if not 0 <= min_size <= max_size:
        msg = "min_size must be between 0 and max_size"
        raise ValueError(msg)
    timeouts = {
        "acquire_timeout": acquire_timeout,
        "idle_timeout": idle_timeout,
        "max_lifetime": max_lifetime,
        "ping_after": ping_after,
    }
    for name, value in timeouts.items():
        if value is not None and value < 0:
            msg = f"{name} must not be negative"
            raise ValueError(msg)
    if max_queries is not None and max_queries < 1:
        msg = "max_queries must be at least 1"
        raise ValueError(msg)


# The pool methods that the shared helpers call; `C` is the backend's connection class
class PoolMethods[C](Protocol):
    async def _checkout(self, limit: float | None) -> C: ...
    async def _finish(self, conn: C) -> None: ...
    def _release_soon(self, conn: C) -> None: ...


# The connection method a pooled stream calls once it has a connection
class StreamOpener[S](Protocol):
    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
        shape: ShapeSpec | S | None,
    ) -> StreamHandle: ...


class BaseAcquire[C]:
    def __init__(self, pool: PoolMethods[C], timeout: float | None, /) -> None:
        self._pool = pool
        self._timeout = timeout
        self._conn: C | None = None

    def __await__(self) -> Generator[Any, Any, C]:
        return self._pool._checkout(self._timeout).__await__()  # ruff: ignore[private-member-access]

    async def __aenter__(self) -> C:
        self._conn = await self._pool._checkout(self._timeout)  # ruff: ignore[private-member-access]
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await self._pool._finish(conn)  # ruff: ignore[private-member-access]


# `begin` opens the transaction on the checked-out connection with the backend's own arguments
class BasePoolTransaction[C, T: AbstractAsyncContextManager[Any]]:
    def __init__(self, pool: PoolMethods[C], begin: Callable[[C], T], /) -> None:
        self._pool = pool
        self._begin = begin
        self._entered = False
        self._conn: C | None = None
        self._tx: T | None = None

    async def __aenter__(self) -> T:
        # set before any await, so a concurrent or repeated entry can't check out a second connection
        if self._entered:
            msg = "a transaction can only be entered once"
            raise InterfaceError(msg)
        self._entered = True
        conn = await self._pool._checkout(None)  # ruff: ignore[private-member-access]
        tx = self._begin(conn)
        try:
            await tx.__aenter__()
        except BaseException:
            # a failed or cancelled BEGIN must not leak the checkout; release resets the connection
            await self._pool._finish(conn)  # ruff: ignore[private-member-access]
            raise
        self._conn = conn
        self._tx = tx
        return tx

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        conn, tx = self._conn, self._tx
        self._conn = self._tx = None
        if conn is None or tx is None:
            return
        try:
            await tx.__aexit__(exc_type, exc, tb)
        finally:
            # the connection goes back even when the ending statement raised
            await self._pool._finish(conn)  # ruff: ignore[private-member-access]


class BasePooledStream[C: StreamOpener[Any]]:
    # a stream on a pool that has hooks: the hooks need the caller's loop, so Python acquires
    def __init__(self, pool: PoolMethods[C], args: StreamArgs[Any], /) -> None:
        self._pool = pool
        self._args = args
        self._conn: C | None = None
        self._inner: StreamHandle | None = None
        self._closed = False

    async def _next(self) -> list[object]:
        if self._closed:
            return []
        if self._inner is None:
            self._conn = await self._pool._checkout(None)  # ruff: ignore[private-member-access]
            if self._stopped():
                await self._give_back()
                return []
            self._inner = self._conn._stream(*self._args)  # ruff: ignore[private-member-access]
        try:
            batch = await self._inner._next()  # ruff: ignore[private-member-access]
        except BaseException:
            self._closed = True
            await self._give_back()
            raise
        if not batch:
            self._closed = True
            await self._give_back()
        return batch

    def _stopped(self) -> bool:
        # close() may run while the first _next is still waiting for a connection
        return self._closed

    def close(self) -> None:
        self._closed = True
        if self._inner is not None:
            self._inner.close()
        if self._conn is not None:
            conn, self._conn = self._conn, None
            self._pool._release_soon(conn)  # ruff: ignore[private-member-access]

    @property
    def rows_affected(self) -> int | None:
        return None if self._inner is None else self._inner.rows_affected

    async def _give_back(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await self._pool._finish(conn)  # ruff: ignore[private-member-access]


# What the pool calls on a native connection it checked out
class PooledNative(Protocol):
    @property
    def init_pending(self) -> bool: ...
    async def _settle(self) -> None: ...
    def _clear_init_pending(self) -> None: ...


# What the pool calls on the backend's native pool; `N` is the native connection and `S` the native row shape
class NativePool[N: PooledNative, S](Protocol):
    async def open(self) -> None: ...
    async def close(self, limit: float | None = None) -> None: ...
    def terminate(self) -> None: ...
    def expire_connections(self) -> None: ...
    async def acquire(self, limit: float | None = None) -> N: ...
    async def release(self, conn: N, reset_done: bool = False) -> None: ...  # ruff: ignore[boolean-type-hint-positional-argument, boolean-default-value-positional-argument]
    def _release_nowait(self, conn: N) -> None: ...
    async def discard(self, conn: N) -> None: ...
    async def _fetch(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        shape: S | None,
    ) -> list[object]: ...
    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
        shape: S | None,
    ) -> StreamHandle: ...
    async def _execute(self, sql: str, params: list[object]) -> int: ...
    @property
    def size(self) -> int: ...
    @property
    def idle(self) -> int: ...
    @property
    def waiting(self) -> int: ...
    @property
    def min_size(self) -> int: ...
    @property
    def max_size(self) -> int: ...
    @property
    def closed(self) -> bool: ...


# `C` is the backend's connection class, `N` its native connection, `S` its native row shape, `P` its native pool and `O`
# its connect options. A subclass supplies `_wrap`, `_native_shape` and `_reset_native`, and overrides `acquire` and
# `_pooled_stream` where it has its own classes.
# Protected hooks for subclasses in other distributions (`rowx_embedded.Database`), which build the native pool after
# construction: `_build` makes a native pool for the given options, `_native` is the pool the constructor makes (`None`
# defers it), `_inner` holds the built pool, `_started` returns it (starting it first when it does not exist yet, within
# `limit` seconds), and `_via_python` sends every query through a checked-out connection (as hooks do) rather than
# straight to the native pool.
class BasePool[C: BaseConnection[Any, Any], N: PooledNative, S, P: NativePool[Any, Any], O]:
    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        resolved: O,
        build: Callable[[O], P],
        /,
        *,
        acquire_timeout: float,
        init: Hook[C] | None = None,
        setup: Hook[C] | None = None,
        reset: Hook[C] | None = None,
    ) -> None:
        self._init = init
        self._setup = setup
        self._reset = reset
        # with any hook set the release is on the caller's path, because the hooks need its loop
        self._hooked = any(hook is not None for hook in (init, setup, reset))
        self._tasks: set[asyncio.Task[None]] = set()
        self._acquire_timeout = acquire_timeout
        self._build: Callable[[O], P] = build
        self._inner: P | None = self._native(resolved)
        self._via_python = self._hooked

    def _wrap(self, impl: N, /, *, pool: Self | None) -> C:
        raise NotImplementedError

    def _native_shape(self, shape: ShapeSpec | S | None, /) -> S | None:
        raise NotImplementedError

    # makes the native connection idle again, ending an open transaction, and returns whether the backend's own reset
    # ran, which lets the native pool skip its own
    async def _reset_native(self, impl: N, /) -> bool:
        raise NotImplementedError

    def acquire(self, *, timeout: float | None = None) -> BaseAcquire[C]:
        return BaseAcquire(self, timeout)

    def _pooled_stream(self, args: StreamArgs[S], /) -> StreamHandle:
        return BasePooledStream(self, args)

    def _native(self, options: O, /) -> P | None:
        return self._build(options)

    @property
    def _impl(self) -> P:
        if self._inner is None:
            msg = "the pool has not started"
            raise InterfaceError(msg)
        return self._inner

    async def _started(self, limit: float | None = None) -> P:  # ruff: ignore[unused-method-argument]
        return self._impl

    async def open(self) -> None:
        await (await self._started(self._acquire_timeout)).open()

    async def close(self, *, timeout: float | None = None) -> None:  # ruff: ignore[async-function-with-timeout]
        await self._impl.close(timeout)

    def terminate(self) -> None:
        self._impl.terminate()

    def expire_connections(self) -> None:
        self._impl.expire_connections()

    async def release(self, conn: C, /) -> None:
        if conn._pool is not self:  # ruff: ignore[private-member-access]
            msg = "the connection is not checked out of this pool"
            raise InterfaceError(msg)
        impl = conn._take()  # ruff: ignore[private-member-access]
        reset_done = False
        if self._hooked:
            try:
                reset_done = await self._run_reset(impl)
            except BaseException:
                await self._impl.discard(impl)
                raise
        await self._impl.release(impl, reset_done)

    async def _run_reset(self, impl: N, /) -> bool:
        # a task that still runs on the connection is cancelled and settled first, so the reset
        # and then the hook see an idle connection
        await impl._settle()  # ruff: ignore[private-member-access]
        done = await self._reset_native(impl)
        if self._reset is not None:
            await self._call(self._reset, impl)
        return done

    async def _call(self, hook: Hook[C], impl: N, /) -> None:
        view = self._wrap(impl, pool=None)
        try:
            await hook(view)
        finally:
            # the connection a hook receives is invalid once it returns
            view._inner = None  # ruff: ignore[private-member-access]

    async def _checkout(self, limit: float | None) -> C:
        impl = await (await self._started(self._acquire_timeout if limit is None else limit)).acquire(limit)
        conn = self._wrap(impl, pool=self)
        if not self._hooked:
            return conn
        try:
            await self._prepare(impl)
        except BaseException:
            conn._take()  # ruff: ignore[private-member-access]
            await self._impl.discard(impl)
            raise
        return conn

    async def _prepare(self, impl: N, /) -> None:
        if impl.init_pending:
            if self._init is not None:
                await self._call(self._init, impl)
            impl._clear_init_pending()  # ruff: ignore[private-member-access]
        if self._setup is not None:
            await self._call(self._setup, impl)

    async def _finish(self, conn: C) -> None:
        # a block that already released, closed or detached its connection has nothing left to give back
        if conn._released or conn._pool is not self:  # ruff: ignore[private-member-access]
            return
        if self._hooked:
            await self.release(conn)
            return
        # off the caller's path: the lease is revoked now, the reset runs in the background
        self._impl._release_nowait(conn._take())  # ruff: ignore[private-member-access]

    def _release_soon(self, conn: C) -> None:
        if not self._hooked:
            self._impl._release_nowait(conn._take())  # ruff: ignore[private-member-access]
            return
        try:
            task = asyncio.get_running_loop().create_task(self._finish(conn))
        except RuntimeError:
            # no loop to run the hooks on
            self._impl._release_nowait(conn._take())  # ruff: ignore[private-member-access]
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fetch(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        shape: ShapeSpec | S | None,
    ) -> list[object]:
        if self._via_python:
            async with self.acquire() as conn:
                return await conn._fetch(sql, params, mask, scalar, limit_one, arity, shape)  # ruff: ignore[private-member-access]
        return await self._impl._fetch(sql, params, mask, scalar, limit_one, arity, self._native_shape(shape))  # ruff: ignore[private-member-access]

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
        shape: ShapeSpec | S | None,
    ) -> StreamHandle:
        if self._via_python:
            return self._pooled_stream((sql, params, mask, scalar, arity, batch_size, shape))
        return self._impl._stream(sql, params, mask, scalar, arity, batch_size, self._native_shape(shape))  # ruff: ignore[private-member-access]

    async def _execute(self, sql: str, params: list[object]) -> int:
        if self._via_python:
            async with self.acquire() as conn:
                return await conn._execute(sql, params)  # ruff: ignore[private-member-access]
        return await self._impl._execute(sql, params)  # ruff: ignore[private-member-access]

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def size(self) -> int:
        return self._impl.size

    @property
    def idle(self) -> int:
        return self._impl.idle

    @property
    def waiting(self) -> int:
        return self._impl.waiting

    @property
    def min_size(self) -> int:
        return self._impl.min_size

    @property
    def max_size(self) -> int:
        return self._impl.max_size

    @property
    def closed(self) -> bool:
        return self._impl.closed
