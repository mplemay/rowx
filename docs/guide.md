# rowx user guide

rowx runs raw SQL written as t-strings and returns typed rows. The same row types drive `rowx check`, a static checker
that prepares every query against a live Postgres 18 database and reports mismatches.

The `python test` blocks on this page are executed against a seeded database (users `alice`, `bob` and `carol`, plus
`orders`) by the test suite, with `DSN` set to its connection string.

## Quickstart

```python test
import asyncio

from rowx.postgres import connect
from rowx import query

type UserRow = tuple[int, str, str | None]


async def main() -> None:
    async with await connect(DSN) as conn:
        uid = await query[int](t"SELECT id FROM users WHERE name = {'alice'}").one(conn)
        user = await query[UserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").one(conn)
        assert user == (uid, "alice", "alice@example.com")


asyncio.run(main())
```

| Method | Result | Behaviour |
|---|---|---|
| `one` | `T` | The first row. Raises `RowNotFound` if there are none. Extra rows are not an error. |
| `optional` | `T \| None` | The first row, or `None` if there are none. Extra rows are not an error. |
| `all` | `list[T]` | Every row. |
| `fetch` | `RowStream[T]` | Streams the rows. See [Streaming](#streaming). |
| `execute` | `QueryResult` | `rows_affected` is the count of rows affected. Allowed on `query[T]`; the rows are discarded. |

`one` and `optional` discard the rest of the result, but DML side effects (`INSERT ... RETURNING`) always run to
completion. Add `LIMIT 1` to large selects: the server still sends the remaining rows, and the connection reads and
discards them before the next operation runs.

`optional` on a nullable scalar is ambiguous. For `query[int | None]`, `None` means either "no row" or a NULL value.
Use a tuple row type (`query[tuple[int | None]]`) when the difference matters. The checker does not report this.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query


async def main() -> None:
    async with await connect(DSN) as conn:
        no_row = await query[int | None](t"SELECT 1 WHERE false").optional(conn)
        null_value = await query[int | None](t"SELECT NULL::int").optional(conn)
        assert no_row is None
        assert null_value is None

        assert await query[tuple[int | None]](t"SELECT 1 WHERE false").optional(conn) is None
        assert await query[tuple[int | None]](t"SELECT NULL::int").optional(conn) == (None,)


asyncio.run(main())
```

## Streaming

`fetch` returns a single-use `RowStream[T]` for results without a known upper bound. It is an async iterator, and an
async context manager for callers that don't want release to depend on garbage collection. The rows cross into Python
`batch_size` at a time (default 1024).

Streaming bounds memory, not time. Each row is a separate `async for` step, so iterating 100,000 rows takes about 1.4
times as long as `all()` (about 72 ms against 52 ms on a release build, for `int4`, `text` and `timestamptz` columns).
Use `all()` when the result is known to be small, and `fetch` when it is large or unbounded.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query


async def main() -> None:
    async with await connect(DSN) as conn:
        users = query[tuple[int, str]](t"SELECT id, name FROM users ORDER BY id")

        names = [name async for _, name in users.fetch(conn)]
        assert names == ["alice", "bob", "carol"]

        async with users.fetch(conn, batch_size=2) as rows:
            assert rows.result is None
            seen = [row async for row in rows]
            assert len(seen) == 3
            # set once the stream is exhausted
            assert rows.result is not None
            assert rows.result.rows_affected == 3


asyncio.run(main())
```

- **No IO until iteration.** `fetch` itself does nothing. The first `async for` step claims the connection, so
  `ConnectionBusyError` and parameter errors are raised there.
- **`.result`** is `None` until the stream is exhausted, then a `QueryResult`. It stays `None` after an early exit,
  an error or a cancellation.
- **The connection is Busy while the stream is open.** From the first row until the stream ends, any other operation on
  that connection raises `ConnectionBusyError`, so a loop body can't query the connection it is streaming from. (A
  result that arrives entirely with the first batch ends the stream at once, so the connection is already idle by the
  first row.)
