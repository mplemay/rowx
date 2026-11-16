# Packages

rowx publishes four distributions, all released together under one version (0.4.1). Install matching versions of the
ones you use. Each distribution has its own import root. An application that runs queries and checks them installs
one extra, `rowx[postgres]`.

An application that installs `rowx[postgres]` imports `from rowx import ...` and `from rowx.postgres import ...`.
`from rowx import query` is the same object as `from rowx_core import query`, and `from rowx.postgres import connect`
is the same object as `from rowx_postgres import connect`. `Pool` imported from `rowx` is the executor protocol, not a
connection pool. `import rowx` loads no backend.

Install the two packages separately when the application should not include the `rowx` command. That install imports
`rowx_core` and `rowx_postgres`.

```sh
uv add rowx-core rowx-postgres
```

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

Free-threaded CPython 3.14t cannot install the abi3 wheels, so pip builds rowx-postgres from the source distribution.

The `embedded` extra adds `rowx-postgres` and the matching `rowx-embedded` as well (see
[rowx-embedded](#rowx-embedded)).

`rowx` re-exports the query API. `rowx.core` re-exports the `rowx_core` names that `rowx` and `rowx.errors` do not
already export. `rowx.postgres` re-exports the driver and needs the `postgres` extra. `rowx.embedded` re-exports
`rowx_embedded.Database` and needs the `embedded` extra. `from rowx import query` and `from rowx_core import query` are
the same object.

```python
from rowx import query
from rowx.postgres import Pool, connect
```

A query runs on the connection or pool you pass to it. `rowx check` reads `--database-url` or `DATABASE_URL` and does
not import your application.

## rowx-embedded

The `rowx-embedded` distribution's import root is `rowx_embedded`. It is an optional in-process PostgreSQL 18 for
development, checking and tests, and it is not for production. It holds all of the embedded support (the `Database`
class, the `embedded://` backend, the scratch and the test provisioner) on top of the matching `rowx-postgres`, which it
depends on; `rowx-postgres` never imports it. It is released with the other distributions, under the same version and
tag. It is a per-platform wheel (macOS arm64, Linux x86_64 and aarch64) and has no sdist. `rowx[embedded]` installs it,
and `rowx.embedded.Database` (or an `embedded://` URL for the CLI and the pytest plugin) then uses it:

```sh
uv add "rowx[embedded]"
```

See [Embedded Postgres](guide/embedded.md).

## Upgrading from 0.2

0.2.0 published one `rowx` wheel. Its native module was `rowx._core`. Application imports were
`from rowx import query` and `from rowx.postgres import connect, Pool`. There was no separate `rowx-core` or
`rowx-postgres` distribution.

0.3.0 published the split as three lockstep wheels. `rowx-core` is pure Python, imports as `rowx_core`, and has
no runtime dependencies. `rowx-postgres` imports as `rowx_postgres`, its native module is `rowx_postgres._core`, and
it depends only on matching `rowx-core`. `rowx` is pure Python, imports as `rowx`, and depends on matching
`rowx-core`. The `postgres` extra installs matching `rowx-postgres`. This tree is version 0.4.1. 0.4.1 adds
`rowx-embedded` in lockstep with the others. It is not on PyPI until the `v0.4.1` tag is published.

`rowx` re-exports the query API of `rowx_core`. `rowx.core` re-exports the `rowx_core` names that `rowx` and
`rowx.errors` do not already export. `rowx.postgres` re-exports `rowx_postgres` and still needs the `postgres` extra.
The import the guide uses is `from rowx import query` and `from rowx.postgres import connect, Pool`.

What the compiler accepts, including the adjacency space and the 65535-parameter cap, is
[t-string compilation](reference/api.md#t-string-compilation). Each exception class is the same object in `rowx` and
`rowx_core` ([Errors](guide/errors.md)). The release notes are the [changelog](changelog.md).
