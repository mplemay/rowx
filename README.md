# rowx

rowx is for when you would otherwise reach for SQLAlchemy and you would rather write the SQL. A query is a
[PEP 750](https://peps.python.org/pep-0750/) t-string plus the Python type of the row.

`rowx check` prepares each checked `query` against a live Postgres 18 database. It reports a mismatch before the
program runs. There is no session and no query builder.

- **SQL stays SQL.** You write the statement you would write in psql. `{name}` is a bound parameter. It is never pasted
  into the SQL text.
- **The row is a normal type.** A tuple, a scalar, a dataclass, a `NamedTuple`, a `TypedDict`, or a pydantic model.
  pyright, mypy, and ty see that type with no plugin.
- **The database checks it.** `rowx check` asks Postgres to prepare and describe the statement, then compares the
  columns and parameters with the row type.
- **Postgres, async, one driver.** Python 3.14+, `asyncio`, and `rowx.postgres`. There is no dialect layer.

Postgres only. One statement per query. `query_unchecked` SQL is not described. There is no ORM and no generated query
functions. The boundary is [Why rowx](docs/why.md) and [Known limits](docs/known-limits.md).

## Quickstart

Install the checker and the PostgreSQL backend with `uv add "rowx[postgres]"`.

```python test
import asyncio

from rowx import query
from rowx.postgres import Pool

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

You now have:

- a parameter (`{name}`) bound on the server
- a row type (`UserRow`) that the checker and your type checker both see
- `.one`, `.all`, and `.execute` (`.optional` when the row may be missing)
- a pool (`Pool`) or one connection (`from rowx.postgres import connect`)
- a check that fails the build when the schema and `UserRow` disagree

A dataclass, `NamedTuple`, `TypedDict`, or pydantic model matches columns by name. The shapes are on
[Rows](docs/guide/rows.md).

```python test
import asyncio
from dataclasses import dataclass

from rowx import query
from rowx.postgres import connect


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

`Pool` imported from `rowx` is the executor protocol, not a connection pool. Import it from `rowx.postgres`. The three
distributions are [Packages](docs/packages.md).

Check the queries against your database:

```sh
rowx check src --database-url postgresql://app@localhost/app
```

`rowx check` exits 0 when it reports no diagnostics, including when no queries were found, 1 when it reports one or
more diagnostics, and 2 when the check could not run. The [checker reference](docs/reference/checker.md) is the contract
for the exit codes.

A type mismatch is reported like this:

```text
error[type-mismatch]: column 2 "name": column type is `int` but the annotation is `str`
  --> src/app.py:14:22
   |
14 |     query[tuple[int, str]](t"SELECT id, name FROM users")
   |                      ^^^ element 2
```

## Next

- [First query](docs/guide/get-started.md): install, then `SELECT`.
- [From SQLAlchemy](docs/from-sqlalchemy.md): a `Session`, `select()`, and `text()` / `tstring()`, and what replaces
  them.
- [Why rowx](docs/why.md): the decision, then SQLx, sqlc, asyncpg, and psycopg.

The rules of a check are in the [checker guide](docs/guide/checker.md). The
[use-rowx skill](.agents/skills/use-rowx/SKILL.md) is agent guidance for queries, the package split, and `rowx check`.
The [changelog](docs/changelog.md) is what changed in each release. The [user guide](docs/guide.md) is the rest of the
job.
