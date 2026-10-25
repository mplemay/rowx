from typing import Protocol


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
        mask: list[bool] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
    ) -> list[object]: ...

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: list[bool] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
    ) -> StreamHandle: ...

    async def _execute(self, sql: str, params: list[object]) -> int: ...

    @property
    def closed(self) -> bool: ...
