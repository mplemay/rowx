# Imports

See [Packages](../references/packages.md).

## Contents

- Install the distribution that matches the work
- Import the driver from `rowx.postgres`
- Pass an executor; do not look one up

---

## Install split

Author and checker work uses `uv add "rowx[postgres]"`. That installs the `rowx` command, matching `rowx-core`, and
matching `rowx-postgres`.

Runtime-only code uses `uv add rowx-core rowx-postgres`. That pair has no `rowx` command. `rowx-postgres` depends
only on matching `rowx-core`. It does not depend on the CLI.

## Canonical driver import

With the `rowx` package, import the query API from `rowx` and the driver from `rowx.postgres`.
`rowx.postgres` re-exports `rowx_postgres` and still needs the `postgres` extra. A runtime-only install, which does
not include `rowx`, imports `rowx_core` and `rowx_postgres`. `rowx.query` is `rowx_core.query`, and `Pool` and
`connect` from `rowx.postgres` are the `rowx_postgres` objects.

`Pool` imported from `rowx` or `rowx_core` is the executor protocol, not a connection pool. `rowx.core` does not
export `Pool`.

`connect` is not exported by `rowx.core`, `rowx`, or `rowx_core`.

Import it with `from rowx.postgres import connect`. `from rowx_postgres import connect` is that same object when the
application installs `rowx-postgres` without `rowx`.

Incorrect:

```python
from rowx import Pool, query


async def open_pool(dsn: str) -> Pool:
    async with Pool(dsn) as pool:
        await query[int](t"SELECT 1").one(pool)
        return pool
```

Correct:

```python
from rowx import query
from rowx.postgres import Pool, connect


async def open_pool(dsn: str) -> None:
    async with Pool(dsn) as pool:
        await query[int](t"SELECT 1").one(pool)
    async with await connect(dsn) as conn:
        await query[int](t"SELECT 1").one(conn)
```

`from rowx import query` and `from rowx_core import query` are the same object.
`import rowx` loads no backend.

## Pass the executor

There is no ContextVar and no implicit connection. The object passed to `one`, `optional`, `all`, `fetch`, or
`execute` compiles and runs the query. Annotate that parameter with a `rowx.postgres` connection, pool, or
transaction, or with `Executor` from `rowx`. Do not annotate it as `Any`.

Incorrect:

```python
from contextvars import ContextVar
from typing import Any

from rowx import query

current: ContextVar[Any] = ContextVar("rowx_connection")


async def user_names() -> list[str]:
    return await query[str](t"SELECT name FROM users ORDER BY name").all(current.get())
```

Correct:

```python
from rowx import Executor, query
from rowx.postgres import connect


async def user_names(conn: Executor) -> list[str]:
    return await query[str](t"SELECT name FROM users ORDER BY name").all(conn)


async def open_and_list(dsn: str) -> list[str]:
    async with await connect(dsn) as conn:
        return await user_names(conn)
```
