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

## Migrations

Migrations are plain `.sql` files in the format [sqlx](https://github.com/launchbadge/sqlx) uses. `Migrator` applies
them in version order, records each one in a history table, and holds an advisory lock while it works. `rowx migrate`
and `rowx database` do the same from the command line, and `rowx check` and the `rowx.testing` pytest plugin build on
it. The blocks below are executed by the test suite, each in a temporary directory.

### Files

```text
migrations/
  0001_create_accounts.up.sql      # a reversible pair
  0001_create_accounts.down.sql
  0002_add_email.sql               # a simple (forward-only) migration
  0003_index_accounts_email.up.sql    # starts with -- no-transaction
  0003_index_accounts_email.down.sql  # so does its own down file
```

- **Names.** `<VERSION>_<DESCRIPTION>.sql` is a simple migration, and `<VERSION>_<DESCRIPTION>.up.sql` with
  `<VERSION>_<DESCRIPTION>.down.sql` is a reversible one. The version is ASCII digits that fit an `i64`: `0` is
  valid, and leading zeros are allowed (`1_a.sql` and `001_b.sql` are the same version). `_` in the description reads
  as a space. Simple and reversible migrations can be mixed.
- **Errors at construction.** A `.sql` file that doesn't match the grammar, a duplicate version, an `.up.sql` without
  its `.down.sql` (or the reverse), and a file that isn't UTF-8 each raise `MigrationSourceError` naming the file. Files
  that don't end in `.sql` and subdirectories are ignored.
- **Scaffolding.** `rowx migrate add "add email"` writes the next file. Versions are sequential (`0001`) when the last
  two differ by one and timestamps (`20261029120000`, UTC) otherwise. The kind follows the latest migration. Pass
  `--reversible` or `--simple`, and `--timestamp` or `--sequential`, to override.
- **`-- no-transaction`.** A file whose bytes start with `-- no-transaction` runs without the `BEGIN` and `COMMIT` rowx
  puts around other files. It is still sent as **one** simple-protocol query, and Postgres runs a multi-statement query
  as one implicit transaction. So:
  - a statement that can't run in a transaction block (`CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`,
    `REINDEX ... CONCURRENTLY`, `VACUUM`) must be the **only** statement in its file;
  - a new enum value from `ALTER TYPE ... ADD VALUE` can't be used in the statement batch that adds it, so put the use
    in a later migration, or give the file its own `BEGIN; ...; COMMIT;` pairs.
- **Checksums.** A history row stores the SHA-384 of the file's raw bytes (the `.up.sql` for a pair), with no
  normalisation. Changing a byte, including a line ending, makes an applied migration `modified`.

```python test
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from rowx.postgres import Migrator, Pool

FILES = {
    "0001_create_accounts.up.sql": "CREATE TABLE accounts (id int PRIMARY KEY, name text NOT NULL);\n",
    "0001_create_accounts.down.sql": "DROP TABLE accounts;\n",
    "0002_add_email.up.sql": "ALTER TABLE accounts ADD COLUMN email text;\n",
    "0002_add_email.down.sql": "ALTER TABLE accounts DROP COLUMN email;\n",
    # alone in its file, because a no-transaction file is still one implicit transaction
    "0003_index_accounts_email.up.sql": "-- no-transaction\nCREATE INDEX CONCURRENTLY accounts_email ON accounts (email);\n",
    "0003_index_accounts_email.down.sql": "-- no-transaction\nDROP INDEX CONCURRENTLY accounts_email;\n",
}


async def main(directory: Path) -> None:
    for name, sql in FILES.items():
        (directory / name).write_text(sql)

    # reads and validates the files now, with no database IO
    migrator = Migrator(directory)
    assert [m.version for m in migrator.migrations if m.kind != "down"] == [1, 2, 3]
    assert migrator.migrations[-1].no_transaction

    async with Pool(DSN, max_size=2) as pool:
        # the plan, read under the migration lock: nothing runs and nothing is written
        planned = await migrator.run(pool, dry_run=True)
        assert [s.version for s in planned] == [1, 2, 3]
        assert [s.state for s in await migrator.info(pool)] == ["pending"] * 3

        for status in await migrator.run(pool):
            assert status.state == "applied"
            assert status.execution_time is not None
        assert await migrator.run(pool) == ()

        # no target reverts only the latest migration
        assert [s.version for s in await migrator.undo(pool, target=None)] == [3]
        # a target reverts every applied version above it, newest first; 0 reverts them all
        assert [s.version for s in await migrator.undo(pool, target=0, dry_run=True)] == [2, 1]
        assert [s.version for s in await migrator.undo(pool, target=1)] == [2]
        assert [s.state for s in await migrator.info(pool)] == ["applied", "pending", "pending"]


with TemporaryDirectory() as tmp:
    asyncio.run(main(Path(tmp)))
```

