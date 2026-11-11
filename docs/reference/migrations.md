# Migrations

Migrations are plain `.sql` files in the **same format sqlx uses**, so an existing sqlx project can switch without
renaming files or rewriting its history table.

Shared contracts, filename grammar, checksums and the plan live in `rowx_core` (`packages/rowx-core/src/rowx_core/`
and `crates/rowx-core/src/migrate/`). The concrete `Migrator` and the bound migration, admin and checker services
live in `rowx_postgres`. The `rowx migrate` and `rowx database` commands live in `rowx`
([Migrate and database commands](checker.md#migrate-and-database-commands)). The test plugin is
[rowx.testing](../contributing/testing.md#test-plugin).

```text
migrations/
  20261029120000_create_users.up.sql
  20261029120000_create_users.down.sql
  20261030090000_add_email_index.sql
```

```sql
-- no-transaction
CREATE INDEX CONCURRENTLY users_email ON users (email);
```

`rowx migrate add` writes those files with no database and no backend, including when the directory already exists
and has to be read to choose the next version. `run`, `revert`, `skip` and `info` need a backend. Only connection
establishment is retried; a migration command itself runs once.

## Files

- **Filename grammar.** `<VERSION>_<DESCRIPTION>.sql` is a simple migration. `<VERSION>_<DESCRIPTION>.up.sql` plus
  `<VERSION>_<DESCRIPTION>.down.sql` is a reversible one. `VERSION` is ASCII digits parsed as an `i64`: 0 or more,
  leading zeros allowed, so `1_a.sql` and `001_b.sql` are a duplicate version. A sign or any other character is an
  error. The description is the rest of the name, with `_` read as a space. Simple and reversible migrations can be
  mixed.
- **Which files are read.** Only regular files directly in the directory, following symlinks. Subdirectories are
  ignored. A file that does not end in `.sql` (case-sensitive) is ignored.
- **Resolution errors.** Each is a `MigrationSourceError` that names the file: a `.sql` file that does not match the
  grammar, two migrations with the same version, an unpaired `.up.sql` or `.down.sql`, or a file that is not UTF-8.
  There is no warn or ignore setting. sqlx tolerates an unpaired up or down file; rowx does not.
- **`-- no-transaction`.** A byte prefix of the file (`starts_with`, as sqlx does) opts the file out of rowx's
  `BEGIN`/`COMMIT`. `-- no-transaction\r\n` and `-- no-transaction (concurrent index)` both count, and a leading BOM
  disables it. The file is still sent as **one** simple Query. Postgres runs a multi-statement Query as one implicit
  transaction unless the file has its own transaction control.
  - A statement that cannot run in a transaction block (`CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`,
    `REINDEX … CONCURRENTLY`, `VACUUM`) **MUST** be the only statement in its file.
  - `ALTER TYPE … ADD VALUE` followed by a use of the new value needs the value committed first, so the file is
    `-- no-transaction` and commits in between itself.
- **Checksum.** SHA-384 over the raw file bytes, including a BOM, with no normalisation, so checksums match sqlx byte
  for byte. The history row holds the `.up.sql` checksum. The `.down.sql` checksum is computed and never compared
  against the database. Recommend `migrations/*.sql text eol=lf` in `.gitattributes`. sqlx's `ignored_chars` setting
  is not ported.
- **Paths only.** `Migrator(path)` takes a directory. Loading from package data is
  [#306](https://github.com/mplemay/rowx/issues/306).

## History table

The default name is `_rowx_migrations`. Its columns are those of sqlx's `_sqlx_migrations`:

```sql
version        BIGINT PRIMARY KEY,
description    pg_catalog.text NOT NULL,
installed_on   pg_catalog.timestamptz NOT NULL DEFAULT pg_catalog.now(),
success        BOOLEAN NOT NULL,
checksum       pg_catalog.bytea NOT NULL,
execution_time BIGINT NOT NULL
```

Internal SQL names `pg_catalog` objects explicitly. `text`, `timestamptz` and `bytea` are qualified because
`search_path` can shadow them. `BIGINT` and `BOOLEAN` stay unqualified. `table = "_sqlx_migrations"` adopts a sqlx
history table.

`table` and each `create_schemas` entry are parsed with Postgres identifier rules, at construction, before any IO.
`table` may be schema-qualified (`app._rowx_migrations`).

- An unquoted part must match `[A-Za-z_][A-Za-z0-9_$]*` and is lowercased, so `MySchema.Migrations` finds the table
  sqlx created unquoted.
- A `"…"` part is kept verbatim, with `""` unescaped.
- Each part is at most 63 bytes, and there are one or two parts (a `create_schemas` entry has one).
- Anything else raises `ValueError`. The executor always quotes each part.

`create_schemas` runs `CREATE SCHEMA IF NOT EXISTS` (each name quoted) before the table is created.

## Engine

`run`, `revert`, `skip` and `info` each claim one connection for the whole operation. The GIL is released around the
IO.

- A `Connection` is claimed directly. A `Pool` is acquired with `async with pool.acquire()`, so hooks, timeouts and
  reset apply.
- A `Transaction`, or a `Connection` with an open transaction, raises `InterfaceError`.
- Migrating through PgBouncer in transaction mode is unsupported. The lock is session-level. Use a direct or
  session-mode connection.

### Locking

The lock is the `bigint` overload of `pg_advisory_lock`, with sqlx's key
`0x3d32ad9e * crc32(current_database())` (CRC-32/ISO-HDLC), so a sqlx migrator and a rowx migrator exclude each
other. It is taken before the history table is created and released with `pg_advisory_unlock` at the end. rowx does
not rely on `RESET_QUERY` to release it. `locking=False` turns the lock off.

**`info` and dry runs take no lock and create nothing.** The drift check uses that read
([Drift gate](checker.md#drift-gate-and-scratch)).

### Applying and reverting

A file runs over the simple query protocol, the whole file as one Query message, with no splitting or parsing.

A transactional apply is `BEGIN`, the file, `SAVEPOINT rowx_probe`, then `INSERT` with `success = true` and
`execution_time = -1`, then `COMMIT`. If the file ended rowx's transaction, the probe fails with 25P01 and rowx
raises `MigrationFailedError` before recording anything. What the file itself committed stays committed and
unrecorded, and the error says so. `execution_time` is then updated, in nanoseconds, outside the transaction.

A `-- no-transaction` apply inserts the row with `success = false`, runs the file, then sets `success = true`. A
failure leaves a **dirty** row, and the next run raises `DirtyMigrationError`.

`revert` mirrors apply, using the `.down.sql` file's own `-- no-transaction` line. Transactional revert deletes the
history row inside the transaction. A non-transactional revert marks the row dirty, runs the file, then deletes the
row.

On error or cancellation, the executor sends `ROLLBACK` when a step's transaction may be open, then
`pg_advisory_unlock`. If either fails, the connection is marked broken and a pool discards it. A cancelled flag is
checked before each step. A transactional step rolls back and is not recorded. A non-transactional one is left dirty.
Only a migration file's own SQL, and the probe after it, becomes `MigrationFailedError`. A failure in the lock or
history-table SQL is a plain `DatabaseError`.

After any change, the claimed connection's statement cache is cleared. When `run`, `revert` or `skip` on a `Pool`
changes anything, the pool's schema epoch is bumped as well.

Migrations do not change `statement_timeout` or `lock_timeout`. In a transactional file, `SET LOCAL` scopes a setting
to that migration. A `-- no-transaction` file whose only statement cannot run in a transaction block cannot carry a
`SET`; its timeouts come from the connection.

## Plan rules

**Validation before any change.** The plan checks, in order, for dirty rows, for applied migrations missing locally
(unless `ignore_missing=True`), and for modified checksums. Each check raises before anything runs. `rowx database
reset` and `setup` construct the migrator, which reads and validates the files, before they create, drop or change
the database. A bad directory fails `reset` before the drop.

- **Out of order is an error by default.** A pending migration whose version is lower than the highest applied
  version raises `MigrationOrderError`. `allow_out_of_order=True` applies pending migrations in version order.
- **`run(target=V)`.** `V` must exist locally, or the call raises `ValueError` before any IO. A `V` below the highest
  applied version is `MigrationOrderError` unless `allow_out_of_order`. Migrations up to and including `V` are
  applied. `target=None` applies everything.
- **`revert`.** `None` reverts the latest applied version only, `target=V` every applied version above `V`, and
  `all=True` everything. `target` with `all`, an unknown target and a target above the latest applied version are
  `ValueError`s. A simple migration in range raises `IrreversibleMigrationError` before anything is reverted. An
  applied version in range that is missing locally is `MigrationMissingError` even with `ignore_missing`.
- **`skip`** records pending migrations as applied without running them. It follows `run`'s rules, including
  `dry_run`.
- **`info`** returns one `MigrationStatus` per version from both sides: `applied`, `pending`, `modified`, `missing`
  or `dirty`. A missing history table means every migration is pending. It creates nothing.

`run`, `revert` and `skip` return one `MigrationResult` per migration the call applied, reverted or skipped.
`elapsed` is `None` for a dry run or a skip. With `dry_run=True` nothing is executed and no history table is created.

## Out of scope

- Package-resource migrations ([#306](https://github.com/mplemay/rowx/issues/306)).
- Python-code migrations, repeatable migrations and squashing.
- Per-test SQL fixtures ([#307](https://github.com/mplemay/rowx/issues/307)).
