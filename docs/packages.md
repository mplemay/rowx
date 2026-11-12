# Packages

rowx publishes four distributions. Install matching versions of the ones you use. Each distribution has its own
import root.

An application that installs `rowx[postgres]` imports `from rowx import ...` and `from rowx.postgres import ...`.
`from rowx import query` is the same object as `from rowx_core import query`, and `from rowx.postgres import connect`
is the same object as `from rowx_postgres import connect`. A runtime-only install imports `rowx_core` and
`rowx_postgres`. `Pool` imported from `rowx` is the executor protocol, not a connection pool. `import rowx` loads no
backend.

## rowx-core

The `rowx-core` distribution's import root is `rowx_core`. It is pure Python and has no runtime dependencies. It holds
the shared query and executor primitives, including `query`, `query_unchecked`, `QueryResult`, and `RowStream`.

```python
from rowx_core import query
```

## rowx-postgres

The `rowx-postgres` distribution's import root is `rowx_postgres`. It depends only on the matching `rowx-core`. It is
the PostgreSQL driver: connections, pools, transactions, migrations, and the Postgres value types.

The canonical connection imports are:

```python
from rowx_postgres import Pool, connect
```

## rowx

The `rowx` distribution's import root is `rowx`. It is the command-line checker and depends on the matching `rowx-core`.
The `postgres` extra adds the matching `rowx-postgres`:

```sh
uv add "rowx[postgres]"
```

The `embedded` extra adds `rowx-postgres` and the matching `rowx-embedded` as well (see
[rowx-embedded](#rowx-embedded)).

`rowx` re-exports the query API. `rowx.core` re-exports the `rowx_core` names that `rowx` and `rowx.errors` do not
already export. `rowx.postgres` re-exports the driver and needs the `postgres` extra. `from rowx import query` and
`from rowx_core import query` are the same object.

```python
from rowx import query
from rowx.postgres import Pool, connect
```

A query runs on the connection or pool you pass to it. `rowx check` reads `--database-url` or `DATABASE_URL` and does
not import your application.

## rowx-embedded

The `rowx-embedded` distribution's import root is `rowx_embedded`. It is an optional in-process PostgreSQL 18 for
development, checking and tests, and it is not for production. It depends on no other rowx distribution, and
`import rowx_postgres` never imports it. It is a per-platform wheel (macOS arm64, Linux x86_64 and aarch64) and has no
sdist. `rowx[embedded]` installs it, and `rowx.postgres.embedded.Database` (or an `embedded://` URL for the CLI and the
pytest plugin) then uses it:

```sh
uv add "rowx[embedded]"
```

See [Embedded Postgres](guide/embedded.md).
