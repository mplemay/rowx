# Packages

## Which distribution

| Work | Install |
|---|---|
| Author queries and run `rowx check` | `uv add "rowx[postgres]"` |
| Runtime only, no `rowx` command | `uv add rowx-core rowx-postgres` |
| Pytest plugin, no database driver | `rowx[pytest]` |

`rowx-core` imports as `rowx_core` and has no runtime dependencies. `rowx-postgres` imports as `rowx_postgres` and
depends only on matching `rowx-core`. `rowx` is the CLI. It re-exports the query API from `rowx_core` and, with the
`postgres` extra, the driver as `rowx.postgres`. `rowx.core` re-exports only the `rowx_core` names that `rowx` and
`rowx.errors` do not already export. The backend does not depend on the CLI.

## Imports

With the `rowx` package:

```python
from rowx import query
from rowx.postgres import Pool, connect
```

`from rowx import query` and `from rowx_core import query` are the same object.
`from rowx.postgres import Pool, connect` and `from rowx_postgres import Pool, connect` are the same objects.
`import rowx` loads no backend.

A runtime-only install does not include `rowx`, so it imports `rowx_core` and `rowx_postgres` directly.

`rowx` and `rowx_core` export `query`, `query_unchecked`, `QueryResult`, `RowStream`, the executor protocols
`Executor`, `Connection`, `Pool`, and `Transaction`, and the error classes. Those protocols are not connections.
The `rowx` package opens connections with `rowx.postgres`, and a runtime-only install opens them with
`rowx_postgres`.

`rowx.core` exports `Acquire`, `Compiled`, `Compiler`, `FieldPlan`, `Identifier`, `Parameter`, `ShapeSpec`,
`StreamHandle`, `TemplatePlan`, `ValueClass`, `capture_template`, and `iter_parts`. `rowx.postgres` exports
`connect`, `ConnectOptions`, `Connection`, `Pool`, `Transaction`, `RESET_QUERY`, `Migrator`, `Migration`,
`MigrationStatus`, `MigrationResult`, and the Postgres value types (`Range`, `Interval`, `Money`, `Cube`, and the
geometric types). `rowx.errors` exports the same error classes as `rowx`. `connect` is not on `rowx.core`.

Pass the connection, pool, or transaction into the query method. Do not store it in a ContextVar, do not fall back
to an implicit Postgres target, and do not annotate the executor as `Any`.
