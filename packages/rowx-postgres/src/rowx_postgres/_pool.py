import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Final, Self

from rowx_core._pool import (  # ruff: ignore[import-private-name]
    BaseAcquire,
    BasePooledStream,
    BasePoolTransaction,
    StreamArgs as BaseStreamArgs,
)
from rowx_core.errors import InterfaceError

from rowx_postgres._compiler import COMPILER
from rowx_postgres._connection import RESET_QUERY, Connection
from rowx_postgres._core import Pool as PoolImpl
from rowx_postgres._options import ConnectOptions
from rowx_postgres._shape import native_shape
from rowx_postgres._transaction import Transaction

if TYPE_CHECKING:
    from types import TracebackType

    from rowx_core import Compiler, ShapeSpec

    from rowx_postgres._core import Connection as ConnectionImpl, RowShape, RowStream as RowStreamImpl

type Hook = Callable[[Connection], Awaitable[None]]
type StreamArgs = BaseStreamArgs[RowShape]

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


class Acquire(BaseAcquire[Connection]):
    pass


class PoolTransaction(BasePoolTransaction[Connection, Transaction]):
    def __init__(self, pool: Pool, /, *, isolation: str | None, readonly: bool, deferrable: bool) -> None:
        super().__init__(
            pool,
            lambda conn: conn.transaction(isolation=isolation, readonly=readonly, deferrable=deferrable),
        )


class PooledStream(BasePooledStream[Connection]):
    pass


class Pool:
    @property
    def compiler(self) -> Compiler:
        return COMPILER

    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        options: str | ConnectOptions,
        /,
        *,
        min_size: int = 0,
        max_size: int = DEFAULT_MAX_SIZE,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
        idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT,
        max_lifetime: float | None = DEFAULT_MAX_LIFETIME,
        max_queries: int | None = None,
        ping_after: float | None = DEFAULT_PING_AFTER,
        init: Hook | None = None,
        setup: Hook | None = None,
        reset: Hook | None = None,
        reset_query: str | None = RESET_QUERY,
    ) -> None:
        validate(
            min_size=min_size,
            max_size=max_size,
            acquire_timeout=acquire_timeout,
            idle_timeout=idle_timeout,
            max_lifetime=max_lifetime,
            max_queries=max_queries,
            ping_after=ping_after,
        )
        resolved = ConnectOptions.from_dsn(options) if isinstance(options, str) else options
        self._init = init
        self._setup = setup
        self._reset = reset
        self._reset_query = reset_query
        # with any hook set the release is on the caller's path, because the hooks need its loop
        self._hooked = any(hook is not None for hook in (init, setup, reset))
        self._tasks: set[asyncio.Task[None]] = set()
        self._acquire_timeout = acquire_timeout

        def build(target: ConnectOptions) -> PoolImpl:
            return PoolImpl(
                target,
                min_size=min_size,
                max_size=max_size,
                acquire_timeout=acquire_timeout,
                idle_timeout=idle_timeout,
                max_lifetime=max_lifetime,
                max_queries=max_queries,
                ping_after=ping_after,
                reset_query=reset_query,
            )

        # Protected hooks for subclasses in other distributions (`rowx_embedded.Database`), which build the native pool
        # after construction: `_build` makes a native pool for the given options, `_native` is the pool the constructor
        # makes (`None` defers it), `_inner` holds the built pool, `_started` returns it (starting it first when it
        # does not exist yet, within `limit` seconds), and `_via_python` sends every query through a checked-out
        # connection (as hooks do) rather than straight to the native pool.
        self._build: Callable[[ConnectOptions], PoolImpl] = build
        self._inner: PoolImpl | None = self._native(resolved)
        self._via_python = self._hooked

    def _native(self, options: ConnectOptions, /) -> PoolImpl | None:
        return self._build(options)

    @property
    def _impl(self) -> PoolImpl:
        if self._inner is None:
            msg = "the pool has not started"
            raise InterfaceError(msg)
        return self._inner

    async def _started(self, limit: float | None = None) -> PoolImpl:  # ruff: ignore[unused-method-argument]
        return self._impl

    async def open(self) -> None:
        await (await self._started(self._acquire_timeout)).open()

    async def close(self, *, timeout: float | None = None) -> None:  # ruff: ignore[async-function-with-timeout]
        await self._impl.close(timeout)

    def terminate(self) -> None:
        self._impl.terminate()

    def expire_connections(self) -> None:
        self._impl.expire_connections()

    def acquire(self, *, timeout: float | None = None) -> Acquire:
        return Acquire(self, timeout)

    def transaction(
        self,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> PoolTransaction:
        return PoolTransaction(self, isolation=isolation, readonly=readonly, deferrable=deferrable)

    async def release(self, conn: Connection, /) -> None:
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

    async def _run_reset(self, impl: ConnectionImpl) -> bool:
        # a task that still runs on the connection is cancelled and settled first, so the reset
        # query and then the hook see an idle connection
        await impl._settle()  # ruff: ignore[private-member-access]
        # with no reset query this still ends an open transaction, so the hook sees an idle connection
        await impl.reset(self._reset_query)
        done = self._reset_query is not None
        if self._reset is not None:
            await self._call(self._reset, impl)
        return done

    @staticmethod
    async def _call(hook: Hook, impl: ConnectionImpl) -> None:
        view = Connection(impl)
        try:
            await hook(view)
        finally:
            # the connection a hook receives is invalid once it returns
            view._inner = None  # ruff: ignore[private-member-access]

    async def _checkout(self, limit: float | None) -> Connection:
        impl = await (await self._started(self._acquire_timeout if limit is None else limit)).acquire(limit)
        conn = Connection(impl, pool=self)
        if not self._hooked:
            return conn
        try:
            await self._prepare(impl)
        except BaseException:
            conn._take()  # ruff: ignore[private-member-access]
            await self._impl.discard(impl)
            raise
        return conn

    async def _prepare(self, impl: ConnectionImpl) -> None:
        if impl.init_pending:
            if self._init is not None:
                await self._call(self._init, impl)
            impl._clear_init_pending()  # ruff: ignore[private-member-access]
        if self._setup is not None:
            await self._call(self._setup, impl)

    async def _finish(self, conn: Connection) -> None:
        # a block that already released, closed or detached its connection has nothing left to give back
        if conn._released or conn._pool is not self:  # ruff: ignore[private-member-access]
            return
        if self._hooked:
            await self.release(conn)
            return
        # off the caller's path: the lease is revoked now, the reset runs in the background
        self._impl._release_nowait(conn._take())  # ruff: ignore[private-member-access]

    def _release_soon(self, conn: Connection) -> None:
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
        shape: ShapeSpec | RowShape | None,
    ) -> list[object]:
        if self._via_python:
            async with self.acquire() as conn:
                return await conn._fetch(sql, params, mask, scalar, limit_one, arity, shape)  # ruff: ignore[private-member-access]
        return await self._impl._fetch(sql, params, mask, scalar, limit_one, arity, native_shape(shape))  # ruff: ignore[private-member-access]

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
        shape: ShapeSpec | RowShape | None,
    ) -> RowStreamImpl | PooledStream:
        if self._via_python:
            return PooledStream(self, (sql, params, mask, scalar, arity, batch_size, shape))
        return self._impl._stream(sql, params, mask, scalar, arity, batch_size, native_shape(shape))  # ruff: ignore[private-member-access]

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
