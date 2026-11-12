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
functions. The boundary is [Why rowx](why.md) and [Known limits](known-limits.md).

Install the checker and the PostgreSQL backend with `uv add "rowx[postgres]"`.

```python test
import asyncio

from rowx import query
from rowx.postgres import connect

type UserRow = tuple[int, str, str | None]  # id, name, email


async def main() -> None:
    async with await connect(DSN) as conn:
        name = "alice"
        user = await query[UserRow](t"SELECT id, name, email FROM users WHERE name = {name}").one(conn)
        assert user[1] == "alice"
        assert user[2] == "alice@example.com"


asyncio.run(main())
```

`{name}` is bound as a parameter.

You now have:

- a parameter and a row type your checker and your type checker both see
- `.one` (and `.optional`, `.all`, `.execute`)
- one connection here, or a `Pool` from `rowx.postgres` when the program keeps one
- a check that fails the build when the schema and `UserRow` disagree

A dataclass row is the same query with a class for `T`. The shapes are on [Rows](guide/rows.md).

```sh
rowx check src --database-url postgresql://app@localhost/app
```

`rowx check` exits 0 when it reports no diagnostics, 1 when it reports one or more, and 2 when the check could not run.
The [checker reference](reference/checker.md) is the contract for the exit codes.

A type mismatch is reported like this:

```text
error[type-mismatch]: column 2 "name": column type is `int` but the annotation is `str`
  --> src/app.py:14:22
   |
14 |     query[tuple[int, str]](t"SELECT id, name FROM users")
   |                      ^^^ element 2
```

## Next

- [First query](guide/get-started.md): install, then `SELECT`.
- [From SQLAlchemy](from-sqlalchemy.md): a `Session`, `select()`, and `text()` / `tstring()`, and what replaces them.
- [Why rowx](why.md): the decision, then SQLx, sqlc, asyncpg, and psycopg.

The [user guide](guide.md) is the rest of the job. The [changelog](changelog.md) is what changed in each release. The
[use-rowx skill](https://github.com/mplemay/rowx/blob/main/.agents/skills/use-rowx/SKILL.md) is agent guidance for
queries, the package split, and `rowx check`.