### Running

At startup, in the application, or as a deploy step. The lock makes both safe when several instances start together.

```python
from rowx.postgres import Migrator, Pool

migrator = Migrator("migrations")


async def start(dsn: str) -> Pool:
    pool = Pool(dsn)
    await pool.open()
    for status in await migrator.run(pool):
        print(f"applied {status.version} {status.description} in {status.execution_time}")
    return pool
```

```sh
rowx migrate run                      # applies every pending migration
rowx migrate run --dry-run            # prints the plan and writes nothing
rowx migrate run --target-version 20261029120000
rowx migrate info
```

- `run`, `undo`, `skip` and `info` take a `Connection` or a `Pool` and return a `tuple[MigrationStatus, ...]`: one entry
  per migration applied, reverted or skipped, or per version for `info`. `status.state` is `applied`, `pending`,
  `modified`, `missing` or `dirty`, and `installed_on` and `execution_time` are set for applied rows. `execution_time`
  is a `timedelta`, or `None` for a dry run or a skipped row. For `undo` it is how long the revert took.
- A `Transaction`, or a `Connection` inside one, raises `InterfaceError`, because each migration manages its own
  transaction. The operation holds its connection for the whole run, so a concurrent query on that `Connection` raises
  `ConnectionBusyError`.
- `migrator.migrations` lists one `Migration` per file (`version`, `description`, `kind` of `simple`, `up` or `down`,
  `checksum`, `no_transaction` and `path`). The files are read when `Migrator` is constructed, so a later edit needs a
  new `Migrator`.
- `Migrator.skip` records pending migrations as applied without running them, to baseline a database. It has no CLI
  command.
- Migrate over a direct connection or a session-mode pooler. The lock is a session-level advisory lock, so PgBouncer in
  transaction mode is unsupported.

```text
$ rowx migrate run
Applied 1/migrate create accounts (3.2ms)
Applied 2/migrate add email (2.0ms)
$ rowx migrate info
1/applied create accounts (3.2ms)
2/applied add email (2.0ms)
3/pending index accounts email
```

`rowx migrate` takes `--database-url` or reads the variable named by `database_url_env`, finds `dir` and the other
settings in `[tool.rowx.migrate]`, and takes `--source DIR` (relative to the current directory) to override the
directory. A flag beats the config, which beats the default (`migrations/`, table `_rowx_migrations`).

```toml
[tool.rowx.migrate]                # its presence turns on the drift gate, --scratch and the test plugin
dir = "migrations"                 # relative to pyproject.toml
table = "_rowx_migrations"         # "_sqlx_migrations" adopts an sqlx project
create_schemas = []                # CREATE SCHEMA IF NOT EXISTS, before the table is created
versioning = "inferred"            # inferred | timestamp | sequential (for `migrate add`)
kind = "inferred"                  # inferred | simple | reversible (for `migrate add`)
```

`rowx database` manages the database itself, from the same URL:

```text
rowx database create               # exits 0 if the database already exists
rowx database drop [--force] [-y]  # prompts on a terminal; -y is required otherwise
rowx database reset [--force] [-y] # drop, create, then run every migration
rowx database setup                # create if missing, then run every migration
```

### Safety

- **Out of order.** A pending migration older than the latest applied one raises `MigrationOrderError`. A branch that
  merged late is the usual cause. `Migrator(..., allow_out_of_order=True)` or `rowx migrate run --allow-out-of-order`
  applies it, in version order.
