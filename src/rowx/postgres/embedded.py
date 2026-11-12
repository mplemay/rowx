# Importing the facade should explain how to install its optional dependency.
try:
    from rowx_postgres.embedded import Database
except ModuleNotFoundError as error:
    if error.name != "rowx_postgres":
        raise
    msg = 'Install "rowx[postgres]" to use rowx.postgres.'
    raise ImportError(msg) from error

__all__ = ["Database"]
