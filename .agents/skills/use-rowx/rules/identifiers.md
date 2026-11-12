# Identifiers

See [Security](../references/security.md).

## Contents

- A name is not a parameter
- `{name:ident}` belongs to `query_unchecked`
- Allowlist before quoting

---

## A table or column cannot be a parameter

`{table}` in `query` is a bound value, not an identifier. `query` also rejects `{name:ident}`: `:ident` is a format
spec, and `query` raises `TypeError` for an unsupported format spec. The checker reports that as `dynamic-query`.

Incorrect:

```python
from rowx import query


async def column_values(conn, table: str, column: str) -> list[str]:
    return await query[str](t"SELECT {column:ident} FROM {table}").all(conn)
```

Correct:

```python
from enum import StrEnum

from rowx import query_unchecked
from rowx.postgres import Connection


class UserColumn(StrEnum):
    NAME = "name"
    EMAIL = "email"


def parse_column(raw: str) -> UserColumn:
    return UserColumn(raw)


async def column_values(conn: Connection, raw: str) -> list[str]:
    column = parse_column(raw)
    return await query_unchecked[str](t"SELECT {column:ident} FROM users ORDER BY 1").all(conn)
```

## Quoting is not an allowlist

`{name:ident}` is only for `query_unchecked`, and only after the application allowlists the name. Quoting doubles
quotes and rejects an empty name, a name over 63 bytes, a control character, or an `:ident` inside a literal or
comment. It does not decide which names are allowed. A caller who can choose `ORDER BY` can learn the order of a
column the statement does not return. Parse the raw input at the edge. `StrEnum` raises `ValueError` for an unknown
value. A `Literal` needs a `match` with `assert_never`.

Incorrect:

```python
from rowx import query_unchecked


async def sorted_names(conn, column: str) -> list[str]:
    return await query_unchecked[str](t"SELECT name FROM users ORDER BY {column:ident}").all(conn)
```

Correct:

```python
from enum import StrEnum

from rowx import query_unchecked
from rowx.postgres import Connection


class SortColumn(StrEnum):
    NAME = "name"
    EMAIL = "email"


def parse_sort(raw: str) -> SortColumn:
    return SortColumn(raw)


async def sorted_names(conn: Connection, raw: str) -> list[str]:
    column = parse_sort(raw)
    return await query_unchecked[str](t"SELECT name FROM users ORDER BY {column:ident}").all(conn)
```
