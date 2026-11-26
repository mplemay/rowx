# SQLite

Install `uv add "rowx[sqlite]"`. A runtime-only install is `uv add rowx-core rowx-sqlite`, imported as `rowx_core` and
`rowx_sqlite`. SQLite is bundled, with no system `libsqlite3`.

Import from `rowx.sqlite`: `connect`, `ConnectOptions`, `Connection`, `Pool`, `Transaction` and `Migrator`. Nothing
SQLite-specific is on `rowx`. The query API is the same as for Postgres.

## URL

`sqlite:///relative.db`, `sqlite:////absolute/path.db`, `sqlite:///C:/x.db` and `sqlite::memory:`. Every other form is
rejected with a hint: `sqlite:app.db`, a bare path, `sqlite://host/x.db`, `file:` URIs. The runtime `connect` rejects
any query string; the CLI names only `mode=memory`, `cache=shared`, `immutable` and `vfs`, and fails any other query
string as an unparseable URL (exit 2). A bare string that is not a database file name stays a PostgreSQL keyword DSN.

A URL never creates a file. Open with `ConnectOptions(path=..., mode="rwc")` to create one, or run
`rowx database create`. A missing file is `DatabaseError` `58030`.

```python test
import asyncio
import tempfile
from pathlib import Path

from rowx import DatabaseError, query
from rowx.sqlite import ConnectOptions, Pool, connect


async def main(root: Path) -> None:
    async with await connect(ConnectOptions(path=str(root / "app.db"), mode="rwc")) as conn:
        await query(t"CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, email TEXT)").execute(conn)
        await query(t"INSERT INTO users (name) VALUES ({'alice'})").execute(conn)
        row = await query[tuple[int, str, str | None]](t"SELECT id, name, email FROM users").one(conn)
        assert row[1:] == ("alice", None)
        try:
            await query(t"INSERT INTO users (name) VALUES ({'alice'})").execute(conn)
        except DatabaseError as error:
            assert (error.sqlstate, error.code) == ("23505", "SQLITE_CONSTRAINT_UNIQUE")

    async with Pool(f"sqlite:///{(root / 'app.db').as_posix()}") as pool:
        assert await query[str](t"SELECT name FROM users").all(pool) == ["alice"]


with tempfile.TemporaryDirectory() as tmp:
    asyncio.run(main(Path(tmp)))
```

## Rules that differ from Postgres

- A column reads by its declared type: `INTEGER`, `REAL`, `TEXT`, `BLOB`, plus the names `BOOLEAN`, `DATE`, `TIME`,
  `DATETIME`/`TIMESTAMP` (naive), `TIMESTAMPTZ` (aware, UTC), `DECIMAL`/`NUMERIC`, `UUID` and `JSON`. No declared type,
  or an expression, is *untyped*: read by storage class, always nullable.
- Parameters are untyped, so the checker emits no `param-type-mismatch`. The compiler emits `?N`. A `$1`, `:name`,
  `@name` or `?` in static SQL is rejected, and so is an interpolation inside a string literal, quoted name or comment.
- There are no arrays or `::` casts. Use `CAST({x} AS TEXT)`, and bind a list as JSON text with
  `WHERE id IN (SELECT value FROM json_each({ids}))`.
- `transaction()` is `BEGIN IMMEDIATE`; `read_only=True` is deferred with `query_only`. Writers queue for
  `connect_timeout` (5 seconds).
- `rowx check` proves a column `NOT NULL` only for plain tables joined by `FROM` and inner joins. A view, subquery, CTE,
  compound select, outer join or expression is `Unproven`: nullable. Narrow it with `AS "col!"`.
- `sqlite::memory:` implies `rowx check --scratch` and needs `[tool.rowx.migrate]`. `rowx database` and `rowx migrate`
  reject it.
- The test plugin selects the backend from a `sqlite:` URL and never opens it; each test gets a copy of a migrated file.

The reference is [SQLite in the guide](https://mplemay.github.io/rowx/guide/sqlite/).
