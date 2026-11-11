# Parameters

Rules of thumb:

- **IN-lists.** Write `= ANY({seq})` with `seq` a list.
- **Literal braces.** JSON and array literals in SQL are written `{{` and `}}`.
- **No interpolation inside a string literal or comment.** `LIKE '%{q}%'` puts the text `$1` inside the literal. Bind
  the whole pattern instead: `LIKE {pat}`. The checker reports this as `param-count-mismatch`, and at runtime the
  parameter count disagrees and raises `InterfaceError`.
- **Cast parameters whose type the server can't infer.** For example `{x}::int IS NULL`. Without a cast, Postgres fails
  with 42P18 or 42P08.
- **Don't write `$n` by hand.** It causes the same count mismatch.
- **Reusing an expression** (`{a}` twice) creates two parameters.

## Nullability overrides

The checker infers column nullability from the catalog and the query plan. When it can't prove a column is NOT NULL, it
treats it as nullable, and your annotation must say `| None`. Narrow it with a quoted alias:

- `AS "col!"` forces NOT NULL.
- `AS "col?"` forces nullable.

The runtime ignores the suffix for tuples and scalars (rows are positional), so a NULL in a `!` column still raises
`RowShapeError`. For a named row type the suffix is stripped from the name the column is matched under. Postgres
truncates identifiers at 63 bytes, which would drop the suffix; the checker reports that as `invalid-override`.

```python test
import asyncio

from rowx_postgres import connect
from rowx import query


async def main() -> None:
    async with await connect(DSN) as conn:
        n = await query[int](t'SELECT count(*) AS "n!" FROM users').one(conn)
        assert n == 3
        email = await query[str | None](t'SELECT email AS "email?" FROM users WHERE name = {"carol"}').one(conn)
        assert email is None or isinstance(email, str)


asyncio.run(main())
```
