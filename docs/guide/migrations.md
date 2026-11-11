# Migrations

Migrations are plain `.sql` files in the format [sqlx](https://github.com/launchbadge/sqlx) uses. `Migrator` applies
them in version order, records each one in a history table, and holds an advisory lock while it works. `rowx migrate`
and `rowx database` do the same from the command line, and `rowx check` and the `rowx.testing` pytest plugin build on
it. The blocks below are executed by the test suite, each in a temporary directory.

## Files

```text
migrations/
  0001_create_accounts.up.sql      # a reversible pair
  0001_create_accounts.down.sql
  0002_add_email.sql               # a simple (forward-only) migration
  0003_index_accounts_email.up.sql    # starts with -- no-transaction
  0003_index_accounts_email.down.sql  # so does its own down file
```

- **Names.** `<VERSION>_<DESCRIPTION>.sql` is a simple migration, and `<VERSION>_<DESCRIPTION>.up.sql` with
  `<VERSION>_<DESCRIPTION>.down.sql` is a reversible one. The version is ASCII digits that fit an `i64`: `0` is
  valid, and leading zeros are allowed (`1_a.sql` and `001_b.sql` are the same version). `_` in the description reads
  as a space. Simple and reversible migrations can be mixed.
- **Errors at construction.** A `.sql` file that doesn't match the grammar, a duplicate version, an `.up.sql` without
  its `.down.sql` (or the reverse), and a file that isn't UTF-8 each raise `MigrationSourceError` naming the file. Files
  that don't end in `.sql` and subdirectories are ignored.
- **Scaffolding.** `rowx migrate add "add email"` writes the next file. Versions are sequential (`0001`) when the last
  two differ by one and timestamps (`20261029120000`, UTC) otherwise. The kind follows the latest migration. Pass
  `--reversible` or `--simple`, and `--timestamp` or `--sequential`, to override.
  Without a `[tool.rowx.migrate]` table, it uses `migrations/` and prints a one-line tip on stderr about adding one.
- **`-- no-transaction`.** A file whose bytes start with `-- no-transaction` runs without the `BEGIN` and `COMMIT` rowx
  puts around other files. It is still sent as **one** simple-protocol query, and Postgres runs a multi-statement query
  as one implicit transaction. So:
  - a statement that can't run in a transaction block (`CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`,
    `REINDEX ... CONCURRENTLY`, `VACUUM`) must be the **only** statement in its file;
  - a new enum value from `ALTER TYPE ... ADD VALUE` can't be used in the statement batch that adds it, so put the use
    in a later migration, or give the file its own `BEGIN; ...; COMMIT;` pairs.
- **Checksums.** A history row stores the SHA-384 of the file's raw bytes (the `.up.sql` for a pair), with no
  normalisation. Changing a byte, including a line ending, makes an applied migration `modified`.

```python test
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from rowx_postgres import Migrator, Pool

FILES = {
    "0001_create_accounts.up.sql": "CREATE TABLE accounts (id int PRIMARY KEY, name text NOT NULL);\n",
    "0001_create_accounts.down.sql": "DROP TABLE accounts;\n",
    "0002_add_email.up.sql": "ALTER TABLE accounts ADD COLUMN email text;\n",
    "0002_add_email.down.sql": "ALTER TABLE accounts DROP COLUMN email;\n",
    # alone in its file, because a no-transaction file is still one implicit transaction
    "0003_index_accounts_email.up.sql": "-- no-transaction\nCREATE INDEX CONCURRENTLY accounts_email ON accounts (email);\n",
    "0003_index_accounts_email.down.sql": "-- no-transaction\nDROP INDEX CONCURRENTLY accounts_email;\n",
}


async def main(directory: Path) -> None:
    for name, sql in FILES.items():
        (directory / name).write_text(sql)

    # reads and validates the files now, with no database IO
    migrator = Migrator(directory)
    assert [m.version for m in migrator.migrations] == [1, 2, 3]
    assert migrator.migrations[-1].no_transaction
    assert migrator.migrations[0].kind == "reversible"
    assert migrator.migrations[0].down_path is not None

    async with Pool(DSN, max_size=2) as pool:
        # the plan, read without the migration lock: nothing runs and nothing is written
        planned = await migrator.run(pool, dry_run=True)
        assert [r.migration.version for r in planned] == [1, 2, 3]
        assert [s.state for s in await migrator.info(pool)] == ["pending"] * 3

        for result in await migrator.run(pool):
            assert result.elapsed is not None
        assert await migrator.run(pool) == ()

        # no target reverts only the latest migration
        assert [r.migration.version for r in await migrator.revert(pool)] == [3]
        # a target reverts every applied version above it, newest first; all=True reverts them all
        assert [r.migration.version for r in await migrator.revert(pool, all=True, dry_run=True)] == [2, 1]
        assert [r.migration.version for r in await migrator.revert(pool, target=1)] == [2]
        assert [s.state for s in await migrator.info(pool)] == ["applied", "pending", "pending"]


with TemporaryDirectory() as tmp:
    asyncio.run(main(Path(tmp)))
```

