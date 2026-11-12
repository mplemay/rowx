# rowx

rowx is typesafe raw SQL for Python 3.14+. A query is a [PEP 750](https://peps.python.org/pep-0750/) t-string with an
explicit row type. A Rust driver built on tokio-postgres runs it, and `rowx check` prepares every query against a live
Postgres 18 database.

```python
from rowx import query
from rowx.postgres import connect

async with await connect(dsn) as conn:
    name = await query[str](t"SELECT name FROM users WHERE id = {user_id}").one(conn)
```

The block above is an illustration. The [user guide](guide.md) has samples the test suite runs.

- [User guide](guide.md): install, queries, rows, executors, the checker, migrations, and testing.
- [Why rowx](why.md): where SQL lives, and how a live database check compares with other libraries.
- [Embedded Postgres](guide/embedded.md): an in-process Postgres 18 for development, checking and tests, with no server.
- [Packages](packages.md): the four distributions and the canonical imports.
- [Postgres type mapping](types.md): what each Postgres type decodes to, and which Python values can be bound.
- [Known limits](known-limits.md): where nullability inference is less precise than the database.
- [Changelog](changelog.md): what changed in each release.
- [use-rowx skill](https://github.com/mplemay/rowx/blob/main/.agents/skills/use-rowx/SKILL.md): agent guidance for
  queries, the package split, and `rowx check`.

Install the checker and the PostgreSQL backend with `uv add "rowx[postgres]"`.
