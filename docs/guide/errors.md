# Errors

Catch these three. They are what a query raises after it has been built.

| Exception | When |
|---|---|
| `RowNotFound` | `.one` and the result has no row. `.optional` returns `None` instead. |
| `RowShapeError` | The row does not match `T`: the wrong number of columns, or NULL in a non-nullable position. |
| `DecodeError` | A value the driver cannot decode, such as an infinite timestamp. Cast to `text`. |

```python test
import asyncio

from rowx import RowNotFound, query
from rowx.postgres import connect


async def main() -> None:
    async with await connect(DSN) as conn:
        try:
            await query[str](t"SELECT name FROM users WHERE false").one(conn)
        except RowNotFound:
            missing = True
        else:
            missing = False
        assert missing


asyncio.run(main())
```

`.one` on an empty result raises `RowNotFound`. Extra rows are not an error. The method table is
[First query](get-started.md). `RowShapeError` is the column check on [Rows](rows.md). `DecodeError` for infinite
timestamps and the other out-of-range values is [Known limits](../guide.md#known-limits) and
[Decode errors](../reference/type-mapping.md#decode-errors).

`RowNotFound`, `RowShapeError`, and `DecodeError` are defined in `rowx_core.errors`. `rowx` and `rowx.errors`
re-export those same objects, so `from rowx import RowNotFound` is `rowx_core.errors.RowNotFound`. The native error
registry uses those classes. There is no Postgres-specific exception tree. The attributes are
[Errors](../reference/api.md#errors).

Constructing a query raises `TypeError` before any IO: a value that is not a template, a NUL in the static text, a
conversion, an unsupported format spec, or an `:ident` value that is not a non-empty `str` (or a tuple of them)
without a NUL or a control character. When the method runs, the compiler can raise `TypeError` for an `:ident`
outside plain code, a name longer than 63 bytes, or more than 65535 parameters. The rules are
[t-string compilation](../reference/api.md#t-string-compilation).

Other calls:

- Another operation on a connection that is streaming raises `ConnectionBusyError` ([Streaming](streaming.md)).
- A transaction used after it has ended, a pooled connection used after its lease, and a session-encoding change raise
  `InterfaceError` ([Transactions](executors.md#transactions),
  [Deployment caveats](executors.md#deployment-caveats), [Session state](security.md#session-state)).
