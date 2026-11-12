# Get started

Install the checker and the PostgreSQL backend with:

```sh
uv add "rowx[postgres]"
```

`rowx` depends on matching `rowx-core`, and the `postgres` extra installs matching `rowx-postgres`.
With that install, import the query API from `rowx` and connections from `rowx.postgres`.
`from rowx import query` and `from rowx_core import query` are the same object.
`Pool` imported from `rowx` is the executor protocol, not a connection pool. `import rowx` loads no backend.

A standalone application installs matching `rowx-core` and `rowx-postgres`. That pair is the query API and the
driver. It does not include the `rowx` command, which is the `rowx` distribution above, and it imports `rowx_core`
and `rowx_postgres`.

```sh
uv add rowx-core rowx-postgres
```

The published 0.2.0 wheel and this layout are compared in [Upgrading from 0.2](#upgrading-from-02).

The `python test` blocks on this page are executed against a seeded database (users `alice`, `bob` and `carol`, plus
`orders`) by the test suite, with `DSN` set to its connection string.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query


async def main() -> None:
    async with await connect(DSN) as conn:
        assert await query[int](t"SELECT 1").one(conn) == 1


asyncio.run(main())
```

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
| `fetch` | `RowStream[T]` | Streams the rows. See [Streaming](streaming.md). |
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
The import this guide uses is `from rowx import query` and `from rowx.postgres import connect, Pool`.

`query(...)` captures a neutral template and selects no compiler. Each call compiles with `compiler.compile` on the
executor passed to the method (`packages/rowx-core/src/rowx_core/_query.py`). A later call on another executor
compiles again. See [Executors](executors.md).

The `query` constructor in `rowx_core` captures the template and selects no compiler. It raises `TypeError` for a
non-template, a NUL in static text, a conversion, an unsupported format spec, and an `:ident` value that is not a
non-empty `str` (or a tuple of them) without a NUL or a control character. When the method runs,
`rowx_postgres._compiler` emits `$n` and the adjacency space, quotes the captured identifier, and raises
`TypeError` for an `:ident` outside plain code, a name longer than 63 bytes, and more than 65535 parameters.

Each exception class is defined in `rowx_core.errors` and re-exported from `rowx` and `rowx.errors`
as that same object. The native error registry uses those classes. There is no Postgres-specific exception tree. See
[Errors](errors.md).