- **`break` drains.** Stopping early does not cancel the statement: the server finishes it, and the connection
  discards the remaining rows, so DML side effects (`INSERT ... RETURNING`) complete. Prefer `async with`: a stream that
  is still referenced after a `break` stays open until it is collected. Bound large reads with `LIMIT`, or cancel the
  task, when stopping early has to be fast.
- **Cancellation.** Cancelling the task while it waits for a row sends a cancel to the server, as for the other
  methods. A cancelled stream is over: the next step raises `StopAsyncIteration`.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query
from rowx.errors import ConnectionBusyError


async def main() -> None:
    async with await connect(DSN) as conn:
        numbers = query[int](t"SELECT generate_series(1, 100000)")

        async with numbers.fetch(conn, batch_size=1) as rows:
            async for _ in rows:
                try:
                    await query[int](t"SELECT 1").one(conn)
                except ConnectionBusyError:
                    pass
                else:
                    raise AssertionError
                break

        # the stream was closed by `async with`, so the connection is usable again
        assert await query[int](t"SELECT 1").one(conn) == 1


asyncio.run(main())
```

`rowx.postgres.connect(dsn)` returns a `Connection`. Use it as an async context manager, or call `close()`. Pass a
`ConnectOptions` instead of a DSN to set `connect_timeout`, `statement_cache_size`, `server_settings` and more
(`ConnectOptions.from_dsn(dsn, connect_timeout=5)`).

## Row types

`T` in `query[T]` is one of:

- `None` (the default): no rows. Use `.execute()`.
- a scalar, such as `str`: exactly one column.
- `tuple[A, B, ...]`: one element per column, in order.

Elements are `int`, `float`, `str`, `bool`, `bytes`, `Decimal`, `date`, `time`, `datetime`, `timedelta`, `UUID`, or
`list[...]` of those for arrays. Add `| None` for nullable columns. `Any` and `object` match any type and either
nullability. `type` aliases and `Annotated[...]` are followed.

```python test
import asyncio
from decimal import Decimal

from rowx.postgres import connect
from rowx import query

type OrderRow = tuple[int, Decimal, str | None]


async def main() -> None:
    async with await connect(DSN) as conn:
        rows = await query[OrderRow](t"SELECT id, amount, note FROM orders ORDER BY id").all(conn)
        assert rows
        total = await query[Decimal](t'SELECT sum(amount) AS "total!" FROM orders').one(conn)
        assert total == sum(amount for _, amount, _ in rows)


asyncio.run(main())
```

At runtime the number of columns must equal the arity of `T` (otherwise `RowShapeError`), and a NULL in a non-nullable
position raises `RowShapeError` naming the column. `rowx check` finds both statically. See [types.md](types.md) for what
each Postgres type decodes to.

## Writing queries

A `query[T]` takes one inline t-string. Conversions (`{x!r}`, `{x=}`), format specs and nested templates are rejected,
so every interpolation is a bound parameter. Use `query_unchecked` for dynamic SQL; it is skipped by the checker.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query, query_unchecked


async def main() -> None:
    async with await connect(DSN) as conn:
        # IN-lists: bind a list as an array with = ANY(...)
        wanted = ["alice", "bob"]
        found = await query[str](t"SELECT name FROM users WHERE name = ANY({wanted}) ORDER BY name").all(conn)
        assert found == wanted

        # literal braces are doubled
        arr = await query[list[int]](t"SELECT '{{1,2,3}}'::int[]").one(conn)
        assert arr == [1, 2, 3]

        # bind the whole pattern, never an interpolation inside a literal
        pattern = "al%"
        like = await query[str](t"SELECT name FROM users WHERE name LIKE {pattern}").all(conn)
        assert like == ["alice"]

        # cast a parameter whose type the server cannot infer
        missing = None
        none_ok = await query[bool](t"SELECT {missing}::int IS NULL").one(conn)
        assert none_ok

        # dynamic identifiers and nested templates are for query_unchecked only
        table, column = "users", "name"
        cond = t"WHERE id > {0}"
        names = await query_unchecked[str](t"SELECT {column:ident} FROM {table:ident} {cond} ORDER BY 1").all(conn)
        assert names == ["alice", "bob", "carol"]


asyncio.run(main())
```

