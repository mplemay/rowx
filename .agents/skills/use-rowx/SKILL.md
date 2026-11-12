---
name: use-rowx
description: >-
  Write typesafe raw SQL for Postgres with rowx and PEP 750 t-strings, check it with
  rowx check, replace a SQLAlchemy Session with a typed t-string query, and run those
  queries from a FastAPI lifespan and path operation. Use when the user mentions rowx, rowx.core,
  rowx.postgres, rowx_core, rowx_postgres, t-string SQL, a Postgres query through rowx,
  rowx check, moving from SQLAlchemy to raw SQL, or FastAPI with rowx.
license: MIT
compatibility: Requires Python >=3.14
allowed-tools: Bash(uv *)
metadata:
  version: "1.1.0"
  author: rowx
---

# Use rowx

rowx is typesafe raw SQL for Python 3.14+. A query is a PEP 750 t-string with an explicit row type. The executor
you pass compiles and runs it. `rowx check` prepares each checked query against a live Postgres 18 database and
does not import application code.

## When to Use This Skill

Invoke this skill when:

- Writing, reviewing, or debugging a rowx query
- Installing `rowx`, `rowx-core`, or `rowx-postgres`
- Binding parameters, including a list with `= ANY({ids})`
- Choosing `query` or `query_unchecked`, or allowlisting an identifier
- Configuring a pool reset, migrations, or `rowx.testing`
- Running or interpreting `rowx check`
- Replacing a SQLAlchemy `Session`, mapped class, `select()`, `text()`, or `tstring()`
- Holding a `Pool` on a FastAPI lifespan and running a query in a path operation
- Mapping a row onto a dataclass or a pydantic model
- Code imports `rowx`, `rowx.core`, `rowx.postgres`, `rowx_core`, `rowx_postgres`, or `query`

Do **not** use this skill for:

- SQL or Postgres work that does not go through rowx
- Changing rowx internals when this skill already answers the question

## Principles

1. Install `rowx[postgres]` for author and checker work. Runtime-only code installs `rowx-core` and `rowx-postgres`
   and does not install the `rowx` command.
2. Write `query[T](t"...").all(conn)` (also `one`, `optional`, `execute`, and `fetch`). The executor owns
   compilation and execution.
3. `{name}` is always a server parameter. It is never pasted into the SQL text.
4. `rowx check` uses `--database-url` or `DATABASE_URL`. It checks the construction site on `rowx` and `rowx_core`.
5. With the `rowx` package, import `query` from `rowx` and `Pool` and `connect` from `rowx.postgres`. A runtime-only
   install imports `rowx_core` and `rowx_postgres`. `rowx.query` is `rowx_core.query`, and `Pool` and `connect` from
   `rowx.postgres` are the `rowx_postgres` objects. `rowx.core` re-exports only the `rowx_core` names that `rowx` and
   `rowx.errors` do not already export.
6. `rowx-core` has no runtime dependencies. `rowx-postgres` depends only on matching `rowx-core`. The backend does
   not depend on the CLI. There is no ContextVar, no implicit Postgres target, and no `Any` dispatch.
7. Postgres only. There is no unit of work and no identity map. A row is the value this statement returned. A
   relationship is a `JOIN` or a second statement. You keep the SQL, transactions, a pool, and a dataclass or
   pydantic row.

## Critical Rules

These rules are **always enforced**. Each links to Incorrect/Correct pairs.

### Imports → [rules/imports.md](rules/imports.md)

- `from rowx import query` and `from rowx_core import query` are the same object.
- `from rowx.postgres import Pool, connect` is the driver import for the `rowx` package. `rowx_postgres` is that same
  driver when the application does not install `rowx`.
- Pass the executor into the query method. `Pool` on `rowx` and `rowx_core` is a protocol, not a connection pool.
  `rowx.core` does not export `Pool`.

### Parameters → [rules/parameters.md](rules/parameters.md)

- `{name}` is a server parameter.
- Bind a list with `= ANY({ids})`. Do not build an `IN` list of placeholders.

### Identifiers → [rules/identifiers.md](rules/identifiers.md)

- A table or column name cannot be a parameter.
- `{name:ident}` is only for `query_unchecked`, and only after the application allowlists the name.
- Quoting is not an allowlist.

### Pool reset → [rules/reset.md](rules/reset.md)

- `reset_query=None` is unsafe for a pool that serves more than one tenant or role.
- A custom `reset_query` replaces the default entirely.

### Migrations → [rules/migrations.md](rules/migrations.md)

- `rowx migrate add` works without a backend, even when a migration directory already exists.
- `rowx database reset` and `setup` validate migration files before they create, drop, or change the database.

### Diagnostics → [rules/diagnostics.md](rules/diagnostics.md)

- Exit 0 when `rowx check` reports no diagnostics, including when no queries were found.
- Exit 1 when it reports one or more diagnostics. Warnings fail the check unless warning failure is disabled.
- Exit 2 on a bad invocation or config, an unreachable database, a describe failure that is not an SQL error, a
  drift or scratch failure, or an internal error.
- `query_unchecked` SQL is not described.

## Quick start

Author and checker install: `uv add "rowx[postgres]"`.

Runtime-only install, with no `rowx` command: `uv add rowx-core rowx-postgres`.

The blocks below run against the seeded `users` table (`alice`, `bob`, `carol`). `DSN` is supplied by the snippet
runner.

### User names

