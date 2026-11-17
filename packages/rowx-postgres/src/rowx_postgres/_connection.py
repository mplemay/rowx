from typing import TYPE_CHECKING, Final

from rowx_core._connection import BaseConnection  # ruff: ignore[import-private-name]

from rowx_postgres._compiler import COMPILER
from rowx_postgres._core import Connection as ConnectionImpl, RowShape, connect as connect_impl
from rowx_postgres._options import ConnectOptions
from rowx_postgres._shape import native_shape
from rowx_postgres._transaction import Transaction

RESET_QUERY: Final[str] = (
    "RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;"
)

if TYPE_CHECKING:
    from rowx_core._connection import ConnectionPool


class Connection(BaseConnection[ConnectionImpl, RowShape]):
    def __init__(self, impl: ConnectionImpl, /, *, pool: ConnectionPool[Connection] | None = None) -> None:
        super().__init__(impl, compiler=COMPILER, native_shape=native_shape, pool=pool)

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


async def connect(dsn_or_options: str | ConnectOptions, /) -> Connection:
    options = ConnectOptions.from_dsn(dsn_or_options) if isinstance(dsn_or_options, str) else dsn_or_options
    return Connection(await connect_impl(options))