Rules of thumb:

- **IN-lists.** Write `= ANY({seq})` with `seq` a list.
- **Literal braces.** JSON and array literals in SQL are written `{{` and `}}`.
- **No interpolation inside a string literal or comment.** `LIKE '%{q}%'` puts the text `$1` inside the literal. Bind
  the whole pattern instead: `LIKE {pat}`. The checker reports this as `param-count-mismatch`, and at runtime the
  parameter count disagrees and raises `InterfaceError`.
- **Cast parameters whose type the server can't infer.** For example `{x}::int IS NULL`. Without a cast, Postgres fails
  with 42P18 or 42P08.
- **Don't write `$n` by hand.** It causes the same count mismatch.
- **Reusing an expression** (`{a}` twice) creates two parameters.

In `query_unchecked`, a nested `Template` is flattened and its values stay bound, and `{name:ident}` emits a quoted
identifier (a `str`, or a `tuple[str, ...]` joined with dots). There is no raw-SQL escape.

## Nullability overrides

The checker infers column nullability from the catalog and the query plan. When it can't prove a column is NOT NULL, it
treats it as nullable, and your annotation must say `| None`. Narrow it with a quoted alias:

- `AS "col!"` forces NOT NULL.
- `AS "col?"` forces nullable.

The runtime ignores names (rows are positional), so a NULL in a `!` column still raises `RowShapeError`. Postgres
truncates identifiers at 63 bytes, which would drop the suffix; the checker reports that as `invalid-override`.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query


async def main() -> None:
    async with await connect(DSN) as conn:
        n = await query[int](t'SELECT count(*) AS "n!" FROM users').one(conn)
        assert n == 3
        email = await query[str | None](t'SELECT email AS "email?" FROM users WHERE name = {"carol"}').one(conn)
        assert email is None or isinstance(email, str)


asyncio.run(main())
```

## Pools

`rowx.postgres` holds the Postgres backend (`connect`, `ConnectOptions`, `Connection`, `Pool`, `Transaction`). The
shared pieces (`query`, `query_unchecked`, `QueryResult`, `RowStream`, `Executor` and the errors) stay in `rowx`, and
`import rowx` loads no backend.

A `Pool` is an `Executor`, so every query method takes it in place of a connection. The constructor does no IO, so a
pool is safe to create at module level. `async with pool` warms it to `min_size` and closes it on exit; the first
acquire also opens it.

```python test
import asyncio

from rowx.postgres import Pool
from rowx import query


async def main() -> None:
    async with Pool(DSN, min_size=1, max_size=4) as pool:
        # acquire, run, release: one call
        assert await query[int](t"SELECT 1").one(pool) == 1

        # many tasks share the pool; waiters are served in order
        results = await asyncio.gather(*(query[int](t"SELECT {i}::int").one(pool) for i in range(20)))
        assert results == list(range(20))

        # hold one connection for several statements
        async with pool.acquire() as conn:
            await query(t"CREATE TEMP TABLE scratch (n int)").execute(conn)
            await query(t"INSERT INTO scratch VALUES (1), (2)").execute(conn)
            assert await query[int](t'SELECT count(*) AS "n!" FROM scratch').one(conn) == 2

        assert pool.size <= pool.max_size


