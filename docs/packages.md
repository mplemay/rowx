# Packages

rowx publishes three distributions. Install matching versions of the ones you use. Each distribution has its own
import root.

## rowx-core

The `rowx-core` distribution imports as `rowx_core`. It is pure Python and has no runtime dependencies. It holds the
shared query and executor primitives, including `query`, `query_unchecked`, `QueryResult`, and `RowStream`.

## rowx-postgres

The `rowx-postgres` distribution imports as `rowx_postgres`. It depends only on the matching `rowx-core`. It is the
PostgreSQL driver: connections, pools, transactions, migrations, and the Postgres value types.

The canonical connection imports are:

```python
from rowx_postgres import Pool, connect
```

## rowx

The `rowx` distribution imports as `rowx`. It is the command-line checker and depends on the matching `rowx-core`.
The `postgres` extra adds the matching `rowx-postgres`:

```sh
uv add "rowx[postgres]"
```

`rowx` re-exports the query API. `from rowx import query` and `from rowx_core import query` are the same object.

```python
from rowx import query
```

A query runs on the connection or pool you pass to it. `rowx check` reads `--database-url` or `DATABASE_URL` and does
not import your application.