- **Modified.** An applied migration whose file changed raises `MigrationChecksumError`. Fix it with a new migration,
  not by editing history. `rowx migrate info` shows both short checksums.
- **Missing.** An applied version with no local file raises `MigrationMissingError`, unless `ignore_missing=True` or
  `--ignore-missing`. Even then, reverting such a version fails with `MigrationSourceError`, since there is no
  `.down.sql` to run.
- **Dirty.** A `-- no-transaction` file records its row with `success = false` before it runs. If the file fails, the
  row stays, `info` reports `dirty`, and the next run raises `DirtyMigrationError` rather than guess what half-applied.
  A transactional file rolls back whole and leaves nothing. To recover from a dirty row, inspect the database, undo or
  finish the file's changes by hand, then delete the row so the migration is pending again:

```python test
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from rowx import query_unchecked
from rowx.errors import DirtyMigrationError, MigrationChecksumError, MigrationFailedError, MigrationOrderError
from rowx.postgres import Migrator, connect

TABLE = "_rowx_migrations"


async def main(directory: Path) -> None:
    (directory / "1_accounts.sql").write_text("CREATE TABLE accounts (id int PRIMARY KEY);\n")
    (directory / "3_email.up.sql").write_text("ALTER TABLE accounts ADD COLUMN email text;\n")
    (directory / "3_email.down.sql").write_text("ALTER TABLE accounts DROP COLUMN email;\n")

    async with await connect(DSN) as conn:
        await Migrator(directory).run(conn)

        # a branch lands a version below the latest applied one
        (directory / "2_name.sql").write_text("ALTER TABLE accounts ADD COLUMN name text;\n")
        try:
            await Migrator(directory).run(conn)
        except MigrationOrderError as error:
            assert error.version == 2
        applied = await Migrator(directory, allow_out_of_order=True).run(conn)
        assert [s.version for s in applied] == [2]

        # an applied file changed
        (directory / "1_accounts.sql").write_text("CREATE TABLE accounts (id bigint PRIMARY KEY);\n")
        try:
            await Migrator(directory).run(conn)
        except MigrationChecksumError as error:
            assert error.version == 1
        (directory / "1_accounts.sql").write_text("CREATE TABLE accounts (id int PRIMARY KEY);\n")

        # a no-transaction file that fails part way leaves a dirty row
        (directory / "4_broken.sql").write_text("-- no-transaction\nCREATE TABLE broken (id int);\nSELECT 1 / 0;\n")
        migrator = Migrator(directory)
        try:
            await migrator.run(conn)
        except MigrationFailedError as error:
            assert error.version == 4
            assert error.__cause__ is not None  # the server's DatabaseError
        assert [s.state for s in await migrator.info(conn)][-1] == "dirty"
        try:
            await migrator.run(conn)
        except DirtyMigrationError as error:
            assert error.version == 4

        # recovery: fix the database by hand, fix the file, delete the row
        (directory / "4_broken.sql").write_text("-- no-transaction\nCREATE TABLE IF NOT EXISTS broken (id int);\n")
        await query_unchecked(t"DELETE FROM {TABLE:ident} WHERE version = {4}").execute(conn)
        # a Migrator reads its files when it is constructed, so it is rebuilt after the edit
        assert [s.version for s in await Migrator(directory).run(conn)] == [4]


with TemporaryDirectory() as tmp:
    asyncio.run(main(Path(tmp)))
```

- **Revert.** `undo(target=None)` reverts only the latest applied migration. `undo(target=V)` reverts every applied
  version above `V`, newest first, and `target=0` reverts them all. `rowx migrate revert` and
  `rowx migrate revert --target-version V` do the same. If any version in the range has no `.down.sql`, nothing is
  reverted and `MigrationSourceError` names the version. A transactional `.down.sql` runs in a transaction with the
  history row's deletion, and a `-- no-transaction` one leaves a dirty row when it fails.
- **Failures.** A statement that fails inside a migration raises `MigrationFailedError` with the server's error as
  `__cause__`. Migrations that committed before it stay applied.
