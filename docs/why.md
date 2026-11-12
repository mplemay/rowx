# Why rowx

You keep the SQL you would have written in psql, you name the row in ordinary Python, and the build fails when Postgres
disagrees.

A query is a [PEP 750](https://peps.python.org/pep-0750/) t-string plus the Python type of the row. `rowx check` finds
`query[T](t"...")` in your files. The checker never imports or executes application code. It asks Postgres to prepare
and describe each checked statement, using `--database-url` or `DATABASE_URL`, and compares the columns and parameters
with `T`. `query_unchecked` SQL is not described. Described metadata is not written to disk, so a check needs a
database.

- **SQL stays SQL.** The statement in the source is the statement Postgres prepares.
- **The row is a normal type.** A tuple, a scalar, a dataclass, a `NamedTuple`, a `TypedDict`, or a pydantic model.
  pyright, mypy, and ty see it with no plugin.
- **The database checks it.** A mismatch is an error before the program runs. The sample diagnostic is on the
  [home page](index.md).
- **Postgres, async, one driver.** Python 3.14+ and `rowx.postgres`. There is no session and no query builder.

Leaving a `Session` is [From SQLAlchemy](from-sqlalchemy.md). The first query is
[First query](guide/get-started.md).

## Kinship

SQLx is the closest relative. `query!` and `query_as!` check a SQL string against a live database at compile time, and
[`query_as!`](https://docs.rs/sqlx/latest/sqlx/macro.query_as.html) maps the row onto a struct you name. SQLx can also
compile from an offline [`.sqlx` cache](https://docs.rs/sqlx/latest/sqlx/macro.query.html#offline-mode). rowx has no
offline mode. Every check prepares the statements it describes
([checker reference](reference/checker.md#output)).

sqlc generates functions from SQL files. `sqlc generate` analyzes those files with a built-in engine, and a database
connection is optional. rowx leaves the t-string in your source and does not emit a function for it.

asyncpg and psycopg run SQL strings at runtime. psycopg's
[static typing](https://www.psycopg.org/psycopg3/docs/advanced/typing.html) covers the Python API and the rows that come
back. It does not prepare the statement against the schema. asyncpg's `fetchrow` and `execute` send the string when you
call them.

SQLAlchemy is the ORM and the expression toolkit. Core is command-oriented SQL. The ORM adds a state-oriented unit of
work. Raw SQL is a [`text()`](https://docs.sqlalchemy.org/en/21/core/sqlelement.html#sqlalchemy.sql.expression.text) or
[`tstring()`](https://docs.sqlalchemy.org/en/21/core/sqlelement.html#sqlalchemy.sql.expression.tstring) construct, and
`tstring()` accepts a PEP 750 t-string. Both run through a DBAPI at runtime.
[From SQLAlchemy](from-sqlalchemy.md) is that comparison in the shape of a move.

## Comparison

One row per project. The kinship section above is where the offline cache and the generated code belong.

| Project | Where the SQL lives | Live database check | ORM | Driver |
|---|---|---|---|---|
| rowx | An inline t-string on `query[T]` ([Queries](guide/query.md)). | Yes, for a checked statement. | No. | Yes. `rowx-postgres`. |
| [SQLx](https://docs.rs/sqlx/latest/sqlx/) | String literals in [`query!`](https://docs.rs/sqlx/latest/sqlx/macro.query.html) and `query_as!`. | Yes, at compile time, unless offline mode is used. | No. | Yes. [`Postgres`](https://docs.rs/sqlx/latest/sqlx/struct.Postgres.html). |
| [asyncpg](https://magicstack.github.io/asyncpg/current/index.html) | Strings passed to [`execute`](https://magicstack.github.io/asyncpg/current/usage.html) and `fetchrow`. | No. They run at runtime. | No. | Yes. |
| [psycopg](https://www.psycopg.org/psycopg3/docs/) | Strings passed to [`execute`](https://www.psycopg.org/psycopg3/docs/basic/usage.html). | No. Typing covers the Python API, not the statement. | No. | Yes. |
| [SQLAlchemy](https://docs.sqlalchemy.org/en/21/intro.html) | Expression objects, ORM mappings, or `text()` / `tstring()`. | No. They run through a DBAPI at runtime. | Yes. | A dialect over a DBAPI. |
| [sqlc](https://docs.sqlc.dev/en/latest/) | Schema and queries in [SQL files](https://docs.sqlc.dev/en/latest/howto/generate.html). | Optional. Analysis works without a connection. | No. | No. The PostgreSQL tutorial emits code for `pgx`. |

## What rowx does not do

- **Postgres only.** One async driver, on [Packages](packages.md). There is no dialect layer.
- **One statement per query.** A string of several statements fails at prepare.
  [Known limits](known-limits.md#execution-limits).
- **`query_unchecked` is not described.** The checker still resolves `T`. [Checker guide](guide/checker.md).
- **No offline describe cache.** A check needs a database. [Checker reference](reference/checker.md#output).
- **No generated query functions.** The t-string stays in your source. sqlc is the project that emits host code.
- **Dynamic identifiers** are an allowlist plus `{name:ident}` on `query_unchecked`, which quotes the name.
  [Security](guide/security.md#identifiers).
