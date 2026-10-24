from rowx._core import connect as connect_impl, ping
from rowx._query import Connection, query, query_unchecked
from rowx.errors import (
    ConnectionBusyError,
    DatabaseError,
    DecodeError,
    EncodeError,
    Error,
    InterfaceError,
    RowNotFound,
    RowShapeError,
)


async def connect(dsn: str, /, *, connect_timeout: float | None = None, statement_cache_size: int = 256) -> Connection:
    impl = await connect_impl(dsn, connect_timeout=connect_timeout, statement_cache_size=statement_cache_size)
    return Connection(impl)


__all__ = [
    "Connection",
    "ConnectionBusyError",
    "DatabaseError",
    "DecodeError",
    "EncodeError",
    "Error",
    "InterfaceError",
    "RowNotFound",
    "RowShapeError",
    "connect",
    "ping",
    "query",
    "query_unchecked",
]
