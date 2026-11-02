from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rowx._core.postgres import RowShape


class StreamHandle(Protocol):
    async def _next(self) -> list[object]: ...
    def close(self) -> None: ...
    @property
    def rows_affected(self) -> int | None: ...


class Executor(Protocol):
    async def _fetch(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        shape: RowShape | None,
    ) -> list[object]: ...

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
        shape: RowShape | None,
    ) -> StreamHandle: ...

    async def _execute(self, sql: str, params: list[object]) -> int: ...

    @property
    def closed(self) -> bool: ...