asyncio.run(main())
```

### Sizing and lifetimes

| Option | Default | Meaning |
|---|---|---|
| `min_size` | 0 | Connections kept open (opened by `open()` and maintained in the background). |
| `max_size` | 10 | The most connections open at once. Waiters queue past it. |
| `acquire_timeout` | 30.0 | Seconds an acquire waits before `PoolTimeoutError`. A failed connect is retried until then. |
| `idle_timeout` | 600.0 | Idle connections above `min_size` are closed after this long. |
| `max_lifetime` | 1800.0 | A connection older than this is closed on release instead of reused. |
| `max_queries` | `None` | Retire a connection after this many queries. |
| `ping_after` | 30.0 | A connection idle for longer is pinged at checkout. There is no round trip otherwise. |

Idle connections are reused most-recently-used first, so hot connections stay hot and cold ones age out. A query that
fails with an IO error is not retried, because it may not be idempotent. `pool.expire_connections()` retires every
connection as it is released. `pool.size`, `idle`, `waiting`, `min_size`, `max_size` and `closed` are available for
metrics.

```python test
import asyncio

from rowx.postgres import Pool
from rowx import query
from rowx.errors import PoolTimeoutError


async def main() -> None:
    async with Pool(DSN, max_size=1, acquire_timeout=0.2) as pool, pool.acquire():
        try:
            await query[int](t"SELECT 1").one(pool)
        except PoolTimeoutError:
            pass
        else:
            raise AssertionError


asyncio.run(main())
```

### Hooks and the reset

Hooks are async callables that take a `Connection`. They run on the caller's event loop, and a hook that raises
discards the connection.

- `init` runs once per connection, at its first checkout.
- `setup` runs before each checkout.
- `reset` runs at release, after the reset query.

On release the pool runs `reset_query` (by default `RESET_QUERY`: `SELECT pg_advisory_unlock_all(); CLOSE ALL;
UNLISTEN *; RESET ALL;`), preceded by a `ROLLBACK` when the connection may be inside a transaction. It does not
`DISCARD ALL`, so the statement cache survives. Pass `reset_query=None` to turn it off.

`RESET ALL` returns session settings to their startup values. That is why `server_settings` survive a release (they
are part of the startup options) while a `SET` run in `init` does not: put settings you want to keep in
`ConnectOptions(server_settings=...)`, or re-apply them in `setup`.

```python test
import asyncio

from rowx.postgres import ConnectOptions, Connection, Pool
from rowx import query


async def main() -> None:
    async def init(conn: Connection) -> None:
        await query(t"SET work_mem = '8MB'").execute(conn)

    opts = ConnectOptions.from_dsn(DSN, server_settings={"statement_timeout": "5s"})
    async with Pool(opts, max_size=1, init=init) as pool:
        async with pool.acquire() as conn:
            assert await query[str](t"SHOW work_mem").one(conn) == "8MB"
            assert await query[str](t"SHOW statement_timeout").one(conn) == "5s"
        async with pool.acquire() as conn:
            # RESET ALL put work_mem back; the startup setting survived
            assert await query[str](t"SHOW work_mem").one(conn) != "8MB"
            assert await query[str](t"SHOW statement_timeout").one(conn) == "5s"


