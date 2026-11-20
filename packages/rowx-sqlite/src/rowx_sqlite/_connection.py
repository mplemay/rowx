from typing import TYPE_CHECKING

from rowx_core._connection import BaseConnection  # ruff: ignore[import-private-name]

from rowx_sqlite._compiler import COMPILER
from rowx_sqlite._core import Connection as ConnectionImpl, RowShape, connect as connect_impl
from rowx_sqlite._options import ConnectOptions
from rowx_sqlite._shape import native_shape
from rowx_sqlite._transaction import Transaction

if TYPE_CHECKING:
    from rowx_core._connection import ConnectionPool

    from rowx_sqlite._options import TransactionMode


class Connection(BaseConnection[ConnectionImpl, RowShape]):
    def __init__(self, impl: ConnectionImpl, /, *, pool: ConnectionPool[Connection] | None = None) -> None:
        super().__init__(impl, compiler=COMPILER, native_shape=native_shape, pool=pool)

    def transaction(self, *, mode: TransactionMode | None = None, read_only: bool = False) -> Transaction:
        return Transaction(self, mode=mode, read_only=read_only)


async def connect(dsn_or_options: str | ConnectOptions, /) -> Connection:
    options = ConnectOptions.from_dsn(dsn_or_options) if isinstance(dsn_or_options, str) else dsn_or_options
    return Connection(await connect_impl(options))
