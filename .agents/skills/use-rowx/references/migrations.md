# Migrations

File format, the CLI, and safety rules are in [Migrations](../../../../docs/guide/migrations.md). Filename grammar,
the plan, and validation order are in [the migrations reference](../../../../docs/reference/migrations.md).

## Add without a backend

`rowx migrate add "add email"` writes the next file with no database and no backend, including when the directory
already exists and has to be read to choose the next version. `run`, `revert`, `skip`, and `info` need a backend.

## Validate before a destructive reset

`Migrator(directory)` reads and validates the files when it is constructed, with no database IO. A file that does
not match the grammar, a duplicate version, an unpaired `.up.sql` or `.down.sql`, or a file that is not UTF-8 raises
`MigrationSourceError`.

`rowx database reset` and `rowx database setup` construct that migrator before they create, drop, or change the
database. A bad directory fails `reset` before the drop. `rowx database drop` does not read migration files.

```python
from rowx_postgres import Migrator, connect


async def apply(dsn: str, directory: str) -> None:
    migrator = Migrator(directory)
    async with await connect(dsn) as conn:
        await migrator.run(conn)
```

Migration files are trusted SQL. Checksums are the SHA-384 of the raw bytes. With `[tool.rowx.migrate]` present,
`rowx check` compares the database with the directory before it describes queries. A drift or scratch failure
exits 2.