- **Trusted code.** Migration files run with the connecting role's privileges and are not parsed or sandboxed. A file
  that ends the transaction rowx opened (`COMMIT` or `ROLLBACK`) is not detected: mark such a file `-- no-transaction`
  and write its own `BEGIN` and `COMMIT`.

### Setup

- **Line endings.** Checksums are over the raw bytes, so a checkout that rewrites line endings makes every applied
  migration `modified`. Add this to `.gitattributes`:

  ```text
  migrations/*.sql text eol=lf
  ```

- **Timeouts.** Migrations don't change `statement_timeout` or `lock_timeout`. Set them on the migrating role, or in the
  URL, rather than in a file, since a lone `CREATE INDEX CONCURRENTLY` can't share its file with a `SET`:

  ```sql
  ALTER ROLE migrator SET lock_timeout = '5s';
  ```

  ```text
  postgresql://migrator@db/app?options=-c%20lock_timeout%3D5s
  ```

  In a transactional file, `SET LOCAL lock_timeout = '5s';` scopes the setting to that migration. A plain `SET` stays on
  the connection.
- **Privileges.** The role needs `CREATE` on the schema that holds the history table, and `rowx check`'s drift gate
  needs `SELECT` on it.

### Adopting from sqlx

rowx reads sqlx's file format and history table, and takes sqlx's advisory lock, so a project switches without renaming
files or rewriting history. Keep the directory and set the table:

```toml
[tool.rowx.migrate]
dir = "migrations"
table = "_sqlx_migrations"
```

```python test
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from rowx import query_unchecked
from rowx.postgres import Migrator, connect

# sqlx stores the SHA-384 of the file bytes: `printf 'CREATE TABLE people (id int PRIMARY KEY);\n' | shasum -a 384`
CHECKSUM = bytes.fromhex(
    "59216535ceb8079ed0db7163ceac6f6072013166f5f78c541a44f91f05cabeda2325e7324a6c40e6ea5cb7924bbc71db"
)


async def main(directory: Path) -> None:
    (directory / "20240101000000_create_people.sql").write_text("CREATE TABLE people (id int PRIMARY KEY);\n")
    (directory / "20240102000000_add_email.up.sql").write_text("ALTER TABLE people ADD COLUMN email text;\n")
    (directory / "20240102000000_add_email.down.sql").write_text("ALTER TABLE people DROP COLUMN email;\n")

    async with await connect(DSN) as conn:
        # sqlx's history table, as `sqlx migrate run` creates it
        await query_unchecked(
            t"""
            CREATE TABLE _sqlx_migrations (
                version BIGINT PRIMARY KEY,
                description TEXT NOT NULL,
                installed_on TIMESTAMPTZ NOT NULL DEFAULT now(),
                success BOOLEAN NOT NULL,
                checksum BYTEA NOT NULL,
                execution_time BIGINT NOT NULL
            )
            """
        ).execute(conn)
        await query_unchecked(t"CREATE TABLE people (id int PRIMARY KEY)").execute(conn)
        await query_unchecked(
            t"INSERT INTO _sqlx_migrations (version, description, success, checksum, execution_time) "
            t"VALUES (20240101000000, 'create people', true, {CHECKSUM}, 1000)"
        ).execute(conn)

        migrator = Migrator(directory, table="_sqlx_migrations")
        assert [(s.version, s.state) for s in await migrator.info(conn)] == [
            (20240101000000, "applied"),
            (20240102000000, "pending"),
        ]
        assert [s.version for s in await migrator.run(conn)] == [20240102000000]


with TemporaryDirectory() as tmp:
    asyncio.run(main(Path(tmp)))
```

What differs from sqlx:

- **Unpaired files are errors.** An `.up.sql` without its `.down.sql` (or the reverse) raises `MigrationSourceError`.
  sqlx tolerates it. A `.sql` file whose name doesn't match the grammar is also an error.
- **No `ignored_chars`.** sqlx's `sqlx.toml` option to ignore characters when hashing isn't ported, so a project that
  used it sees `modified` for the affected files.
