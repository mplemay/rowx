# Get started

Install the checker and the PostgreSQL backend with:

```sh
uv add "rowx[postgres]"
```

`rowx` depends on matching `rowx-core`, and the `postgres` extra installs matching `rowx-postgres`.
`from rowx import query` and `from rowx_core import query` are the same object. Connections are
`from rowx_postgres import connect, Pool`. `import rowx` loads no backend.

A standalone application installs matching `rowx-core` and `rowx-postgres`. That pair is the query API and the
driver. It does not include the `rowx` command, which is the `rowx` distribution above.

```sh
uv add rowx-core rowx-postgres
```

The published 0.2.0 wheel and this layout are compared in [Upgrading from 0.2](#upgrading-from-02).

The `python test` blocks on this page are executed against a seeded database (users `alice`, `bob` and `carol`, plus
`orders`) by the test suite, with `DSN` set to its connection string.

```python test
import asyncio

from rowx_postgres import connect
from rowx_core import query


async def main() -> None:
    async with await connect(DSN) as conn:
        assert await query[int](t"SELECT 1").one(conn) == 1


asyncio.run(main())
```

## Quickstart

```python test
import asyncio

from rowx_postgres import connect
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

from rowx_postgres import connect
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

The unreleased 0.3.0 split ships three lockstep wheels. `rowx-core` is pure Python, imports as `rowx_core`, and has
no runtime dependencies. `rowx-postgres` imports as `rowx_postgres`, its native module is `rowx_postgres._core`, and
it depends only on matching `rowx-core`. `rowx` is pure Python, imports as `rowx`, and depends on matching
`rowx-core`. The `postgres` extra installs matching `rowx-postgres`. The version in this tree is still 0.2.0 until
[#270](https://github.com/mplemay/rowx/issues/270). The first published split is 0.3.0. It is not on PyPI.

`rowx.postgres` remains a compatibility re-export of `rowx_postgres`. It still needs the `postgres` extra. The
import this guide uses is `from rowx_postgres import connect, Pool`.

`query(...)` captures a neutral template and selects no compiler. Each call compiles with `compiler.compile` on the
executor passed to the method (`packages/rowx-core/src/rowx_core/_query.py`). A later call on another executor
compiles again. See [Executors](executors.md).

Generic template rejects happen in the `query` constructor in `rowx_core`: a non-template, a NUL in static text, a
conversion, and an unsupported format spec. PostgreSQL `$n` placeholders, adjacency padding, quoting, lexical
placement, and the 65535 parameter cap happen when the method runs, in `rowx_postgres._compiler`.

Each exception class is defined in `rowx_core.errors` and re-exported from `rowx` and `rowx.errors` as that same
object. The native error registry uses those classes. There is no Postgres-specific exception tree. See
[Errors](errors.md).
