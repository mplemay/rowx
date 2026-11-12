# Reference

These pages are the user contract: the public API, PostgreSQL type rules, the checker, nullability, migrations,
security, the driver and the embedded database. A feature PR must not change them silently. Describe a spec gap in the
PR, then update the reference in a follow-up docs PR.

rowx publishes four distributions. Install matching versions of the ones you use.

| Distribution | Import root | Depends on |
|---|---|---|
| `rowx-core` | `rowx_core` | nothing |
| `rowx-postgres` | `rowx_postgres` | matching `rowx-core` |
| `rowx` | `rowx` | matching `rowx-core`; `rowx[postgres]` adds matching `rowx-postgres`, and `rowx[embedded]` adds `rowx-embedded` as well |
| `rowx-embedded` | `rowx_embedded` | nothing |

`rowx-core` is pure Python and holds the shared query and executor primitives. `rowx` re-exports that query API and
supplies the checker, the `rowx` command and the opt-in pytest plugin. `rowx-postgres` is the PostgreSQL driver.
`rowx-embedded` is the optional in-process Postgres 18 engine for development, checking and tests.

`from rowx import query` and `from rowx_core import query` are the same object.
`from rowx.postgres import Pool, connect` and `from rowx_postgres import Pool, connect` are the same objects.
`Pool` imported from `rowx` is the executor protocol, not a connection pool. `import rowx` loads no backend.
With the `rowx` distribution, open connections from `rowx.postgres`:

```python
from rowx import query
from rowx.postgres import Pool, connect
```

A runtime-only install of `rowx-core` and `rowx-postgres` imports `rowx_core` and `rowx_postgres`.

A runtime query compiles with the executor passed to `one`, `optional`, `all`, `fetch` or `execute`. It is not bound
to the first executor that ran it. `rowx check` selects one database from `--database-url` or the configured URL
environment variable. It does not import application code, and it still checks constructions that no query runs.

| Page | Covers |
|---|---|
| [API](api.md) | Public surface, row types, t-strings, overrides and errors |
| [Type mapping](type-mapping.md) | Rules around the generated type table |
| [Checker](checker.md) | `rowx check`, discovery, row types, rules and output |
| [Nullability](nullability.md) | Describe, the plan walk and the evidence table |
| [Migrations](migrations.md) | SQL-file migrations |
| [Security](security.md) | Threat model and the guarantees |
| [Driver](driver.md) | Connections, execution, codecs, the pool and transactions |
| [Embedded Postgres](embedded.md) | The in-process database: `embedded://`, its API, driver, checker and migration rules, and its limits |

The OID table itself is generated: [Postgres type mapping](../types.md). Layout, the decision log and the test
strategy are under [Contributing](../contributing/architecture.md).
