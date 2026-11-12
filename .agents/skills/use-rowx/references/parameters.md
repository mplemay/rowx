# Parameters

## Rules

- `{name}` is always a server parameter. It is never pasted into the SQL text.
- Bind a list with `= ANY({ids})`. Do not build an `IN` list of placeholders. An empty list is one parameter.
- Bind the whole pattern. `LIKE '%{q}%'` leaves the marker inside the literal and raises `InterfaceError`. The
  checker reports `param-count-mismatch`.
- Do not write `$n` by hand. Reusing an expression creates two parameters.
- Cast a parameter whose type the server cannot infer, for example `{value}::int IS NULL`.

```python
from rowx import query


async def selected(conn, ids: list[str]) -> list[str]:
    return await query[str](t"SELECT name FROM users WHERE name = ANY({ids}) ORDER BY name").all(conn)
```

Nullability overrides are quoted aliases: `AS "col!"` forces NOT NULL, and `AS "col?"` forces nullable. A NULL in a
`!` column still raises `RowShapeError` at runtime.
