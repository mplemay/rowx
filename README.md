# rowx

Typesafe raw SQL for Python 3.14+. No ORM: you write SQL as a [PEP 750](https://peps.python.org/pep-0750/) t-string and
say what a row looks like. A Rust driver built on tokio-postgres runs it, and `rowx check` validates every query against
a live Postgres 18 database before you ship.

## Quickstart

```python test
import asyncio

import rowx
from rowx import query

type UserRow = tuple[int, str, str | None]  # id, name, email


async def main() -> None:
    async with await rowx.connect(DSN) as conn:
        name = "alice"
        user = await query[UserRow](t"SELECT id, name, email FROM users WHERE name = {name}").fetch_one(conn)
        assert user[1] == "alice"

        names = await query[str](t"SELECT name FROM users ORDER BY name").fetch_all(conn)
        assert names == ["alice", "bob", "carol"]

        ids = [user[0]]
        updated = await query(t"UPDATE users SET email = {None} WHERE id = ANY({ids})").execute(conn)
        assert updated == 1


asyncio.run(main())
```

- Interpolations (`{name}`) are always bound as server-side parameters. They are never pasted into the SQL text.
- `query[T]` states the row type. `fetch_one` returns `T`, `fetch_all` returns `list[T]`, and `execute` returns the
  number of rows affected.
- Type checkers (pyright, mypy, ty) infer all of this with no plugin.

Check the queries against your database:

```sh
rowx check src --database-url postgresql://app@localhost/app
```

`rowx check` exits 0 when every query matches the database, 1 when it reports diagnostics, and 2 on a usage or
connection error.

## Documentation

- [User guide](docs/guide.md): row types, writing queries, overrides, transactions, the checker, CI and poolers.
- [Postgres type mapping](docs/types.md): what each Postgres type decodes to and which Python types can be bound.
- [Known limits](docs/known-limits.md): where nullability inference is less precise than the database.
- [Design](docs/design.md): the full specification.