## Running

At startup, in the application, or as a deploy step. The lock makes both safe when several instances start together.

```python
from rowx_postgres import Migrator, Pool

migrator = Migrator("migrations")


async def start(dsn: str) -> Pool:
    pool = Pool(dsn)
    await pool.open()
    for result in await migrator.run(pool):
        print(f"applied {result.migration.version} {result.migration.description} in {result.elapsed}")
    return pool
```

```sh
rowx migrate run                      # applies every pending migration
rowx migrate run --dry-run            # prints the plan and writes nothing
rowx migrate run --target-version 20261029120000
rowx migrate revert                   # reverts the latest applied migration
rowx migrate revert --target-version 20261029120000   # every applied version above it
rowx migrate revert --all             # everything
rowx migrate skip                     # records pending migrations as applied, without running them
rowx migrate info
rowx migrate info --output-format json
```

- `run`, `revert` and `skip` take a `Connection` or a `Pool` and return a `tuple[MigrationResult, ...]`: one
  `MigrationResult(migration, elapsed)` per migration applied, reverted or skipped, in the order it happened. `elapsed`
  is a `timedelta` (for `revert`, how long the revert took), or `None` for a dry run or a skip. `skip` takes `dry_run`
  too.
- `info` returns a `tuple[MigrationStatus, ...]`, one per version. `status.state` is `applied`, `pending`, `modified`,
  `missing` or `dirty`; `kind` is `simple` or `reversible` (`None` when the file is missing); `installed_on` and
  `execution_time` are set for applied rows; and `local_checksum` and `applied_checksum` are the two SHA-384 values.
- `info` and `dry_run=True` take no advisory lock and create nothing, so they return even while a migration is running.
  If the call is cancelled, no further migration starts and the lock is released.
- A `Transaction`, or a `Connection` inside one, raises `InterfaceError`, because each migration manages its own
  transaction. The operation holds its connection for the whole run, so a concurrent query on that `Connection` raises
  `ConnectionBusyError`.
- `migrator.migrations` lists one `Migration` per version (`version`, `description`, `kind` of `simple` or
  `reversible`, `path`, `down_path`, `checksum`, `down_checksum` and `no_transaction`); a reversible pair is a single
  entry, and `checksum` is the up file's. `migrator.path` is the directory, resolved against the cwd. The files are
  read when `Migrator` is constructed, so a later edit needs a new `Migrator`.
- `Migrator.skip` and `rowx migrate skip [--target-version V] [--dry-run]` record pending migrations as applied without
  running them, to baseline a database that already has the schema. A skipped row has no execution time. `skip` follows
  `run`'s rules and takes `--allow-out-of-order` and `--ignore-missing`.
- Migrate over a direct connection or a session-mode pooler. The lock is a session-level advisory lock, so PgBouncer in
  transaction mode is unsupported.

```text
$ rowx migrate run
Applied 1/migrate create accounts (3.2ms)
Applied 2/migrate add email (2.0ms)
$ rowx migrate info
1/applied create accounts (3.2ms)
2/applied add email (2.0ms)
3/pending index accounts email
```

`rowx migrate info --output-format json` writes an array with one object per version, for scripts and CI:

```json
[
  {
    "version": 2,
    "description": "add email",
    "state": "applied",
    "kind": "reversible",
    "installed_on": "2026-10-29T12:00:01.250000+00:00",
    "execution_time": 2000000,
    "local_checksum": "ccb5...",
    "applied_checksum": "ccb5..."
  }
]
```

`installed_on` is an ISO 8601 timestamp, `execution_time` is in nanoseconds, and the checksums are hex (a field is
`null` when it has no value, such as `applied_checksum` on a pending migration). `info` exits 0 even when it reports
drift.

`rowx migrate` takes `--database-url` or reads the variable named by `database_url_env`, finds `dir` and the other
settings in `[tool.rowx.migrate]`, and takes `--source DIR` (relative to the current directory) to override the
directory. A flag beats the config, which beats the default (`migrations/`, table `_rowx_migrations`).

