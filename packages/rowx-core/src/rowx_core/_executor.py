from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence
    from contextlib import AbstractAsyncContextManager
    from types import TracebackType

    from rowx_core._rowplan import ShapeSpec
    from rowx_core._template import Compiler


class StreamHandle(Protocol):
    async def _next(self) -> list[object]: ...
    def close(self) -> None: ...
    @property
    def rows_affected(self) -> int | None: ...


class Executor(Protocol):
    @property
    def compiler(self) -> Compiler: ...

    async def _fetch(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        shape: ShapeSpec | None,
    ) -> list[object]: ...

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
        shape: ShapeSpec | None,
    ) -> StreamHandle: ...

    async def _execute(self, sql: str, params: list[object]) -> int: ...

    @property
    def closed(self) -> bool: ...


class Transaction(Executor, Protocol):
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    def transaction(self) -> Transaction: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class Connection(Executor, Protocol):
    async def close(self) -> None: ...
    def transaction(self) -> Transaction: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class Acquire[C: Connection](Protocol):
    def __await__(self) -> Generator[object, None, C]: ...
    async def __aenter__(self) -> C: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class Pool[C: Connection](Executor, Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    def acquire(self, *, timeout: float | None = None) -> Acquire[C]: ...
    def transaction(self) -> AbstractAsyncContextManager[Transaction]: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
