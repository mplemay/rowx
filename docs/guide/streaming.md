# Streaming

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
from rowx import ConnectionBusyError


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