asyncio.run(main())
```

A `fetch` on a pool acquires at its first row and holds that connection until the stream ends or is closed. Prefer
`async with`, and see the caveats below.

### Deployment caveats

- **Fork.** A pool records its process id, and using it in a forked child raises `InterfaceError`. Create the pool
  after the fork (for gunicorn, do not create it at import time under `--preload`; create it in a worker start hook or
  the app's startup).
- **What `RESET ALL` does not reset:** `SET ROLE`, `SET SESSION AUTHORIZATION` and temporary tables. Undo them
  yourself, in a `reset` hook or a custom `reset_query`.
- **Early exit from a pooled stream drains.** Breaking out of a `fetch` makes the connection discard the remaining rows
  before the reset runs. The connection stays counted against `max_size` while it drains. Bound large reads with
  `LIMIT`.
- **Hooks move release onto the caller's path.** With no hooks, release happens in the background and `__aexit__`
  returns at once. With any hook set, `__aexit__` awaits the `reset` hook and the reset query first.
- **Leases.** A stream or transaction on a pooled connection is tied to that checkout. Using it after the
  `async with pool.acquire()` block raises `InterfaceError`. `conn.detach()` takes a connection out of the pool for
  good.

### Coming from asyncpg

| asyncpg | rowx |
|---|---|
| `await asyncpg.create_pool(dsn, min_size=1, max_size=10)` | `Pool(dsn, min_size=1, max_size=10)`, then `async with pool` or `await pool.open()` |
| `await pool.fetchrow(sql, *args)` | `await query[Row](t"...").one(pool)` (or `.optional(pool)`) |
| `async with pool.acquire() as conn:` | `async with pool.acquire() as conn:` |
| `async with conn.transaction():` | `async with conn.transaction() as tx:` or `async with pool.transaction() as tx:` |
| `await pool.close()` | `await pool.close()` (`terminate()` is synchronous) |

## Transactions

`conn.transaction()` and `pool.transaction()` (which holds a connection for the block) return a `Transaction`. It is an
`Executor`: queries run on it are part of the transaction. A clean exit commits, and an exception or a cancellation
rolls back. `commit()` and `rollback()` end it early, and using the transaction afterwards raises `InterfaceError`.
`tx.transaction()` opens a savepoint, which takes no isolation arguments.

```python test
import asyncio

from rowx.postgres import Pool
from rowx import query


async def main() -> None:
    async with Pool(DSN, max_size=2) as pool:
        count = query[int](t'SELECT count(*) AS "n!" FROM orders')
        before = await count.one(pool)

        # an exception rolls back
        try:
            async with pool.transaction(isolation="serializable") as tx:
                await query(t"DELETE FROM orders").execute(tx)
                raise RuntimeError
        except RuntimeError:
            pass
        assert await count.one(pool) == before

        # a savepoint rolls back on its own
        async with pool.transaction() as tx:
            await query(t"DELETE FROM orders WHERE id = (SELECT min(id) FROM orders)").execute(tx)
            try:
                async with tx.transaction():
                    await query(t"DELETE FROM orders").execute(tx)
                    raise RuntimeError
            except RuntimeError:
                pass
            assert await count.one(tx) == before - 1
            await tx.rollback()
        assert await count.one(pool) == before


asyncio.run(main())
```

- **Failed transactions.** An error inside the block marks the transaction failed. Leaving the block cleanly while it
  is failed raises `InterfaceError` instead of letting `COMMIT` roll back silently. Rolling back to a savepoint clears
  the mark.
- **The ending statement can't be cancelled.** `COMMIT` and `ROLLBACK` run detached, so a second cancel can't leave the
  transaction open. If you are cancelled while a `COMMIT` is in flight, the outcome is unknown: it raises and the
  connection is discarded.
- **A failed `COMMIT`** (for example `40001`) raises `DatabaseError`; the connection is left idle and outside a
  transaction.
- **Raw `BEGIN` and `COMMIT` alongside the helper are unsupported.** Use one or the other on a connection.

## The checker

```text
rowx check [PATHS...] [--database-url URL] [--config FILE] [--output-format text|json] [--statistics] [-j N]
```

The checker never imports or executes your code. It parses your files, finds every `query[T](t"...")` call, resolves `T`
statically, and asks Postgres to prepare and describe the SQL. Then it compares the columns and parameters with your
types. See [the checker rules](design/checker.md#rules-all-errors) for the rules.

| Exit code | Meaning |
|---|---|
| 0 | No diagnostics, including when no queries were found. |
| 1 | One or more diagnostics. |
| 2 | Usage or config error, an unreachable database, a describe failure that is not an SQL error, or an internal error. |

Configuration lives in `pyproject.toml`:

```toml
[tool.rowx]
include = ["src"]                 # default ["."]
exclude = ["**/migrations/**"]
database_url_env = "DATABASE_URL"
statement_timeout = "10s"         # integer + unit: ms|s|min
```

The database URL comes from `--database-url`, else from the environment variable named by `database_url_env`.

### Safety

- The checker performs Parse/Describe, catalog lookups and `EXPLAIN` only, never `ANALYZE`. It does not execute your
  statements.
- Postgres may still **evaluate user-defined IMMUTABLE and STABLE functions while planning**, and it takes locks on
  referenced relations.
- Run it as a dedicated non-superuser role against a CI or disposable database.

The checker's session is read-only by default (`default_transaction_read_only = on`) and sets `statement_timeout`,
`lock_timeout` and `idle_in_transaction_session_timeout`.

## CI recipe

Run the checker against a throwaway `postgres:18` service that has your schema applied.

```yaml
name: rowx

