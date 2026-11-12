# Packages

The three distributions and the canonical imports are in [Packages](../../../../docs/packages.md). The export lists
are under [Public surface](../../../../docs/reference/api.md#public-surface). Install steps are in
[Get started](../../../../docs/guide/get-started.md).

## Which distribution

| Work | Install |
|---|---|
| Author queries and run `rowx check` | `uv add "rowx[postgres]"` |
| Runtime only, no `rowx` command | `uv add rowx-core rowx-postgres` |
| Pytest plugin, no database driver | `rowx[pytest]` |

`rowx-core` imports as `rowx_core` and has no runtime dependencies. `rowx-postgres` imports as `rowx_postgres` and
depends only on matching `rowx-core`. `rowx` is the CLI, re-exports `rowx_core`, and the `postgres` extra installs
matching `rowx-postgres`. The backend does not depend on the CLI.

## Imports

```python
from rowx import query
from rowx_postgres import Pool, connect
```

`from rowx import query` and `from rowx_core import query` are the same object. `import rowx` loads no backend.

`rowx` and `rowx_core` export the executor protocols `Executor`, `Connection`, `Pool`, and `Transaction`. Those
protocols are not connections. Open a PostgreSQL connection with `rowx_postgres`.

`from rowx.postgres import Pool, connect` is a compatibility re-export of `rowx_postgres`. It still needs the
`postgres` extra. New code imports `rowx_postgres`.

Pass the connection, pool, or transaction into the query method. Do not store it in a ContextVar, do not fall back
to an implicit Postgres target, and do not annotate the executor as `Any`.
