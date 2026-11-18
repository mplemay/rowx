# Importing the facade should explain how to install its optional dependency.
try:  # ruff: ignore[non-empty-init-module]
    import rowx_sqlite  # ruff: ignore[unused-import]
except ModuleNotFoundError as error:
    if error.name != "rowx_sqlite":
        raise
    msg = 'Install "rowx[sqlite]" to use rowx.sqlite.'
    raise ImportError(msg) from error
