from typing import TYPE_CHECKING, Protocol, Self

from rowx_core._transaction import TrackedStream
from rowx_core.errors import ConnectionBusyError, InterfaceError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from types import TracebackType
    from typing import Any

    from rowx_core._executor import StreamHandle
    from rowx_core._rowplan import ShapeSpec
    from rowx_core._template import Compiler
    from rowx_core._transaction import BaseTransaction


# What the shared glue calls on a backend's native connection; `S` is the native row shape
class NativeConnection[S](Protocol):
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
    async def ping(self) -> None: ...
    async def close(self) -> None: ...
    def detach(self) -> None: ...
    async def _savepoint(self) -> int: ...
    async def _release_savepoint(self, n: int) -> object: ...
    async def _rollback_to_savepoint(self, n: int) -> object: ...
    def _end_transaction(self, commit: bool) -> Awaitable[None]: ...  # ruff: ignore[boolean-type-hint-positional-argument]
    @property
    def closed(self) -> bool: ...


# What a pooled connection gives itself back to
class ConnectionPool[C](Protocol):
    async def release(self, conn: C, /) -> None: ...


# `native_shape` turns a `ShapeSpec` into the backend's own row shape and `compiler` is the backend's compiler
class BaseConnection[I: NativeConnection[Any], S]:
    @property
    def compiler(self) -> Compiler:
        return self._compiler

    def __init__(
        self,
        impl: I,
        /,
        *,
        compiler: Compiler,
        native_shape: Callable[[ShapeSpec | S | None], S | None],
        pool: ConnectionPool[Self] | None = None,
    ) -> None:
        self._inner: I | None = impl
        self._compiler = compiler
        self._native_shape = native_shape
        # set while the connection is checked out of a pool
        self._pool = pool
        # the innermost open transaction; failures of statements run on this connection mark it failed
        self._tx: BaseTransaction[I, S] | None = None

    @property
    def _impl(self) -> I:
        if self._inner is None:
            msg = "the connection was released to the pool; acquire another one"
            raise InterfaceError(msg)
        return self._inner

    @property
    def _released(self) -> bool:
        return self._inner is None

    def _take(self) -> I:
        # invalidates this object, so nothing can use the connection after it is handed on
        impl = self._impl
        self._inner = None
        self._pool = None
        return impl

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
        try:
            return await self._impl._fetch(sql, params, mask, scalar, limit_one, arity, self._native_shape(shape))  # ruff: ignore[private-member-access]
        except ConnectionBusyError:
            # nothing ran, so a transaction is untouched
            raise
        except BaseException:
            self._note_failure()
            raise

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
        stream = self._impl._stream(sql, params, mask, scalar, arity, batch_size, self._native_shape(shape))  # ruff: ignore[private-member-access]
        # a plain connection keeps the bare stream, so only a transaction pays for the wrapper
        return TrackedStream(stream, self._note_failure) if self._tx is not None else stream

    async def _execute(self, sql: str, params: list[object]) -> int:
        try:
            return await self._impl._execute(sql, params)  # ruff: ignore[private-member-access]
        except ConnectionBusyError:
            raise
        except BaseException:
            self._note_failure()
            raise

    def _note_failure(self) -> None:
        if self._tx is not None:
            self._tx._failed = True  # ruff: ignore[private-member-access]

    async def ping(self) -> None:
        await self._impl.ping()

    def detach(self) -> None:
        self._impl.detach()
        self._pool = None

    async def close(self) -> None:
        # closing a pooled connection gives it back
        if self._pool is not None:
            await self._pool.release(self)
            return
        await self._impl.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def closed(self) -> bool:
        return self._inner is None or self._inner.closed
