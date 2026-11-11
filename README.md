# rowx

SQLx for Python 3.14+. Typesafe raw SQL, with no ORM: you write SQL as a [PEP 750](https://peps.python.org/pep-0750/)
t-string and say what a row looks like. `rowx check` validates every query against a live Postgres 18 database.

## Quickstart

Install the checker and PostgreSQL backend with `uv add "rowx[postgres]"`.

Free-threaded CPython 3.14t cannot install the abi3 wheels, so pip builds rowx-postgres from the source distribution.

```python test
import asyncio

from rowx import query
from rowx_postgres import Pool

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

- `Pool` is safe to create at module level and acquires a connection per call. Use `rowx_postgres.connect(dsn)` for a
  single connection, and `pool.transaction()` for transactions.
- Interpolations (`{name}`) are always bound as server-side parameters. They are never pasted into the SQL text.
- `query[T]` states the row type. `one` returns `T`, `optional` returns `T | None`, `all` returns `list[T]`, and
  `execute` returns a `QueryResult` with `rows_affected`.
- Type checkers (pyright, mypy, ty) infer all of this with no plugin.

Rows can also be a dataclass, `NamedTuple`, `TypedDict` or pydantic model, matched to the columns by name:

```python test
import asyncio
from dataclasses import dataclass

from rowx import query
from rowx_postgres import connect


@dataclass(slots=True)
class User:
    id: int
    name: str
    email: str | None


async def main() -> None:
    async with await connect(DSN) as conn:
        user = await query[User](t"SELECT id, name, email FROM users WHERE name = {'alice'}").one(conn)
        assert user.name == "alice"


asyncio.run(main())
```

Check the queries against your database:

```sh
rowx check src --database-url postgresql://app@localhost/app
```

`rowx check` exits 0 when every query matches the database, 1 when it reports diagnostics, and 2 on a usage or config
error, an unreachable database, a describe failure that is not an SQL error, or an internal error.

A type mismatch is reported like this:

```text
error[type-mismatch]: column 2 "name": column type is `int` but the annotation is `str`
  --> src/app.py:14:22
   |
14 |     query[tuple[int, str]](t"SELECT id, name FROM users")
   |                      ^^^ element 2
```

The rules are in the [checker guide](docs/guide/checker.md).

## Distributions

`rowx` is the optional CLI and re-exports `query`. `from rowx import query` and `from rowx_core import query` are the
same object.

`rowx-core` (`rowx_core`) has no runtime dependencies. `rowx-postgres` (`rowx_postgres`) depends only on matching
`rowx-core`.

A standalone application installs `rowx-core` and `rowx-postgres` and does not get the `rowx` command:

```sh
uv add rowx-core rowx-postgres
```

The checker uses `--database-url` or `DATABASE_URL` and does not import the application.

`rowx.postgres` remains a compatibility re-export of `rowx_postgres` and still needs the `postgres` extra.

## Documentation

- [User guide](docs/guide.md): row types (tuples, scalars and named classes), pools, transactions, migrations, writing
  queries, overrides, the checker, CI and poolers.
- [Why rowx](docs/why.md): where SQL lives, and how a live database check compares with other libraries.
- [Changelog](docs/changelog.md): what changed in each release.
- [Packages](docs/packages.md): the three distributions and the canonical imports.
- [Postgres type mapping](docs/types.md): what each Postgres type decodes to and which Python types can be bound.
- [Known limits](docs/known-limits.md): where nullability inference is less precise than the database.
- [Reference](docs/reference/index.md): the full specification.
