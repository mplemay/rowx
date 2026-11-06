from pkgutil import extend_path

from rowx._executor import Executor
from rowx._query import QueryResult, RowStream, query, query_unchecked
from rowx.errors import (
    ConnectionBusyError,
    DatabaseError,
    DecodeError,
    DirtyMigrationError,
    EncodeError,
    Error,
    InterfaceError,
    IrreversibleMigrationError,
    MigrateError,
    MigrationChecksumError,
    MigrationFailedError,
    MigrationMissingError,
    MigrationOrderError,
    MigrationSourceError,
    PoolTimeoutError,
    RowConstructError,
    RowNotFound,
    RowShapeError,
)

__all__ = [
    "ConnectionBusyError",
    "DatabaseError",
    "DecodeError",
    "DirtyMigrationError",
    "EncodeError",
    "Error",
    "Executor",
    "InterfaceError",
    "IrreversibleMigrationError",
    "MigrateError",
    "MigrationChecksumError",
    "MigrationFailedError",
    "MigrationMissingError",
    "MigrationOrderError",
    "MigrationSourceError",
    "PoolTimeoutError",
    "QueryResult",
    "RowConstructError",
    "RowNotFound",
    "RowShapeError",
    "RowStream",
    "query",
    "query_unchecked",
]

# runtime only: an editable install has one `rowx` directory per member, and this merges them
__path__ = extend_path(__path__, __name__)