on: [push, pull_request]

jobs:
  check:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18
        env:
          POSTGRES_USER: app
          POSTGRES_PASSWORD: app
          POSTGRES_DB: app
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U app -d app"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      DATABASE_URL: postgresql://app:app@localhost:5432/app
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
      - run: uv sync
      # migrate: replace with your migration tool
      - run: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql
      - run: uv run rowx check src
```

Pin the action versions to commit hashes in your own workflow. Migrate **before** checking: the checker validates
against whatever schema the database has.

## Server-side poolers and the statement cache

Each connection keeps an LRU cache of prepared statements, sized by `statement_cache_size` (default 256). `0` disables
the cache, so every call uses an unnamed prepare.

Behind an external pooler in **transaction mode** (PgBouncer, for example), a prepared statement may land on a different
server connection than the one that prepared it. Either connect with `statement_cache_size=0`, or use PgBouncer 1.21 or
newer with `max_prepared_statements` set.

```python
from rowx.postgres import ConnectOptions, Pool

pool = Pool(ConnectOptions.from_dsn("postgresql://app@pooler:6432/app", statement_cache_size=0), max_size=5)
```

Also behind a pooler:

- **Transaction mode** hands out a different server connection per transaction, so session state (`SET`, advisory
  locks, `LISTEN`) isn't reliable. The reset query is then pointless work, so consider `reset_query=None`, and keep
  `max_size` below the pooler's server pool size.
- **Session mode** keeps a server connection per client connection, so the default reset works as it does against
  Postgres directly.

TLS defaults to `prefer`, as in libpq: it uses TLS when the server offers it, without verifying the certificate, and
falls back to plaintext when the server doesn't support TLS. Use `sslmode=verify-full` in production.

## Known limits

- **Nullability inference** is conservative, never optimistic. Views, set operations, recursive CTEs and computed
  columns are nullable; use `"col!"` to narrow. See [known-limits.md](known-limits.md) for each case.
- **Unsupported types** (`money`, ranges, composites, `xml`, `hstore`, `regclass`, ...) are
  `unsupported-type` in the checker and raise `DecodeError` at runtime. Cast to `text`.
- **Decode errors.** `date`, `timestamp` and `timestamptz` that are infinite or outside years 1 to 9999, and `interval`
  values with a months component, raise `DecodeError`. Cast to `text`.
- **Array elements.** Element types and nullability inside `list[...]` are not checked at runtime; a NULL element
  decodes to `None`.
- **One operation per connection.** There is no pipelining yet. An open `fetch` stream holds the connection until it
  ends (on a pool, until it releases the lease).
- **No multi-statement execution** and **no command tag** in `QueryResult`. See [known-limits.md](known-limits.md).
- **Cancellation.** A cancelled statement inside a transaction aborts it (25P02), and a future cancelled during prepare
  can leak a named server statement until the connection closes.
- **Schema changes.** If the schema changes inside a transaction, a retried statement can fail with 25P02. `ROLLBACK`
  and retry.
- **Named row types** (dataclasses, TypedDict, ...) and type overrides (`"col: Type"`) are not supported. Rows are
  tuples and scalars only.
- **`float` parameters.** An `int` above 2^53 bound to a `float4`/`float8` parameter loses precision.
