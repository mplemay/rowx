# Parameters

See [Parameters](../references/parameters.md).

## Contents

- Bind every value
- Bind a list with `= ANY({ids})`
- Keep interpolations out of literals

---

## Values stay parameters

`{name}` is always a server parameter. `query` rejects a non-template, a conversion, a format spec, and a nested
template. There is no raw-SQL escape. Text that looks like SQL is still a value.

Incorrect:

```python
from rowx import query


async def user_id(conn, name: str) -> int | None:
    sql = f"SELECT id FROM users WHERE name = '{name}'"
    return await query[int](sql).optional(conn)
```

Correct:

```python
from rowx import query


async def user_id(conn, name: str) -> int | None:
    return await query[int](t"SELECT id FROM users WHERE name = {name}").optional(conn)
```

## Lists use ANY

Bind a list with `= ANY({ids})`. That is one parameter however long the list is. An empty list is valid.
`IN ({listed})` is still one parameter. Building that list with an f-string and passing the `str` to `query` raises
`TypeError` because `query` requires a template.

Incorrect:

```python
from rowx import query


async def by_name(conn, names: list[str]) -> list[str]:
    listed = ", ".join(f"'{name}'" for name in names)
    sql = f"SELECT name FROM users WHERE name IN ({listed}) ORDER BY name"
    return await query[str](sql).all(conn)
```

Correct:

```python
from rowx import query


async def by_name(conn, ids: list[str]) -> list[str]:
    return await query[str](t"SELECT name FROM users WHERE name = ANY({ids}) ORDER BY name").all(conn)
```

## Literals and hand-written placeholders

`LIKE '%{q}%'` puts the parameter marker inside the literal. Bind the whole pattern. A hand-written `$n` causes the
same parameter-count mismatch: the checker reports `param-count-mismatch`, and at runtime it raises
`InterfaceError`. Reusing `{name}` creates two parameters.

Incorrect:

```python
from rowx import query


async def prefixed(conn, prefix: str) -> list[str]:
    return await query[str](t"SELECT name FROM users WHERE name LIKE '{prefix}%'").all(conn)
```

Correct:

```python
from rowx import query


async def prefixed(conn, prefix: str) -> list[str]:
    pattern = f"{prefix}%"
    return await query[str](t"SELECT name FROM users WHERE name LIKE {pattern}").all(conn)
```
