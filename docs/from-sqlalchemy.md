# From SQLAlchemy

You have a `Session`, a mapped class, and either `select(User)` or `text()` / `tstring()`. You want the SQL in the
source, a typed row, and a failure when the schema drifts.

rowx is that command. You write the SQL, name the row in ordinary Python, and `rowx check` prepares the statement
against a live Postgres database. There is no session and no identity map.

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
        name = "alice"
        user = await query[User](t"SELECT id, name, email FROM users WHERE name = {name}").one(conn)
        assert user.name == "alice"
        assert user.email == "alice@example.com"


asyncio.run(main())
```

`{name}` is a bound parameter. `.one` returns the dataclass, or raises `RowNotFound` when the result is empty. The
shapes of `T` are on [Rows](guide/rows.md).

The SQLAlchemy shapes this replaces are a sketch. The test suite does not run them.

```python
session.scalars(select(User).where(User.name == "alice")).one()

session.execute(text("SELECT id, name, email FROM users WHERE name = :name"), {"name": "alice"}).one()

session.execute(tstring(t"SELECT id, name, email FROM users WHERE name = {name}")).one()
```

`tstring()` accepts a PEP 750 t-string. It still runs through a DBAPI at runtime. `rowx check` prepares the statement
and compares the result with `User` before the program runs.

## What maps where

| SQLAlchemy | rowx |
|---|---|
| `scalar_one` / `scalar_one_or_none` | `.one` / `.optional` |
| `session.begin()` | `conn.transaction()` or `pool.transaction()` |
| `create_async_engine` | `Pool` from `rowx.postgres` |
| a second lookup that hit the identity map | a second query you wrote |
| `relationship()` | a `JOIN`, or a second statement |
| Alembic autogenerate | SQL files and `rowx migrate` |

Alembic autogenerate writes a migration from mapped classes. `rowx migrate` applies SQL files you write, in the format
sqlx uses. The files are SQL, not generated models. See [Migrations](guide/migrations.md).

Transactions and pools are on [Executors](guide/executors.md#transactions). The same page has a
[Coming from asyncpg](guide/executors.md#coming-from-asyncpg) table, for a driver switch rather than an ORM switch.

## What you give up, and what you keep

SQLAlchemy's overview splits the toolkit in two. Core is command-oriented SQL. The ORM is a state-oriented unit of
work: a session, an identity map, and mutable mapped objects.

You give up the unit of work. A row is a value. Assigning `user.email` changes the Python object. The `UPDATE` is a
statement you write. A second read is a second statement. `relationship()` does not load related rows. You write the
`JOIN`, or you run the next query.

You keep the SQL, the transaction, and the pool. The row can be a dataclass or a pydantic model. pyright, mypy, and ty
see it with no plugin. `rowx check` fails the build when Postgres disagrees with that type.

## When to stay

Stay on SQLAlchemy when the application speaks more than one database, when a large unit of work is the model you are
not rewriting, or when the code depends on identity-map semantics: two lookups return the same instance. rowx is
Postgres only, and one async driver.

The decision in the other direction is [Why rowx](why.md). The first query, with no mapping table, is
[First query](guide/get-started.md).
