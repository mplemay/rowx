# Importing the facade should explain how to install its optional dependency.
try:
    from rowx_embedded import Database
except ModuleNotFoundError as error:
    if error.name != "rowx_embedded":
        raise
    msg = 'Install "rowx[embedded]" to use rowx.embedded.'
    raise ImportError(msg) from error

__all__ = ["Database"]