```toml
[tool.rowx.migrate]                # its presence turns on the drift gate, --scratch and the test plugin
dir = "migrations"                 # relative to pyproject.toml
table = "_rowx_migrations"         # "_sqlx_migrations" adopts an sqlx project
create_schemas = []                # CREATE SCHEMA IF NOT EXISTS, before the table is created
ignore_missing = false             # default for --ignore-missing; also lets `missing` pass the drift gate
allow_out_of_order = false         # default for --allow-out-of-order
versioning = "inferred"            # inferred | timestamp | sequential (for `migrate add`)
kind = "inferred"                  # inferred | simple | reversible (for `migrate add`)
```

`ignore_missing` and `allow_out_of_order` set the policy everywhere migrations are applied or compared: `rowx migrate`,
the drift gate (`ignore_missing` lets `missing` pass), `rowx check --scratch`, and the test plugin's template build.
`--ignore-missing` and `--allow-out-of-order` turn a policy on for one command, and `--no-ignore-missing` and
`--no-allow-out-of-order` turn it off when the config has it on.

`--connect-timeout SECONDS` (default 10) on `rowx migrate` and `rowx database` retries while the server can't be
reached or is starting up (`57P03`), so a command run right after `docker compose up` waits for the database. Each
attempt is cut off when the time runs out, so a host that never answers fails after the timeout instead of hanging.
`--connect-timeout 0` tries once, limited only by the URL's own `connect_timeout`. An authentication error or any other
server error is not retried, and only connecting is retried, never the command itself.

`rowx database` manages the database itself, from the same URL:

```text
rowx database create               # exits 0 if the database already exists
rowx database drop [--force] [-y]  # prompts on a terminal; -y is required otherwise
rowx database reset [--force] [-y] # drop, create, then run every migration
rowx database setup                # create if missing, then run every migration
```

## Safety

- **Out of order.** A pending migration older than the latest applied one raises `MigrationOrderError`. A branch that
  merged late is the usual cause. `Migrator(..., allow_out_of_order=True)` or `rowx migrate run --allow-out-of-order`
  applies it, in version order.
- **Modified.** An applied migration whose file changed raises `MigrationChecksumError` (`version`, `local_checksum`,
  `applied_checksum`). Fix it with a new migration, not by editing history. `rowx migrate info` shows both short
  checksums.
- **Missing.** An applied version with no local file raises `MigrationMissingError`, unless `ignore_missing=True` or
  `--ignore-missing`. Even then, reverting such a version raises `MigrationMissingError`, since there is no
  `.down.sql` to run.
- **A file that ends the transaction.** A transactional file that runs `COMMIT` or `ROLLBACK` itself, and doesn't open
  another transaction, raises `MigrationFailedError` and records nothing. What the file committed stays committed but
  unrecorded, so mark such a file `-- no-transaction`.
- **Dirty.** A `-- no-transaction` file records its row with `success = false` before it runs. If the file fails, the
  row stays, `info` reports `dirty`, and the next run raises `DirtyMigrationError` rather than guess what half-applied.
  A transactional file rolls back whole and leaves nothing. To recover from a dirty row, inspect the database, undo or
  finish the file's changes by hand, then delete the row so the migration is pending again:

```python test
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from rowx import query_unchecked
from rowx_core.errors import DirtyMigrationError, MigrationChecksumError, MigrationFailedError, MigrationOrderError
from rowx_postgres import Migrator, connect

TABLE = "_rowx_migrations"


async def main(directory: Path) -> None:
    (directory / "1_accounts.sql").write_text("CREATE TABLE accounts (id int PRIMARY KEY);\n")
    (directory / "3_email.up.sql").write_text("ALTER TABLE accounts ADD COLUMN email text;\n")
    (directory / "3_email.down.sql").write_text("ALTER TABLE accounts DROP COLUMN email;\n")

    async with await connect(DSN) as conn:
        await Migrator(directory).run(conn)

        # a branch lands a version below the latest applied one
        (directory / "2_name.sql").write_text("ALTER TABLE accounts ADD COLUMN name text;\n")
        try:
            await Migrator(directory).run(conn)
        except MigrationOrderError as error:
            assert error.version == 2
        applied = await Migrator(directory, allow_out_of_order=True).run(conn)
        assert [r.migration.version for r in applied] == [2]

        # an applied file changed
        (directory / "1_accounts.sql").write_text("CREATE TABLE accounts (id bigint PRIMARY KEY);\n")
        try:
            await Migrator(directory).run(conn)
        except MigrationChecksumError as error:
            assert error.version == 1
        (directory / "1_accounts.sql").write_text("CREATE TABLE accounts (id int PRIMARY KEY);\n")

        # a no-transaction file that fails part way leaves a dirty row
        (directory / "4_broken.sql").write_text("-- no-transaction\nCREATE TABLE broken (id int);\nSELECT 1 / 0;\n")
        migrator = Migrator(directory)
        try:
            await migrator.run(conn)
        except MigrationFailedError as error:
            assert error.version == 4
            assert error.__cause__ is not None  # the server's DatabaseError
        assert [s.state for s in await migrator.info(conn)][-1] == "dirty"
        try:
            await migrator.run(conn)
        except DirtyMigrationError as error:
            assert error.version == 4

        # recovery: fix the database by hand, fix the file, delete the row
        (directory / "4_broken.sql").write_text("-- no-transaction\nCREATE TABLE IF NOT EXISTS broken (id int);\n")
        await query_unchecked(t"DELETE FROM {TABLE:ident} WHERE version = {4}").execute(conn)
        # a Migrator reads its files when it is constructed, so it is rebuilt after the edit
        assert [r.migration.version for r in await Migrator(directory).run(conn)] == [4]


with TemporaryDirectory() as tmp:
    asyncio.run(main(Path(tmp)))
```

