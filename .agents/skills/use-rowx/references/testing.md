# Testing

The plugin, fixtures, and template database are in [Testing](../../../../docs/guide/testing.md). How this
repository runs tests, including the snippet glob, is in
[Testing](../../../../docs/contributing/testing.md#test-plugin).

## Opt in

`rowx.testing` is opt-in. Nothing registers it as a pytest entry point.

```python
pytest_plugins = ["rowx.testing"]
```

`uv add "rowx[pytest]"` adds pytest and does not add a database backend. A database test also needs the postgres
backend: `rowx[postgres]`, or `rowx-core` and `rowx-postgres`. Author work that uses the plugin and a database
installs both extras: `uv add "rowx[postgres,pytest]"`.

The fixtures are `rowx_database_url`, `rowx_pool`, and `rowx_connection`. The fixtures are synchronous, so an async
test uses `asyncio.run`, or runs under pytest-asyncio or anyio. The plugin supports asyncio only: rowx awaitables
are asyncio futures, so anyio works on its asyncio backend, and Trio does not. Without a URL the tests skip, or
fail when `rowx_require_database` is true. A missing migrations directory fails the session. With neither
`[tool.rowx.migrate]` nor `rowx_migrations`, tests get an empty database.
