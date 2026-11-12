# Importing the facade should explain how to install its optional dependency.
try:  # ruff: ignore[non-empty-init-module]
    from rowx_postgres import (
        RESET_QUERY,
        Box,
        Circle,
        Connection,
        ConnectOptions,
        Cube,
        Interval,
        Line,
        LineSegment,
        Migration,
        MigrationResult,
        MigrationStatus,
        Migrator,
        Money,
        Path,
        Point,
        Polygon,
        Pool,
        Range,
        Transaction,
        connect,
        embedded,
    )
except ModuleNotFoundError as error:
    if error.name != "rowx_postgres":
        raise
    msg = 'Install "rowx[postgres]" to use rowx.postgres.'
    raise ImportError(msg) from error

__all__ = [
    "RESET_QUERY",
    "Box",
    "Circle",
    "ConnectOptions",
    "Connection",
    "Cube",
    "Interval",
    "Line",
    "LineSegment",
    "Migration",
    "MigrationResult",
    "MigrationStatus",
    "Migrator",
    "Money",
    "Path",
    "Point",
    "Polygon",
    "Pool",
    "Range",
    "Transaction",
    "connect",
    "embedded",
]