- **Revert.** `revert()` reverts only the latest applied migration. `revert(target=V)` reverts every applied version
  above `V`, newest first, and `revert(all=True)` reverts them all. `target` with `all`, an unknown `target`, and a
  `target` above the latest applied version are `ValueError`s. `rowx migrate revert`,
  `rowx migrate revert --target-version V` and `rowx migrate revert --all` do the same, and `--all` with
  `--target-version` is a usage error (exit 2). `--target-version 0` is an unknown version, not "everything". Use
  `--all`. If any version in the range is a simple migration (no
  `.down.sql`), nothing is reverted and `IrreversibleMigrationError` names the version. A transactional `.down.sql` runs
  in a transaction with the history row's deletion, and a `-- no-transaction` one leaves a dirty row when it fails.
- **Failures.** A statement that fails inside a migration raises `MigrationFailedError` with the server's
  `DatabaseError` as `error` (and `__cause__`). It also has `version`, `path`, `dirty` (a history row was left
  `success = false`) and `applied`, the `MigrationResult`s that completed before it, which stay applied.
- **Trusted code.** Migration files run with the connecting role's privileges and are not parsed or sandboxed. A file
  that ends the transaction rowx opened (`COMMIT` or `ROLLBACK`) is not detected: mark such a file `-- no-transaction`
  and write its own `BEGIN` and `COMMIT`.

## Setup

- **Line endings.** Checksums are over the raw bytes, so a checkout that rewrites line endings makes every applied
  migration `modified`. Add this to `.gitattributes`:

  ```text
  migrations/*.sql text eol=lf
  ```

- **Timeouts.** Migrations don't change `statement_timeout` or `lock_timeout`. Set them on the migrating role, or in the
  URL, rather than in a file, since a lone `CREATE INDEX CONCURRENTLY` can't share its file with a `SET`:

  ```sql
  ALTER ROLE migrator SET lock_timeout = '5s';
  ```

  ```text
  postgresql://migrator@db/app?options=-c%20lock_timeout%3D5s
  ```

  In a transactional file, `SET LOCAL lock_timeout = '5s';` scopes the setting to that migration. A plain `SET` stays on
  the connection.
- **Privileges.** The role needs `CREATE` on the schema that holds the history table, and `rowx check`'s drift gate
  needs `SELECT` on it.

## Adopting from sqlx

rowx reads sqlx's file format and history table, and takes sqlx's advisory lock, so a project switches without renaming
files or rewriting history. Keep the directory and set the table:

```toml
[tool.rowx.migrate]
dir = "migrations"
table = "_sqlx_migrations"
```

