# First query

Install the checker and the PostgreSQL backend, then run a query.

```sh
uv add "rowx[postgres]"
```

**Import `Pool` from `rowx.postgres`.** `Pool` imported from `rowx` is the executor protocol, not a connection pool.
The three distributions are [Packages](../packages.md).

If you already shipped 0.2, the imports on this page are the ones you have. The wheel split is
[Upgrading from 0.2](../packages.md#upgrading-from-02).

The `python test` blocks on this page run against a seeded database (users `alice`, `bob`, and `carol`, plus `orders`).
The test suite sets `DSN`.

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

`{uid}` is a bound parameter. `UserRow` is the row type. `.one` returns that tuple.

You now have a query the checker and your type checker both see. A dataclass row is [Rows](rows.md). A `Session` and
`select()` are [From SQLAlchemy](../from-sqlalchemy.md). The check command is [Check](checker.md).

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
