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
  `<VERSION>_<DESCRIPTION>.down.sql` is a reversible one. `VERSION` is ASCII digits parsed as an `i64`, so it is 0 or
  more and may have leading zeros, as in sqlx: `1_a.sql` and `001_b.sql` are a duplicate version. A sign or any other
  character is an error. The description is the rest of the name, with `_` read as a space. Simple and reversible
  migrations can be mixed in one directory.
- **Which files are read.** Only regular files directly in the directory are read, following symlinks, and
  subdirectories are ignored. A file that doesn't end in `.sql` (case-sensitive) is ignored.
- **Resolution errors.** Each is a `MigrationSourceError` that names the file:
  - a `.sql` file that doesn't match the grammar (sqlx's current default; there is no warn or ignore setting);
  - two migrations with the same version;
  - an `.up.sql` without its `.down.sql`, or the reverse (sqlx tolerates this; the sqlx adoption guide says so);
  - a file that isn't UTF-8.
- **`-- no-transaction`.** When a file starts with these bytes (`starts_with`, exactly as sqlx does), rowx sends no
  `BEGIN` around the file ([A `-- no-transaction` migration](#a----no-transaction-migration)). So
  `-- no-transaction\r\n` and `-- no-transaction (concurrent index)` both count, and a leading BOM disables it. It
  opts the file out of rowx's `BEGIN`/`COMMIT` only. The file is still sent as **one** simple
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
- **Checksum.** SHA-384 over the raw file bytes (a BOM included), with no normalisation, so checksums match sqlx byte
  for byte. The guide recommends `migrations/*.sql text eol=lf` in `.gitattributes`. sqlx's `ignored_chars` setting is
  not ported, and a project that used it will see `modified` (the adoption guide says so).
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
- **Table names.** `table` and each `create_schemas` entry are parsed with Postgres identifier rules, at construction.
  `table` may be schema-qualified (`app._rowx_migrations`).
  - An unquoted part must match `[A-Za-z_][A-Za-z0-9_$]*` and is lowercased, so `MySchema.Migrations` finds the table
    sqlx created.
  - A `"…"` part is kept verbatim, with `""` unescaped.
  - Each part is at most 63 bytes, and there are one or two parts (a `create_schemas` entry has one).
  - Anything else raises `ValueError`, before any IO. The executor always emits each part quoted. (sqlx interpolates
    the raw string.)
- **`create_schemas`** runs `CREATE SCHEMA IF NOT EXISTS` (each name quoted) before the table is created, as sqlx does.

## Engine

### Layers

- `rowx-core/src/migrate/` holds what any backend shares, as pure functions with `cargo` unit tests: the filename
  grammar, resolving (name, bytes) entries into sorted `Migration`s (with a thin `read_dir` as the only IO), checksums,
  table-name parsing, and the plan. The plan takes the local migrations, the applied rows and the options, and returns
  either the steps to run or an error.
- `rowx-postgres/src/migrate.rs` carries a plan out over one connection: the lock, the history table, apply, revert and
  skip. `rowx-postgres/src/admin.rs` creates, drops, checks for and renames databases, sets template flags, and
  re-renders a DSN for another database ([Database administration](checker.md#migrate-and-database-commands)). The claim
  and `cancel::spawn` live in the binding, mirroring `exec::simple`.
- Python only constructs the migrator, calls it and maps errors.
- Errors are `thiserror` enums in `rowx-core` (source and plan) and `rowx-postgres` (apply). Python exceptions are
  created only in `rowx-py-core/src/errors.rs` (the registry and the core variants) and
  `rowx-postgres-py/src/errors.rs`.

### One claim per run

`run`, `revert`, `skip` and `info` each claim the connection **once**, for the whole operation, as a
[Transaction](driver.md#transactions) does. The GIL is released throughout. The binding takes only a connection impl.

- A `Connection` is claimed directly. For a `Pool`, `_migrate.py` does `async with pool.acquire() as conn`, so the
  `init`/`setup` hooks, `acquire_timeout`, release and reset all behave normally.
- A concurrent query on the claimed connection raises `ConnectionBusyError`.
- A `Transaction`, or a `Connection` with an open transaction (`txn_depth() > 0`), raises `InterfaceError`. Each
  migration manages its own transaction, and a `-- no-transaction` file can't run inside one.

### Locking

- The lock is `pg_advisory_lock(key)`, where `key` is sqlx's `0x3d32ad9e * crc32(current_database())`, so a sqlx
  migrator and a rowx migrator exclude each other.
- It is the `bigint` overload of a session-level lock. The key fits in an `i64`, with CRC-32/ISO-HDLC as in sqlx, and a
  test vector is hard-coded from sqlx. It is taken **before** the history table is created, and released with
  `pg_advisory_unlock` at the end of the operation.
- **`info` and dry runs take no lock** and create nothing, so the drift gate never waits behind a running deploy.
- rowx doesn't rely on `RESET_QUERY` to release the lock, because `reset_query` may be `None` or custom
  ([Errors and cancellation](#errors-and-cancellation)).
- `locking=False` turns the lock off (sqlx offers this for CockroachDB).
- Migrating through PgBouncer in transaction mode is unsupported (the lock is per session). The guide says to use a
  direct or session-mode connection.

### Executing a file

A migration is many statements, so a file runs over the **simple query protocol** through the existing `Conn::simple`
(`batch_execute`), which sends the whole file as one Query message, as sqlx does. rowx doesn't split or parse the file.
This is internal: there is still no public raw-SQL escape. Migration files are trusted code
([Security and safety](security.md)).

### A transactional migration

On one connection:

1. `BEGIN`.
2. The file.
3. `SAVEPOINT rowx_probe`. If the file ended rowx's transaction (with `COMMIT` or `ROLLBACK`) and left none open, the
   probe fails with 25P01. rowx sends `ROLLBACK` if needed and raises `MigrationFailedError` ("the file ended rowx's
   transaction; mark it `-- no-transaction`") before anything is recorded. Otherwise it releases the savepoint.
4. `INSERT` into the history table with `success = true` and `execution_time = -1`.
5. `COMMIT`.

`execution_time` is then updated, in nanoseconds, outside the transaction (sqlx #1966). rowx does not parse the file to
detect transaction control; the probe is what catches it.

The probe detects a file that ended rowx's transaction, but it can't undo what that file committed. After a `ROLLBACK`
in the file, nothing before it survives; after a `COMMIT`, everything before it is committed. Either way, any
statements after it ran in Postgres's own implicit transaction and are committed too. No history row is written, and
the error says that the file's changes may be committed but unrecorded. A file that ends
rowx's transaction and then opens its own with `BEGIN` passes the probe, and the history row joins that transaction.

### A `-- no-transaction` migration

1. `INSERT` the row with `success = false`.
2. The file.
3. `UPDATE` the row to `success = true`, with the `execution_time`.

A failure part-way through leaves a **dirty** row, and the next run raises `DirtyMigrationError`. This improves on
sqlx, which records nothing on failure and so can't detect partial application on Postgres. Without its own `COMMIT`s,
a failed multi-statement file is rolled back whole by Postgres's implicit transaction; rowx can't tell, so the row is
dirty either way and the operator decides.

### Reverting

`revert` mirrors apply, with the `.down.sql` file's own `-- no-transaction` line:

- **Transactional:** `BEGIN`, the `.down.sql` file, the same `rowx_probe` check, `DELETE` the history row, `COMMIT` (as
  sqlx does).
- **`-- no-transaction`:** `UPDATE` the row to `success = false`, the file, then `DELETE` the row. A failure leaves the
  row dirty, as for apply.

### Errors and cancellation

Neither an error nor a cancellation leaves a lock or a transaction behind.

- On error or cancellation, the executor sends `ROLLBACK` when a step's transaction may be open, then
  `pg_advisory_unlock`. Cancelling cancels and settles the connection as `close()` does
  ([Cancellation](driver.md#cancellation-cancelrs)).
- If the `ROLLBACK` or the unlock fails, the connection is marked Broken (a pooled one is discarded), never left holding
  the lock. Advisory locks are re-entrant per session, so a missed unlock could otherwise stack.
- The binding passes a cancelled flag, and the executor checks it before each step, so no step starts after the Python
  future is dropped.
- A transactional step rolls back and isn't recorded. A non-transactional one is left dirty.
- Only a migration file's own SQL, and the `rowx_probe` check after it, becomes a `MigrationFailedError`. A failure in
  the lock or history-table SQL raises `DatabaseError`, unwrapped. `MigrationFailedError` carries `dirty` and the
  results applied before the failure ([API](api.md#rowxpostgres)).

### Schema epoch

After any change, the claimed connection's statement cache is cleared. When `run`, `revert` or `skip` on a `Pool`
changes anything, it also bumps the pool's schema epoch (`PgPool::bump_epoch`, [Release](driver.md#release), decision
C18), so no other connection reuses a statement plan that is now stale.

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
  raises `MigrationOrderError(version, latest)`. `allow_out_of_order=True` (`--allow-out-of-order`) applies pending
  migrations in version order.
- **`run(target=V)`.**
  - `V` must exist locally; otherwise `ValueError` before any IO (`TargetNotFound`).
  - A `V` below the highest applied version is `MigrationOrderError`, unless `allow_out_of_order`.
  - Migrations up to and including `V` are applied.
- **`revert(target=None, all=False)`.** The plan's target is `Latest`, `Above(V)` or `All`, newest first.
  - `None` reverts the latest applied version only, `target=V` every applied version above `V`, and `all=True`
    everything. `target` with `all` is a `ValueError`.
  - `V` must be a local version (`TargetNotFound`) that isn't above the latest applied one (`TargetTooNew`); both are
    `ValueError` (sqlx `VersionNotPresent`/`VersionTooNew`).
  - If any version in range is simple, `IrreversibleMigrationError` is raised before anything is reverted. (sqlx skips
    such versions silently.)
  - An applied version in range that is missing locally is `MigrationMissingError` even with `ignore_missing`, because
    it can't be reverted. The plan's `Missing` carries every missing version, not just the first.
- **`skip(target=None)`** records pending migrations as applied without running them, to baseline an existing database
  (sqlx `skip`). It follows `run`'s rules, including `dry_run`.
- **`info`** returns a `MigrationStatus` for every version from both sides, ordered by version:
  - `applied`, `pending`, `modified`, `missing` or `dirty`;
  - `kind`, the checksums on both sides, and `installed_on` and `execution_time` for applied ones.

  It takes no lock and creates nothing: a missing history table means every migration is pending. (sqlx's `info` creates
  the table, which its own FIXME calls a bug.)

## Out of scope

- Loading migrations from package resources (`importlib.resources`).
- Python-code migrations, repeatable or "afterMigrate" migrations, and migration squashing.
- `sqlty breaking`-style cross-revision checks ([Roadmap](roadmap.md#deferred-work-by-expected-value), item 7). This
  feature gives that check a way to build the old and new schemas.
- Per-test SQL fixtures (sqlx `#[sqlx::test(fixtures(...))]`).