```python test
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from rowx import query_unchecked
from rowx_postgres import Migrator, connect

# sqlx stores the SHA-384 of the file bytes: `printf 'CREATE TABLE people (id int PRIMARY KEY);\n' | shasum -a 384`
CHECKSUM = bytes.fromhex(
    "59216535ceb8079ed0db7163ceac6f6072013166f5f78c541a44f91f05cabeda2325e7324a6c40e6ea5cb7924bbc71db"
)


async def main(directory: Path) -> None:
    (directory / "20240101000000_create_people.sql").write_text("CREATE TABLE people (id int PRIMARY KEY);\n")
    (directory / "20240102000000_add_email.up.sql").write_text("ALTER TABLE people ADD COLUMN email text;\n")
    (directory / "20240102000000_add_email.down.sql").write_text("ALTER TABLE people DROP COLUMN email;\n")

    async with await connect(DSN) as conn:
        # sqlx's history table, as `sqlx migrate run` creates it
        await query_unchecked(
            t"""
            CREATE TABLE _sqlx_migrations (
                version BIGINT PRIMARY KEY,
                description TEXT NOT NULL,
                installed_on TIMESTAMPTZ NOT NULL DEFAULT now(),
                success BOOLEAN NOT NULL,
                checksum BYTEA NOT NULL,
                execution_time BIGINT NOT NULL
            )
            """
        ).execute(conn)
        await query_unchecked(t"CREATE TABLE people (id int PRIMARY KEY)").execute(conn)
        await query_unchecked(
            t"INSERT INTO _sqlx_migrations (version, description, success, checksum, execution_time) "
            t"VALUES (20240101000000, 'create people', true, {CHECKSUM}, 1000)"
        ).execute(conn)

        migrator = Migrator(directory, table="_sqlx_migrations")
        assert [(s.version, s.state) for s in await migrator.info(conn)] == [
            (20240101000000, "applied"),
            (20240102000000, "pending"),
        ]
        assert [r.migration.version for r in await migrator.run(conn)] == [20240102000000]


with TemporaryDirectory() as tmp:
    asyncio.run(main(Path(tmp)))
```

What differs from sqlx:

- **Unpaired files are errors.** An `.up.sql` without its `.down.sql` (or the reverse) raises `MigrationSourceError`.
  sqlx tolerates it. A `.sql` file whose name doesn't match the grammar is also an error.
- **No `ignored_chars`.** sqlx's `sqlx.toml` option to ignore characters when hashing isn't ported, so a project that
  used it sees `modified` for the affected files.
- **Table names follow Postgres rules.** `table` and `create_schemas` are parsed like identifiers in SQL: an unquoted
  part matches `[A-Za-z_][A-Za-z0-9_$]*` and is folded to lower case, so sqlx's `MySchema.Migrations` finds the same
  table in rowx. A `"…"` part is kept verbatim, with `""` for a `"`. A `.` outside quotes separates the schema from the
  table, and an empty part, a third part, a NUL byte, a bad character or a part over 63 bytes raises `ValueError`.
- **Dirty rows.** A `-- no-transaction` file that fails leaves a `success = false` row, which sqlx also reports as
  dirty. sqlx itself records nothing when such a file fails.
- **Locks exclude each other.** Both tools take `pg_advisory_lock` on the same key, derived from the database name, so
  an sqlx deploy and a rowx deploy wait for one another.

## Checking

With `[tool.rowx.migrate]` in `pyproject.toml`, `rowx check` compares the database with the directory before it
describes any query. A `pending`, `modified`, `missing` or `dirty` migration exits 2 with a summary on stderr and
nothing on stdout:

```text
error: database has drifted from `migrations/`:
  3 pending: 20261001090000, 20261002090000, 20261003090000
run `rowx migrate run`, or `rowx check --scratch`
```

- The gate engages only when at least one query is described, and `--no-drift-check` skips it.
- `ignore_missing = true` in `[tool.rowx.migrate]` lets `missing` pass.
- The check reads the history table and creates nothing. The role needs `SELECT` on it, and without it the gate exits 2
  with "grant SELECT on `<table>` to the checker role". It takes no migration lock, so
  it never waits for a migration that is running.
- Without `[tool.rowx.migrate]`, `rowx check` behaves as it did before.

`rowx check --scratch` needs no database to prepare. It treats the URL as a **server** URL, creates `rowx_check_<hex>`,
runs the migrations in it, checks every query against it, and drops it on every exit path. It needs a role with
`CREATEDB`, and it skips the drift gate, since the database is built from the files. Like the gate, it only builds the
database when at least one query is described, so a tree with no queries doesn't validate the migrations (use
`rowx database setup` or `rowx migrate run` for that).

```yaml
name: rowx

on: [push, pull_request]

jobs:
  check:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18
        env:
          POSTGRES_USER: app
          POSTGRES_PASSWORD: app
          POSTGRES_DB: app
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U app -d app"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      DATABASE_URL: postgresql://app:app@localhost:5432/app
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
      - run: uv sync
      # builds a scratch database from the migrations, checks against it, and drops it
      - run: uv run rowx check --scratch src
```

The scratch database is created and dropped by the run itself, so the service only has to accept connections. To check
against a database you migrated yourself, run `rowx migrate run` first and drop `--scratch`; the drift gate then fails
the job if the database and the directory disagree.
