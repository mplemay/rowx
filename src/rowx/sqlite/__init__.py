# Importing the facade should explain how to install its optional dependency.
try:  # ruff: ignore[non-empty-init-module]
    from rowx_sqlite import Connection, ConnectOptions, Migrator, Pool, Transaction, connect
except ModuleNotFoundError as error:
    if error.name != "rowx_sqlite":
        raise
    msg = 'Install "rowx[sqlite]" to use rowx.sqlite.'
    raise ImportError(msg) from error

__all__ = [
    "ConnectOptions",
    "Connection",
    "Migrator",
    "Pool",
    "Transaction",
    "connect",
]
