from typing import TYPE_CHECKING, Self

from rowx._core.postgres import connect as connect_impl

if TYPE_CHECKING:
    from types import TracebackType

    from rowx._core.postgres import Connection as ConnectionImpl, RowStream as RowStreamImpl


class Connection:
    def __init__(self, impl: ConnectionImpl, /) -> None:
        self._impl = impl

    async def _fetch(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: list[bool] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
    ) -> list[object]:
        return await self._impl._fetch(sql, params, mask, scalar, limit_one, arity)  # ruff: ignore[private-member-access]

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: list[bool] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
    ) -> RowStreamImpl:
        return self._impl._stream(sql, params, mask, scalar, arity, batch_size)  # ruff: ignore[private-member-access]

    async def _execute(self, sql: str, params: list[object]) -> int:
        return await self._impl._execute(sql, params)  # ruff: ignore[private-member-access]

    async def close(self) -> None:
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
        return self._impl.closed


async def connect(dsn: str, /, *, connect_timeout: float | None = None, statement_cache_size: int = 256) -> Connection:
    impl = await connect_impl(dsn, connect_timeout=connect_timeout, statement_cache_size=statement_cache_size)
    return Connection(impl)
