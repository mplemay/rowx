# From SQLAlchemy

Postgres only. You have a `Session` or an `AsyncSession`, a mapped class, and either `select(User)` or raw SQL
through `text()` or `tstring()`. The rowx form is `query[T](t"...")` on a `Pool`, a connection, or a transaction.

SQLAlchemy Core is command-oriented SQL. The ORM adds a state-oriented unit of work. rowx is the command. The row
is an ordinary Python type, and `rowx check` prepares the statement against a live Postgres 18 database.
`text()` and `tstring()` send the statement through a DBAPI when the program runs.

`tstring()` accepts a PEP 750 t-string, and it can also interpolate SQLAlchemy expressions. In rowx, `{name}` is a
bound server parameter. A table or column name is an allowlisted `{name:ident}` on `query_unchecked`
([Identifiers](../rules/identifiers.md)).

The three functions below are the shapes this page replaces. They are illustrations, and they are not run.

```python
from sqlalchemy import select, text, tstring
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str | None]


async def by_select(session: AsyncSession, name: str) -> User:
    return (await session.scalars(select(User).where(User.name == name))).one()


async def by_text(session: AsyncSession, name: str) -> tuple[int, str, str | None]:
    row = (
        await session.execute(
            text("SELECT id, name, email FROM users WHERE name = :name"),
            {"name": name},
        )
    ).one()
    return row.id, row.name, row.email


async def by_tstring(session: AsyncSession, name: str) -> tuple[int, str, str | None]:
    row = (await session.execute(tstring(t"SELECT id, name, email FROM users WHERE name = {name}"))).one()
    return row.id, row.name, row.email
```

The same read, the posts a `relationship()` would have loaded, and a transaction you roll back:

```python test
import asyncio
from dataclasses import dataclass

from rowx import query
from rowx.postgres import Pool


@dataclass(frozen=True, slots=True)
class User:
    id: int
    name: str
    email: str | None = None


async def main() -> None:
    async with Pool(DSN) as pool:
        alice = await query[User](
            t"SELECT id, name, email FROM users WHERE name = {'alice'}",
        ).one(pool)
        assert alice.name == "alice"
        assert alice.email == "alice@example.com"

        titles = await query[str](
            t"""
            SELECT p.title
            FROM posts p
            JOIN users u ON u.id = p.user_id
            WHERE u.name = {'alice'}
            ORDER BY p.title
            """,
        ).all(pool)
        assert titles == ["alice@example.com", "hello from alice"]

        async with pool.transaction() as tx:
            updated = await query(
                t"UPDATE users SET email = {'moved@example.com'} WHERE name = {'alice'}",
            ).execute(tx)
            assert updated.rows_affected == 1
            again = await query[User](
                t"SELECT id, name, email FROM users WHERE name = {'alice'}",
            ).one(tx)
            assert again is not alice
            assert again.email == "moved@example.com"
            await tx.rollback()

        still = await query[User](
            t"SELECT id, name, email FROM users WHERE name = {'alice'}",
        ).one(pool)
        assert still.email == "alice@example.com"


asyncio.run(main())
```

A second query returns the row that statement produced. `again is not alice` is that fact: nothing remembers the
first instance, and nothing flushes a mutation you made on it. Write the `UPDATE`. `conn.transaction()` is the same
helper when you already hold a connection. A clean exit commits. An exception rolls back. `rollback()` ends the
transaction early.

## Mapping

| SQLAlchemy | rowx |
|---|---|
| `Session` or `AsyncSession` | the `Pool`, connection, or transaction you pass in |
| mapped class | a dataclass or a pydantic model, used as `T` |
| `select(User)` | `query[User](t"SELECT ...")` |
| `text()` or `tstring()` | that SQL as the t-string on `query[T]` |
| `scalar_one` / `scalar_one_or_none` | `.one` / `.optional` |
| `session.begin()` | `async with conn.transaction() as tx` or `pool.transaction()` |
| `create_async_engine` | `Pool` from `rowx.postgres` |
| a second lookup that hit the identity map | a second query you wrote |
| `relationship()` | a `JOIN`, or a second statement |
| Alembic autogenerate | SQL files and `rowx migrate` |

`.one` raises `RowNotFound` when the result has no row. `.optional` returns `T | None`. `.all` returns `list[T]`.
`.execute` returns `QueryResult` and exposes `rows_affected`.

`Pool` and `connect` come from `rowx.postgres`. `Pool` imported from `rowx` is the executor protocol. Open a pool
with `async with Pool(dsn)`, or open one connection with `async with await connect(dsn)`.

Alembic autogenerate has no rowx equivalent. `rowx migrate add` writes a SQL file, and `rowx migrate` applies those
files. The files are SQL, not generated models ([Migrations](migrations.md)).

## What you give up, and what you keep

You give up the unit of work, the identity map, and relationship loading.

You keep the SQL, transactions, a pool, and a row that is a dataclass or a pydantic model.

Stay with SQLAlchemy when the application uses more than Postgres, when you are keeping a large unit of work, or
when callers depend on one object identity across queries.

## Pydantic row

rowx does not depend on pydantic. The application installs pydantic 2.11 or newer. A `BaseModel` row is validated
the way `Model.model_validate` validates a dict keyed by column name. A validator failure is `RowConstructError`.

Declare the column's Python type on the field. The checker compares that type with the column and does not apply
pydantic's lax coercions. `EmailStr`, `AnyUrl`, and `SecretStr` are not row fields. A constraint such as
`Field(gt=0)` is metadata on the column's type.

```python test
import asyncio
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from rowx import query
from rowx.postgres import connect


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: Annotated[int, Field(gt=0)]
    name: str
    email: str | None = None


async def main() -> None:
    async with await connect(DSN) as conn:
        alice = await query[User](
            t"SELECT id, name, email FROM users WHERE name = {'alice'}",
        ).one(conn)
        assert alice.name == "alice"
        assert alice.email == "alice@example.com"
        missing = await query[User](
            t"SELECT id, name, email FROM users WHERE name = {'missing'}",
        ).optional(conn)
        assert missing is None


asyncio.run(main())
```

A FastAPI path operation can return this model. The app that holds the pool is [FastAPI](fastapi.md).
