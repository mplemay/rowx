# Quickstart

Distributions are in [Packages](packages.md).

## Install

Author and checker work:

`uv add "rowx[postgres]"`

Runtime-only code, with no `rowx` command:

`uv add rowx-core rowx-postgres`

Python is `>=3.14`. `rowx-core` has no runtime dependencies.

## First query

```python
import asyncio

from rowx import query
from rowx.postgres import connect


async def user_names(dsn: str) -> list[str]:
    async with await connect(dsn) as conn:
        return await query[str](t"SELECT name FROM users ORDER BY name").all(conn)


asyncio.run(user_names("postgresql://app@localhost/app"))
```

`one` returns `T` and raises `RowNotFound` when there is no row. `optional` returns `T | None`. `all` returns
`list[T]`. `fetch` streams. `execute` returns `QueryResult` and exposes `rows_affected`. The same methods take a
pool or a transaction.

Check the files against an explicit database:

`rowx check src --database-url postgresql://app@localhost/app`

The URL can also come from `DATABASE_URL`. The checker does not import the application.
