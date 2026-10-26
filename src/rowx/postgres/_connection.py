from typing import TYPE_CHECKING, Final, Self

from rowx._core.postgres import connect as connect_impl
from rowx.errors import InterfaceError
from rowx.postgres._options import ConnectOptions
from rowx.postgres._transaction import Transaction

RESET_QUERY: Final[str] = "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;"

if TYPE_CHECKING:
    from types import TracebackType

    from rowx._core.postgres import Connection as ConnectionImpl, RowStream as RowStreamImpl
    from rowx.postgres._pool import Pool


class Connection:
    def __init__(self, impl: ConnectionImpl, /, *, pool: Pool | None = None) -> None:
        self._inner: ConnectionImpl | None = impl
        # set while the connection is checked out of a pool
        self._pool = pool

    @property
    def _impl(self) -> ConnectionImpl:
        if self._inner is None:
            msg = "the connection was released to the pool; acquire another one"
            raise InterfaceError(msg)
        return self._inner

    @property
    def _released(self) -> bool:
        return self._inner is None

    def _take(self) -> ConnectionImpl:
        # invalidates this object, so nothing can use the connection after it is handed on
        impl = self._impl
        self._inner = None
        self._pool = None
        return impl

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

    async def ping(self) -> None:
        await self._impl.ping()

    async def reset(self) -> None:
        await self._impl.reset(RESET_QUERY)

    def transaction(
        self,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> Transaction:
        return Transaction(self, isolation=isolation, readonly=readonly, deferrable=deferrable)

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


async def connect(dsn_or_options: str | ConnectOptions, /) -> Connection:
    options = ConnectOptions.from_dsn(dsn_or_options) if isinstance(dsn_or_options, str) else dsn_or_options
    return Connection(await connect_impl(options))
