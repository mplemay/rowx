# Migrations

See [Migrations](../references/migrations.md), [the migrations guide](../../../../docs/guide/migrations.md), and
[the migrations reference](../../../../docs/reference/migrations.md).

## Contents

- `rowx migrate add` needs no backend
- Validate files before a destructive reset

---

## Adding a migration

`rowx migrate add "add email"` writes the next file with no database and no backend, including when the migration
directory already exists and has to be read to choose the next version. `run`, `revert`, `skip`, and `info` need a
backend. Do not open a connection to invent the next version.

Incorrect:

```python
from rowx import query
from rowx_postgres import connect


async def next_version(dsn: str) -> int:
    async with await connect(dsn) as conn:
        current = await query[int](t"SELECT coalesce(max(version), 0) FROM _rowx_migrations").one(conn)
        return current + 1
```

Correct:

```sh
rowx migrate add "add email"
```

The correct way to add a file is the command `rowx migrate add "add email"`, not a query against the history table.
`Migrator` reads and validates the files when it is constructed, with no database IO. A bad file raises
`MigrationSourceError`.

## Validate before reset drops

`rowx database reset` and `rowx database setup` construct the migrator, which reads and validates the files, before
they create, drop, or change the database. A bad directory fails `reset` before the drop. `rowx database drop` does
not read migration files.

Incorrect:

```python
from rowx import query
from rowx_postgres import Migrator, connect


async def reset(dsn: str, directory: str) -> None:
    async with await connect(dsn) as conn:
        await query(t"CREATE SCHEMA IF NOT EXISTS app").execute(conn)
        await Migrator(directory).run(conn)
```

Correct:

```sh
# validates the directory before the drop; setup does the same before it creates or changes the database
rowx database reset
rowx database setup
```