- **Table names follow Postgres rules.** `table` and `create_schemas` are parsed like identifiers in SQL: an unquoted
  part matches `[A-Za-z_][A-Za-z0-9_$]*` and is folded to lower case, so sqlx's `MySchema.Migrations` finds the same
  table in rowx. A `"…"` part is kept verbatim, with `""` for a `"`. A `.` outside quotes separates the schema from the
  table, and an empty part, a third part, a NUL byte, a bad character or a part over 63 bytes raises `ValueError`.
- **Dirty rows.** A `-- no-transaction` file that fails leaves a `success = false` row, which sqlx also reports as
  dirty. sqlx itself records nothing when such a file fails.
- **Locks exclude each other.** Both tools take `pg_advisory_lock` on the same key, derived from the database name, so
  an sqlx deploy and a rowx deploy wait for one another.

### Checking

With `[tool.rowx.migrate]` in `pyproject.toml`, `rowx check` compares the database with the directory before it
describes any query. A `pending`, `modified`, `missing` or `dirty` migration exits 2 with a summary on stderr and
nothing on stdout:

```text
error: database has drifted from `migrations/`:
  3 pending: 20261001090000, 20261002090000, 20261003090000
run `rowx migrate run`, or `rowx check --scratch`
```

- The gate engages only when at least one query is described, and `--no-drift-check` skips it.
- The check reads the history table and creates nothing. The role needs `SELECT` on it. It takes the migration lock,
  so it waits for a migration that is running to finish.
- Without `[tool.rowx.migrate]`, `rowx check` behaves as it did before.

`rowx check --scratch` needs no database to prepare. It treats the URL as a **server** URL, creates `rowx_check_<hex>`,
runs the migrations in it, checks every query against it, and drops it on every exit path. It needs a role with
`CREATEDB`, and it skips the drift gate, since the database is built from the files. Like the gate, it only builds the
database when at least one query is described, so a tree with no queries doesn't validate the migrations (use
`rowx database setup` or `rowx migrate run` for that).

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
      # builds a scratch database from the migrations, checks against it, and drops it
      - run: uv run rowx check --scratch src
```

The scratch database is created and dropped by the run itself, so the service only has to accept connections. To check
against a database you migrated yourself, run `rowx migrate run` first and drop `--scratch`; the drift gate then fails
the job if the database and the directory disagree.

### Testing

`rowx.testing` is a pytest plugin that gives each test a freshly migrated database. It is opt-in: install the extra and
name the plugin in a `conftest.py`.

```sh
pip install "rowx[pytest]"
```

```python
# conftest.py
pytest_plugins = ["rowx.testing"]
```

(`-p rowx.testing` on the command line works too. `import rowx` never imports pytest, and nothing loads without this.)

| Fixture | Value |
|---|---|
| `rowx_database_url` | The URL of a database cloned for this test. |
| `rowx_pool` | An open `Pool` on that database. |
| `rowx_connection` | A `Connection` to it. |

The fixtures are synchronous, so an async test uses the plugin's database through `asyncio.run`, or runs under
pytest-asyncio or anyio. The plugin supports **asyncio only**: rowx awaitables are asyncio futures, so anyio works on
its asyncio backend, and Trio does not.

```python
# test_signup.py
from rowx import query
from rowx.postgres import Pool


async def test_signup(rowx_pool: Pool) -> None:  # with pytest-asyncio or anyio's asyncio backend
    assert await query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool) == 0
```

Configuration:

- The server URL comes from the ini option `rowx_database_url`, else from the variable named by `database_url_env` in
  `[tool.rowx]`. The role needs `CREATEDB`. With no URL the tests skip, or fail when `ROWX_REQUIRE_DB=1`.
- The migrations are `dir` (default `migrations/`) from `[tool.rowx.migrate]` in the `pyproject.toml` at pytest's
  rootdir or above it. Keep a `[tool.pytest.ini_options]` table next to it so pytest picks that directory as its
  rootdir. A missing migrations directory fails the session instead of giving tests an empty database.
- `--rowx-keep-templates` keeps stale templates instead of dropping them.

How the template works:

- The plugin migrates **once per session**, into a template database named `rowx_tpl_<hash>`. The hash covers the
  history table, `create_schemas` and the `(version, checksum)` list of the forward migrations, so editing a migration
  builds a new template, and an unchanged directory reuses the existing one across sessions and across processes that
  share the server.
- A template counts as complete once its migrations ran and it was marked with a database comment, so a build that died
  part way is replaced. Building a new template drops stale `rowx_tpl_*` databases that no session is using.
- Each test runs `CREATE DATABASE rowx_test_<hex> TEMPLATE rowx_tpl_<hash>` and drops the clone with `WITH (FORCE)`
  afterwards, so no test sees another's rows.
- Roles are cluster-wide, so a test that creates one isn't isolated by the clone.

```python test
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

