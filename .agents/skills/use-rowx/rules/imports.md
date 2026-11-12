# Imports

See [Packages](../references/packages.md), [the packages page](../../../../docs/packages.md), and
[Public surface](../../../../docs/reference/api.md#public-surface).

## Contents

- Install the distribution that matches the work
- Import the driver from `rowx_postgres`
- Pass an executor; do not look one up

---

## Install split

Author and checker work uses `uv add "rowx[postgres]"`. That installs the `rowx` command, matching `rowx-core`, and
matching `rowx-postgres`.

Runtime-only code uses `uv add rowx-core rowx-postgres`. That pair has no `rowx` command. `rowx-postgres` depends
only on matching `rowx-core`. It does not depend on the CLI.

## Canonical driver import

`rowx.postgres` re-exports `rowx_postgres` and still needs the `postgres` extra. New code imports `rowx_postgres`.
`Pool` imported from `rowx` or `rowx_core` is the executor protocol, not a connection pool.

`connect` is not exported by `rowx` or `rowx_core`.

Import it with `from rowx_postgres import connect`. `rowx.postgres.connect` is only the compatibility re-export.

Incorrect:

```python
from rowx import Pool, query
from rowx.postgres import connect


async def open_pool(dsn: str) -> Pool:
    async with Pool(dsn) as pool:
        await query[int](t"SELECT 1").one(pool)
        return pool
```

Correct:

```python
from rowx import query
from rowx_postgres import Pool, connect


async def open_pool(dsn: str) -> None:
    async with Pool(dsn) as pool:
        await query[int](t"SELECT 1").one(pool)
    async with await connect(dsn) as conn:
        await query[int](t"SELECT 1").one(conn)
```

`from rowx import query` and `from rowx_core import query` are the same object. `import rowx` loads no backend.

## Pass the executor

There is no ContextVar and no implicit connection. The object passed to `one`, `optional`, `all`, `fetch`, or
`execute` compiles and runs the query. Annotate that parameter with a `rowx_postgres` connection, pool, or
transaction, or with `Executor` from `rowx_core`. Do not annotate it as `Any`.

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
from rowx import query
from rowx_core import Executor
from rowx_postgres import connect


async def user_names(conn: Executor) -> list[str]:
    return await query[str](t"SELECT name FROM users ORDER BY name").all(conn)


async def open_and_list(dsn: str) -> list[str]:
    async with await connect(dsn) as conn:
        return await user_names(conn)
```
