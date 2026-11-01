# rowx

Typesafe raw SQL for Python 3.14+. No ORM: you write SQL as a [PEP 750](https://peps.python.org/pep-0750/) t-string and
say what a row looks like. A Rust driver built on tokio-postgres runs it, and `rowx check` validates every query against
a live Postgres 18 database before you ship.

## Quickstart

```python test
import asyncio

from rowx.postgres import Pool
from rowx import query

type UserRow = tuple[int, str, str | None]  # id, name, email


async def main() -> None:
    async with Pool(DSN, max_size=10) as pool:
        name = "alice"
        user = await query[UserRow](t"SELECT id, name, email FROM users WHERE name = {name}").one(pool)
        assert user[1] == "alice"

        names = await query[str](t"SELECT name FROM users ORDER BY name").all(pool)
        assert names == ["alice", "bob", "carol"]

        ids = [user[0]]
        updated = await query(t"UPDATE users SET email = {None} WHERE id = ANY({ids})").execute(pool)
        assert updated.rows_affected == 1


asyncio.run(main())
```

- `Pool` is safe to create at module level and acquires a connection per call. Use `rowx.postgres.connect(dsn)` for a
  single connection, and `pool.transaction()` for transactions.
- Interpolations (`{name}`) are always bound as server-side parameters. They are never pasted into the SQL text.
- `query[T]` states the row type. `one` returns `T`, `optional` returns `T | None`, `all` returns `list[T]`, and
  `execute` returns a `QueryResult` with `rows_affected`.
- Type checkers (pyright, mypy, ty) infer all of this with no plugin.

Check the queries against your database:

```sh
rowx check src --database-url postgresql://app@localhost/app
```

`rowx check` exits 0 when every query matches the database, 1 when it reports diagnostics, and 2 on a usage or
connection error.

## Documentation

- [User guide](docs/guide.md): row types, pools, transactions, migrations, writing queries, overrides, the checker, CI
  and poolers.
- [Postgres type mapping](docs/types.md): what each Postgres type decodes to and which Python types can be bound.
- [Known limits](docs/known-limits.md): where nullability inference is less precise than the database.
- [Design](docs/design/README.md): the full specification.
