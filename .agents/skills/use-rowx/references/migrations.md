# Migrations

File format, the CLI, and safety rules are in [Migrations](../../../../docs/guide/migrations.md). Filename grammar,
the plan, and validation order are in [the migrations reference](../../../../docs/reference/migrations.md).

## Add without a backend

`rowx migrate add "add email"` writes the next file with no database and no backend, including when the directory
already exists and has to be read to choose the next version. `run`, `revert`, `skip`, and `info` need a backend.
Do not open a connection to invent the next version.

```sh
rowx migrate add "add email"
```

## Validate before a destructive reset

`Migrator(directory)` reads and validates the files when it is constructed, with no database IO. A file that does
not match the grammar, a duplicate version, an unpaired `.up.sql` or `.down.sql`, or a file that is not UTF-8 raises
`MigrationSourceError`.

```python
from rowx_postgres import Migrator


def load_migrator(directory: str) -> Migrator:
    return Migrator(directory)
```

`rowx database reset` and `rowx database setup` construct that migrator before they create, drop, or change the
database. A bad directory fails `reset` before the drop. `rowx database drop` does not read migration files.

```sh
# validates the directory before the drop; setup does the same before it creates or changes the database
rowx database reset
rowx database setup
```

Migration files are trusted SQL. Checksums are the SHA-384 of the raw bytes. The drift gate is installed only when
`[tool.rowx.migrate]` is present, `--scratch` is off, and `--no-drift-check` is off. Nothing is compared when no
statement would be described. `--scratch` applies migrations and does not drift-check. A drift or scratch failure
exits 2.
