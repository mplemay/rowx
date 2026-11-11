# Queries

What `query` and `query_unchecked` accept is in [Writing queries](../../../../docs/guide/query.md). Methods are in
[Get started](../../../../docs/guide/get-started.md). `fetch` is in [Streaming](../../../../docs/guide/streaming.md).

## Shape

A query is `query[T](t"...")` with one inline t-string. Call `.all(conn)`, `.one(conn)`, `.optional(conn)`,
`.execute(conn)`, or `.fetch(conn)`. `all`, `one`, `optional`, and `execute` compile and run when called.

`fetch` compiles and returns a `RowStream`. The statement runs on the first iteration. There is no IO until iteration.

A later call on another executor compiles again. Construction selects no compiler.

```python
from rowx import query
from rowx_postgres import connect


async def user_names(dsn: str) -> list[str]:
    async with await connect(dsn) as conn:
        return await query[str](t"SELECT name FROM users ORDER BY name").all(conn)
```

`query` rejects conversions, format specs, and nested templates. Dynamic SQL, nested templates, and `{name:ident}`
belong to `query_unchecked`. The checker does not describe `query_unchecked` SQL. It still resolves `T`.

Both `from rowx import query` and `from rowx_core import query` are checked. The checker does not import the
application and does not infer which executor will run the query.