CONFTEST = 'pytest_plugins = ["rowx.testing"]\n'
PYPROJECT = """
[tool.rowx]
database_url_env = "DATABASE_URL"

[tool.rowx.migrate]
dir = "migrations"
"""
TEST = """
import asyncio

from rowx import query


def test_each_test_gets_a_clean_migrated_database(rowx_pool):
    async def work():
        await query(t"INSERT INTO accounts (id) VALUES (1)").execute(rowx_pool)
        return await query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool)

    assert asyncio.run(work()) == 1


def test_another_test_starts_empty(rowx_pool):
    count = asyncio.run(query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool))
    assert count == 0
"""

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "migrations").mkdir()
    (root / "migrations" / "0001_accounts.sql").write_text("CREATE TABLE accounts (id int PRIMARY KEY);\n")
    (root / "conftest.py").write_text(CONFTEST)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "test_accounts.py").write_text(TEST)
    # the plugin wants a server URL; the DSN of this page's tests pins a search_path, so its admin URL stands in
    server_url = os.environ["ROWX_TEST_DSN"]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=root,
        env={**os.environ, "DATABASE_URL": server_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

A complete project, with migrations, an application module, drift-checked queries and a `conftest.py`, is in
[`examples/migrations`](../examples/migrations).

## The checker

```text
rowx check [PATHS...] [--database-url URL] [--config FILE] [--output-format text|json] [--statistics] [-j N] [--scratch] [--no-drift-check]
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

`[tool.rowx.migrate]` is described under [Migrations](#migrations).

The database URL comes from `--database-url`, else from the environment variable named by `database_url_env`.

### Safety

- The checker performs Parse/Describe, catalog lookups and `EXPLAIN` only, never `ANALYZE`. It does not execute your
  statements.
- Postgres may still **evaluate user-defined IMMUTABLE and STABLE functions while planning**, and it takes locks on
  referenced relations.
- Run it as a dedicated non-superuser role against a CI or disposable database.

With `--scratch` the statement is amended:

- The checker still never imports or executes your Python, and never executes your checked queries.
- It does execute the **migration files**, and only in the database it created for the run, as the connecting role.
  Migration files are trusted code. The role needs `CREATEDB`.

`rowx check --scratch` needs `[tool.rowx.migrate]`. It treats the database URL as a server URL, creates
`rowx_check_<hex>` (`TEMPLATE template0`), runs the migrations in it, checks against it, and drops it `WITH (FORCE)` on
every exit path. The drift gate doesn't apply. A missing `CREATEDB` privilege and a failing migration both exit 2.

The scratch database is tagged with `COMMENT ON DATABASE 'rowx:<pid>@<host>:<unix-ts>'`. At startup the checker drops
any `rowx_check_*` database whose tag is over an hour old and which has no sessions, ignoring errors, so a SIGKILLed or
cancelled CI run doesn't leak its database. Administrative connections use the URL's own database, or `postgres`
(`template1` when the target is `postgres`), and fall back from `postgres` to `template1` where `postgres` is missing or
refused.

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
      # migrate: replace with your migration tool, or use `rowx migrate run` (see Migrations)
      - run: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql
      - run: uv run rowx check src
```

Pin the action versions to commit hashes in your own workflow. Migrate **before** checking: the checker validates
against whatever schema the database has. With SQL file migrations, [`rowx check --scratch`](#checking) builds the
database itself.

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
- **Unsupported types** (multiranges, composites, `xml`, `hstore`, `regclass`, ...) are
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
