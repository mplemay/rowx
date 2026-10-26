import asyncio
from collections.abc import Awaitable, Callable, Generator
from typing import TYPE_CHECKING, Any, Final, Self

from rowx._core.postgres import Pool as PoolImpl
from rowx.errors import InterfaceError
from rowx.postgres._connection import RESET_QUERY, Connection
from rowx.postgres._options import ConnectOptions

if TYPE_CHECKING:
    from types import TracebackType

    from rowx._core.postgres import Connection as ConnectionImpl, RowStream as RowStreamImpl

type Hook = Callable[[Connection], Awaitable[None]]
type StreamArgs = tuple[str, list[object], list[bool] | None, bool, int | None, int]

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


class Acquire:
    def __init__(self, pool: Pool, timeout: float | None, /) -> None:
        self._pool = pool
        self._timeout = timeout
        self._conn: Connection | None = None

    def __await__(self) -> Generator[Any, Any, Connection]:
        return self._pool._checkout(self._timeout).__await__()  # ruff: ignore[private-member-access]

    async def __aenter__(self) -> Connection:
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


class PooledStream:
    # a stream on a pool that has hooks: the hooks need the caller's loop, so Python acquires
    def __init__(self, pool: Pool, args: StreamArgs, /) -> None:
        self._pool = pool
        self._args = args
        self._conn: Connection | None = None
        self._inner: RowStreamImpl | None = None
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


class Pool:
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
        self._impl = PoolImpl(
            resolved,
            min_size=min_size,
            max_size=max_size,
            acquire_timeout=acquire_timeout,
            idle_timeout=idle_timeout,
            max_lifetime=max_lifetime,
            max_queries=max_queries,
            ping_after=ping_after,
            reset_query=reset_query,
        )

    async def open(self) -> None:
        await self._impl.open()

    async def close(self, *, timeout: float | None = None) -> None:  # ruff: ignore[async-function-with-timeout]
        await self._impl.close(timeout)

    def terminate(self) -> None:
        self._impl.terminate()

    def expire_connections(self) -> None:
        self._impl.expire_connections()

    def acquire(self, *, timeout: float | None = None) -> Acquire:
        return Acquire(self, timeout)

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
        done = False
        if self._reset_query is not None:
            await impl.reset(self._reset_query)
            done = True
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
        impl = await self._impl.acquire(limit)
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
        mask: list[bool] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
    ) -> list[object]:
        if self._hooked:
            async with self.acquire() as conn:
                return await conn._fetch(sql, params, mask, scalar, limit_one, arity)  # ruff: ignore[private-member-access]
        return await self._impl._fetch(sql, params, mask, scalar, limit_one, arity)  # ruff: ignore[private-member-access]

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: list[bool] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
    ) -> RowStreamImpl | PooledStream:
        if self._hooked:
            return PooledStream(self, (sql, params, mask, scalar, arity, batch_size))
        return self._impl._stream(sql, params, mask, scalar, arity, batch_size)  # ruff: ignore[private-member-access]

    async def _execute(self, sql: str, params: list[object]) -> int:
        if self._hooked:
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