```python test
import asyncio

from rowx import query
from rowx.postgres import connect


async def main() -> None:
    async with await connect(DSN) as conn:
        names = await query[str](t"SELECT name FROM users ORDER BY name").all(conn)
        assert names == ["alice", "bob", "carol"]


asyncio.run(main())
```

### List binding

```python test
import asyncio

from rowx import query
from rowx.postgres import Pool


async def main() -> None:
    async with Pool(DSN) as pool:
        ids = ["alice", "carol"]
        found = await query[str](
            t"SELECT name FROM users WHERE name = ANY({ids}) ORDER BY name",
        ).all(pool)
        assert found == ["alice", "carol"]


asyncio.run(main())
```

## Task Routing Table

Load only the most relevant reference first.

| I want to... | Reference |
|---|---|
| Install and run a first query | [references/quickstart.md](references/quickstart.md) |
| Choose a distribution or import | [references/packages.md](references/packages.md) |
| Call `query` or `query_unchecked` | [references/queries.md](references/queries.md) |
| Bind parameters or a list | [references/parameters.md](references/parameters.md) |
| Allowlist an identifier or reset a pool | [references/security.md](references/security.md) |
| Add or apply migrations | [references/migrations.md](references/migrations.md) |
| Test against a migrated database | [references/testing.md](references/testing.md) |
| Run `rowx check` and read the exit code | [references/checker.md](references/checker.md) |
| Catch a rowx exception | [references/errors.md](references/errors.md) |
| Replace a SQLAlchemy `Session`, `select()`, `text()`, or `tstring()` | [references/sqlalchemy.md](references/sqlalchemy.md) |
| Hold a `Pool` on a FastAPI lifespan | [references/fastapi.md](references/fastapi.md) |
| Map a row onto a dataclass or a pydantic model | [references/sqlalchemy.md](references/sqlalchemy.md) |

## From SQLAlchemy

Postgres only. `select(User)`, `text()`, and `tstring()` become `query[User](t"SELECT ...")`. You pass a `Pool`, a
connection from `connect`, or a transaction. SQLAlchemy Core is command-oriented SQL. The ORM adds a state-oriented
unit of work. rowx is the command, and `rowx check` prepares the statement. `text()` and `tstring()` run through a
DBAPI at runtime. `{name}` is a bound value. SQLAlchemy's `tstring()` can also interpolate expressions. An
identifier stays on `query_unchecked` after an allowlist.

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
| Alembic autogenerate | SQL files and `rowx migrate` ([Migrations](references/migrations.md)) |

You give up the unit of work, the identity map, and relationship loading. You keep the SQL, transactions, a pool,
and a row that is a dataclass or a pydantic model. Stay when the application uses more than Postgres, when you are
keeping a large unit of work, or when callers depend on one object identity across queries. The runnable
replacement, including a pydantic row, is [From SQLAlchemy](references/sqlalchemy.md).

## FastAPI

Create the `Pool` in the lifespan and store it on `app.state`. Import it from `rowx.postgres`. Leaving the
lifespan's `async with Pool` calls `await pool.close()`. Depend on the pool for one statement. Depend on
`pool.acquire()` or `pool.transaction()` when several statements share a checkout. Call `query[T](t"...")` in the
path operation. `.optional` returns `T | None`. `.one` raises `RowNotFound`. Raise the HTTP error in the path
operation. A yielded transaction commits on a normal return and rolls back when the path operation raises,
including `HTTPException`. Pass `scope="function"` so that exit finishes before the response is sent. The app is
[FastAPI](references/fastapi.md). Test the function the route calls with [Testing](references/testing.md).

## Common Gotchas

- Constructing `Pool` from `rowx` or `rowx_core`. Those names are executor protocols. `rowx.core` does not export
  `Pool`.
- Importing `connect` from `rowx` or `rowx_core`. The connection and the connection pool come from `rowx.postgres`,
  or from `rowx_postgres` when the application does not install `rowx`.
- Calling `.all()` or `.one()` with no executor.
- Pasting a value into SQL, or building `IN ($1, $2)` instead of `= ANY({ids})`.
- Putting `{name}` inside a SQL string literal. Bind the whole pattern.
- Using `{name:ident}` in `query`, or in `query_unchecked` before an allowlist. Quoting does not allowlist a name.
- Setting `reset_query=None` for a pool that serves more than one tenant or role.
- Passing a custom `reset_query` and assuming the default statements still run.
- Expecting `rowx migrate add` to load `rowx_postgres` because a migration directory exists.
- Resetting a database before the migration files have been validated. `rowx database reset` validates first, and a
  bad directory fails before the drop.
- Reading exit 1 as "the database is down" or "no queries were found". No queries is exit 0. An unreachable
  database is exit 2.
- Expecting `rowx check` to describe `query_unchecked` SQL or to import the application.
- Installing `rowx[pytest]` and expecting a database driver. The extra adds pytest. A database test also needs
  `rowx[postgres]` or `rowx-core` and `rowx-postgres`.
- Storing the connection in a ContextVar, falling back to an implicit Postgres target, making `rowx-postgres`
  depend on the CLI, or typing the executor as `Any`.
- Expecting a second query to return the same instance. Each call returns the rows that statement produced.
- Creating a `Pool` at import in a server that forks. Open it in the FastAPI lifespan. A pool used after a fork
  raises `InterfaceError`.
- Leaving `RowNotFound` for FastAPI to turn into a 404. Use `.optional` and raise the HTTP error in the path
  operation.
- Porting a SQLAlchemy `tstring()` interpolation that inserts an expression. `{name}` binds a value.
