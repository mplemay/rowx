from typing import TYPE_CHECKING, Any, Protocol

from rowx_core.errors import InterfaceError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from contextlib import AbstractAsyncContextManager
    from types import TracebackType

    from rowx_core._executor import StreamHandle
    from rowx_core._rowplan import ShapeSpec

type StreamArgs[S] = tuple[str, list[object], Sequence[int] | None, bool, int | None, int, ShapeSpec | S | None]


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
