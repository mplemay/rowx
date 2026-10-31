# Migrations (`rowx.postgres.Migrator`, `rowx migrate`, `rowx database`)

Migrations are plain `.sql` files in the **same format sqlx uses**, so an existing sqlx project can switch to rowx
without renaming files or rewriting its history table. This reverses non-goal "Migrations" in
[Overview](overview.md) and decision 15 in the [Decision log](decision-log.md#design-interview-2026-10-23).

Prior art: sqlx (`sqlx-core::migrate` `Migrator`, `MigrationSource` and `Migrate`; `sqlx-postgres` `migrate.rs`;
`sqlx-cli` `migrate` and `database`; `sqlx.toml` `[migrate]`; `#[sqlx::test]`), sqlty (`schema` as a directory of
migration files, `sqlty breaking`) and Flyway (the out-of-order policy).

```text
migrations/
  20261029120000_create_users.up.sql       # reversible pair
  20261029120000_create_users.down.sql
  20261030090000_add_email_index.sql       # simple (forward-only)
```

```sql
-- no-transaction
CREATE INDEX CONCURRENTLY users_email ON users (email);
```

The Python surface is in [`rowx.postgres`](api.md#rowxpostgres), the commands and config are in
[Migrate and database commands](checker.md#migrate-and-database-commands), and the test plugin is in
[Test plugin](testing.md#test-plugin-rowxtesting).

## Files

- **Filename grammar.** `<VERSION>_<DESCRIPTION>.sql` is a simple migration. `<VERSION>_<DESCRIPTION>.up.sql` plus
  `<VERSION>_<DESCRIPTION>.down.sql` is a reversible one. `VERSION` is a positive `i64`. The description is the rest of
  the name, with `_` read as a space. Simple and reversible migrations can be mixed in one directory.
- **Which files are read.** Only regular files directly in the directory are read, and subdirectories are ignored. A
  file that doesn't end in `.sql` is ignored.
- **Resolution errors.** Each is a `MigrationSourceError` that names the file:
  - a `.sql` file that doesn't match the grammar (sqlx's current default; there is no warn or ignore setting);
  - two migrations with the same version;
  - an `.up.sql` without its `.down.sql`, or the reverse;
  - a file that isn't UTF-8.
- **`-- no-transaction`.** When it is the first line of a file (byte-exact, as in sqlx), rowx sends no `BEGIN` around
  the file ([A `-- no-transaction` migration](#a----no-transaction-migration)). The file is still sent as **one** simple
  Query, and Postgres runs a multi-statement Query as a single implicit transaction unless the file has its own
  transaction control. So:
  - A statement that can't run in a transaction block (`CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`,
    `REINDEX … CONCURRENTLY`, `VACUUM`) must be the **only statement** in its file. Even an explicit `COMMIT` before it
    doesn't help, because each later statement of the same Query opens a new implicit block.
  - `ALTER TYPE … ADD VALUE` followed by a use of the new value needs the value committed first, so the file is
    `-- no-transaction` and commits in between itself, for example `BEGIN; ALTER TYPE … ADD VALUE …; COMMIT;` and then
    `BEGIN; … COMMIT;` for the statements that use it.
- **Checksum of a reversible migration.** The history row holds the `.up.sql` checksum, as in sqlx. The `.down.sql`
  checksum is computed but never compared against the database.
- **Checksum.** SHA-384 over the raw file bytes, with no normalisation, so checksums match sqlx byte for byte. The guide
  recommends `migrations/*.sql text eol=lf` in `.gitattributes`. sqlx's `ignored_chars` setting is not ported.
- **Paths only.** `Migrator(path)` takes a directory. Loading from package data with `importlib.resources` is not part
  of this feature ([Roadmap](roadmap.md#deferred-work-by-expected-value)).

## History table

- The default name is `_rowx_migrations`. Its columns are those of sqlx's `_sqlx_migrations`:

  ```sql
  version        BIGINT PRIMARY KEY,
  description    TEXT NOT NULL,
  installed_on   TIMESTAMPTZ NOT NULL DEFAULT now(),
  success        BOOLEAN NOT NULL,
  checksum       BYTEA NOT NULL,
  execution_time BIGINT NOT NULL
  ```

- **Adopting sqlx.** `table = "_sqlx_migrations"` is the only change. A test runs sqlx-format history rows and checksums
  through rowx.
- **Quoting.** `table` may be schema-qualified (`app._rowx_migrations`). It is parsed into one or two identifiers, and
  each is quoted. Anything else raises `ValueError`. (sqlx interpolates the raw string.)
- **`create_schemas`** runs `CREATE SCHEMA IF NOT EXISTS` (each name quoted) before the table is created, as sqlx does.

## Engine

### Layers

- `rowx-core/src/migrate/` holds what any backend shares, as pure functions with `cargo` unit tests: the filename
  grammar, resolving a directory into sorted `Migration`s, checksums, and the plan. Reading the files is its only IO.
  The plan takes the local migrations, the applied rows and the options, and returns either the steps to run or an
  error.
- `rowx-postgres/src/migrate.rs` carries a plan out over one connection: the lock, the history table, apply, revert and
  skip. `rowx-postgres/src/admin.rs` creates, drops and checks for a database
  ([Database administration](checker.md#migrate-and-database-commands)).
- Python only constructs the migrator, calls it and maps errors.
- Errors are `thiserror` enums in `rowx-core` (source and plan) and `rowx-postgres` (apply). Python exceptions are
  created only in `rowx-py-core/src/errors.rs` (the registry and the core variants) and
  `rowx-postgres-py/src/errors.rs`.

### One claim per run

`run`, `undo`, `skip` and `info` each claim the connection **once**, for the whole operation, as a
[Transaction](driver.md#transactions) does. The GIL is released throughout.

- A `Connection` is claimed directly. A `Pool` acquires a single connection.
- A `Transaction` argument raises `InterfaceError`. Each migration manages its own transaction, and a
  `-- no-transaction` file can't run inside one.

### Locking

- The lock is `pg_advisory_lock(key)`, where `key` is sqlx's `0x3d32ad9e * crc32(current_database())`, so a sqlx
  migrator and a rowx migrator exclude each other.
- It is a session-level lock. It is taken **before** the history table is created, and released with
  `pg_advisory_unlock` at the end of the operation.
- On a pooled connection, `RESET_QUERY`'s `pg_advisory_unlock_all()` ([Release](driver.md#release)) is the backstop if
  the unlock never runs.
- `locking=False` turns the lock off (sqlx offers this for CockroachDB).

### Executing a file

A migration is many statements, so a file runs over the **simple query protocol** through the existing `Conn::simple`
(`batch_execute`), which sends the whole file as one Query message, as sqlx does. rowx doesn't split or parse the file.
This is internal: there is still no public raw-SQL escape. Migration files are trusted code
([Security and safety](security.md)).

### A transactional migration

On one connection:

1. `BEGIN`.
2. The file.
3. `INSERT` into the history table with `success = true` and `execution_time = -1`.
4. `COMMIT`.

`execution_time` is then updated, in nanoseconds, outside the transaction (sqlx #1966). A file that issues its own
`COMMIT` must be `-- no-transaction`; the guide says so, and rowx does not parse the file to detect it.

### A `-- no-transaction` migration

1. `INSERT` the row with `success = false`.
2. The file.
3. `UPDATE` the row to `success = true`, with the `execution_time`.

A failure part-way through leaves a **dirty** row, and the next run raises `DirtyMigrationError`. This improves on
sqlx, which records nothing on failure and so can't detect partial application on Postgres. Without its own `COMMIT`s,
a failed multi-statement file is rolled back whole by Postgres's implicit transaction; rowx can't tell, so the row is
dirty either way and the operator decides.

### Reverting

`undo` mirrors apply, with the `.down.sql` file's own `-- no-transaction` line:

- **Transactional:** `BEGIN`, the `.down.sql` file, `DELETE` the history row, `COMMIT` (as sqlx does).
- **`-- no-transaction`:** `UPDATE` the row to `success = false`, the file, then `DELETE` the row. A failure leaves the
  row dirty, as for apply.

### Cancellation

Cancelling `run` cancels and settles the connection as `close()` does ([Cancellation](driver.md#cancellation-cancelrs)).
A transactional migration rolls back and isn't recorded. A non-transactional one is left dirty. The lock ends with the
session, or with the pool's reset.

### Schema epoch

When `run` or `undo` on a `Pool` changes anything, it bumps the pool's schema epoch ([Release](driver.md#release),
decision C18), so no connection reuses a statement plan that is now stale.

### Timeouts

Migrations don't change `statement_timeout` or `lock_timeout`. The guide covers setting them inside a file:

- In a transactional migration, `SET LOCAL` scopes the setting to that migration. A plain `SET` stays on the claimed
  connection for the rest of the operation (and on a `Connection` afterwards), until a pool's `RESET ALL`.
- A `-- no-transaction` file whose only statement is, say, `CREATE INDEX CONCURRENTLY` can't carry a `SET` (see
  [Files](#files)). Its timeouts come from the connection: `server_settings`, a DSN `options=-c lock_timeout=…`, or a
  role or database default.

## Plan rules

- **Validation before any change.** The plan checks, in this order, for dirty rows, for applied migrations that are
  missing locally (unless `ignore_missing=True`), and for modified checksums. Each check raises before anything runs.
- **Out-of-order is an error by default.** A pending migration whose version is lower than the highest applied version
  raises `MigrationOrderError`, naming both versions. `allow_out_of_order=True` (`--allow-out-of-order`) applies it, in
  version order.
- **`run(target=V)`.** `V` must exist locally, and a `V` below the highest applied version is `MigrationOrderError`.
  Migrations up to and including `V` are applied.
- **`undo(target=V)`.** It reverts every applied version above `V`, newest first, where `V = 0` means all of them. If
  any of them has no `.down.sql`, `MigrationSourceError` is raised before anything is reverted. (sqlx skips such
  migrations silently.) `rowx migrate revert` with no target reverts only the latest migration.
- **`skip(target=None)`** records pending migrations as applied without running them, to baseline an existing database
  (sqlx `skip`).
- **`info`** returns a `MigrationStatus` per version, taken from both sides:
  - `applied`, `pending`, `modified`, `missing` or `dirty`;
  - `installed_on` and `execution_time` for applied ones.

  It creates nothing: a missing history table means every migration is pending. (sqlx's `info` creates the table,
  which its own FIXME calls a bug.)

## Out of scope

- Loading migrations from package resources (`importlib.resources`).
- Python-code migrations, repeatable or "afterMigrate" migrations, and migration squashing.
- `sqlty breaking`-style cross-revision checks ([Roadmap](roadmap.md#deferred-work-by-expected-value), item 7). This
  feature gives that check a way to build the old and new schemas.
- Per-test SQL fixtures (sqlx `#[sqlx::test(fixtures(...))]`).
