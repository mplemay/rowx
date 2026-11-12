# Executors

`rowx.postgres.connect(dsn)` returns a `Connection`. Use it as an async context manager, or call `close()`. Pass a
`ConnectOptions` instead of a DSN to set `connect_timeout`, `statement_cache_size`, `server_settings` and more
(`ConnectOptions.from_dsn(dsn, connect_timeout=5)`).

## Pools

A `Pool` is an `Executor`, so every query method takes it in place of a connection. Import it from `rowx.postgres`.
`Pool` imported from `rowx` is the executor protocol, not a connection pool. The three distributions are
[Packages](../packages.md). Coming from a `Session` is [From SQLAlchemy](../from-sqlalchemy.md).

A query captures its template without selecting a database. The connection, pool or transaction passed to a query
supplies its compiler and execution behavior. Construction rejects a bad template, including a bad `:ident` value,
and the compiler emits `$n` and the adjacency space when the method runs and raises for `:ident` placement, the
63-byte name limit, and the parameter cap ([Errors](errors.md)). Static checking separately selects its backend
from the configured database URL, without importing or executing application code.

The constructor does no IO, so a pool is safe to create at module level. `async with pool` warms it to `min_size` and
closes it on exit; the first acquire also opens it.

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
from rowx import PoolTimeoutError


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

On release the pool runs `reset_query` (by default `RESET_QUERY`: `RESET SESSION AUTHORIZATION; RESET ALL; SELECT
pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;`), preceded by a `ROLLBACK` when the
connection may be inside a transaction. When it may not be, the pool puts a short idle check in front of the reset
(default or custom), and a reset that fails the check, because a transaction is unexpectedly open, discards the
connection. The reset resets authorization first and then the settings, so the rest of it runs as the startup role with
the startup `search_path`. It does not `DISCARD ALL` or `DEALLOCATE ALL`, so the statement cache survives. Pass
`reset_query=None` to turn it off; an open transaction is still rolled back before a `reset` hook runs.

`reset_query=None` is unsafe for a pool that serves more than one tenant or role: nothing then clears `SET ROLE`,
`search_path`, temporary tables, advisory locks or custom settings between checkouts.

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
- **What the reset resets:** the role and session authorization (`RESET SESSION AUTHORIZATION`; `RESET ALL` alone
  doesn't cover them), settings, advisory locks, cursors, `LISTEN`s and temporary tables (`DISCARD TEMP`). A cached
  statement that used a dropped temporary table fails on its next use, like any statement against a dropped table.
  A custom `reset_query` replaces all of this, so keep the parts you need.
- **SQL-level `PREPARE` is unsupported across checkouts.** The reset doesn't `DEALLOCATE ALL`, because that would
  destroy the statements rowx caches, so a statement prepared with `PREPARE` may or may not survive into the next
  checkout. Use bound parameters instead.
- **Session encoding drift.** rowx sends UTF-8 and standard-conforming strings, and watches the server's parameter
  reports. If application SQL runs `SET client_encoding` to anything but `UTF8` (or its alias `UNICODE`) or `SET
  standard_conforming_strings = off` (also `SET LOCAL`), the connection fails every later call with `InterfaceError`
  naming the setting. The statement that caused it fails too, instead of returning rows that may be decoded wrongly.
  A pool discards the connection when it is released, so a legitimate `SET standard_conforming_strings = off` costs a
  connection. On a pool with hooks (`reset=`, `setup=` or `init=`), release itself raises after discarding it. A role
  or database default of `standard_conforming_strings = off` makes the first call of every checkout fail. Setting
  `client_encoding = 'UTF8'` again changes nothing. Leaving a `transaction()` block because the body raised keeps the
  body's exception; the failed rollback is attached as a note. This is a backstop with a small window: a statement
  already queued behind the `SET` (for example from another task sharing a connection) may still run first.
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

The SQLAlchemy names for a session, an engine, and a unit of work are on
[From SQLAlchemy](../from-sqlalchemy.md). This table is the driver switch.

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
  `max_size` below the pooler's server pool size. Keep per-tenant state inside a transaction (`SET LOCAL`) there, and
  see [Pool reset](security.md#pool-reset) for why `reset_query=None` is unsafe against Postgres directly.
- **Session mode** keeps a server connection per client connection, so the default reset works as it does against
  Postgres directly.
