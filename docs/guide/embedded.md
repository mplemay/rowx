# Embedded Postgres

rowx can run a real PostgreSQL 18 inside your Python process, with no server, no Docker and no child process. It is for
development, `rowx check` and tests. **It is not for production**: it serves one session at a time and cannot cancel a
running statement. Production keeps a real server.

In code you use `rowx.embedded.Database`, which is built like a pool. The CLI and the pytest plugin take an
`embedded://` URL instead. The engine is PostgreSQL 18.4 compiled to WASIX and
run by Wasmer; every instance has its own memory. It is the `rowx-embedded` companion package, which `rowx[embedded]`
installs. This page is the guide; [the reference](../reference/embedded.md) has the contract.

The `python test` blocks on this page are executed by the test suite, on the embedded engine.

## Install

```sh
uv add "rowx[embedded]"
```

The extra installs the matching `rowx-postgres` and the `rowx-embedded` wheel, which carries the engine. Wheels exist
for macOS on Apple silicon and Linux on x86_64 and aarch64. On any other platform (Windows, macOS on Intel) the extra
still installs, but ships no engine, so `rowx.embedded` raises an `ImportError` that names the extra and an
`embedded://` URL reports the missing extra.

You can use it without the CLI: `rowx-embedded` (which brings `rowx-postgres`) is enough for `Database`, imported from
`rowx_embedded`.

## Database

`Database` is an `Executor` and a `rowx_postgres.Pool` that owns one in-process instance. `Database()` is a fresh
in-memory database, gone when it closes; `Database("/absolute/path")` keeps its data in that directory between runs,
with one owner at a time. A relative path is a `ValueError`. Pass `options=ConnectOptions(...)` for
`application_name`, `server_settings` (such as `{"search_path": "app"}`) and the statement cache; a host, port, user,
password or `sslmode` is a `ValueError`. `acquire_timeout` bounds the first use (including `open()`), and `init`,
`setup` and `reset` are the pool hooks.

The constructor does no IO, so a module-level database is fine: the instance starts on the first use (the first query,
`open()` or `acquire()`), and closing the database closes the instance. The pool has one session, because the engine
serves one session at a time, so there is no size, lifetime or ping setting.

```python test
import asyncio

from rowx import query
from rowx.embedded import Database


async def main() -> None:
    async with Database() as db:
        assert (db.max_size, db.directory) == (1, None)
        await query(t"CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL)").execute(db)
        await query(t"INSERT INTO users VALUES ({1}, {'alice'})").execute(db)
        assert await query[str](t"SELECT name FROM users").one(db) == "alice"
        assert "18" in await query[str](t"SHOW server_version").one(db)
        async with db.transaction() as tx:
            await query(t"INSERT INTO users VALUES ({2}, {'bob'})").execute(tx)
        async with db.acquire() as conn:
            assert await query[int](t"SELECT count(*) FROM users").one(conn) == 2

    # a second database is a second, independent one
    async with Database() as db:
        assert await query[int](t"SELECT count(*) FROM pg_tables WHERE tablename = 'users'").one(db) == 0


asyncio.run(main())
```

`ready` is true once the instance has started, `await db.open()` starts it ahead of the first query (and waits for it
for at most `acquire_timeout`), and `db.close()` closes it. `db.terminate()` is the synchronous stop: it abandons an
in-memory database at once, and closes a directory database properly, which waits for a running statement to finish.
Without `async with`, call `close()` yourself.

### The DSN

`db.dsn` is a plain Unix-socket DSN, so anything that speaks to Postgres (psycopg included) can use it. It is available
once the database has started (`open()` first) and raises `InterfaceError` before that and after `close`. Another client
can only get the session while the database itself holds none: after a query the database keeps its idle connection
until it has been idle for 10 minutes, so call `db.expire_connections()` first.

```python test
import asyncio

from rowx import query
from rowx.embedded import Database
from rowx_postgres import connect


async def main() -> None:
    # Database("/abs/path") persists instead
    async with Database() as db:
        async with await connect(db.dsn) as conn:
            await query(t"CREATE TABLE notes (body text)").execute(conn)
            await query(t"INSERT INTO notes VALUES ({'hello'})").execute(conn)
        # the first connection is closed, so the next one gets the session
        async with await connect(db.dsn) as conn:
            assert await query[str](t"SELECT body FROM notes").one(conn) == "hello"


asyncio.run(main())
```

A directory database has one owner. A second `Database` (or `rowx migrate`) on a directory another process holds fails
at once with an `InterfaceError` that names the owner's pid. The directory keeps real durability (`fsync` on); an
in-memory instance runs with it off.

Do not use a database or a connection after a `fork` once it has started: the engine's threads exist only in the process
that started it, so the child gets an `InterfaceError` and the parent's instance is untouched. A `Database` that never
started in the parent starts normally in a forked child, and creating the database after the fork is always safe.

## URLs

The command line and the pytest plugin take a URL, because they have no code to construct a `Database` from. Generic
`connect`, `Pool` and `ConnectOptions.from_dsn` do not accept one (an `InterfaceError`: it is not a connection string);
to switch between a server and an embedded database at run time, choose in your own code.

| URL | Database |
|---|---|
| `embedded://` | A fresh in-memory database, gone when its owner closes. |
| `embedded:///absolute/path` | A database kept in that directory between runs. One owner at a time. |

Nothing else may appear in the URL: no host, port, user or password, no relative path and no `sslmode` (they are a
`ValueError`). Other query parameters are passed on to the connection, so `embedded://?options=-c%20search_path%3Dapp`
works.

## The one-session rule

An embedded database serves one session at a time. A second client that connects while a session is running gets
SQLSTATE `53300` (`too_many_connections`) with a hint, and the first session is not disturbed.

```python test
import asyncio

from rowx.embedded import Database
from rowx_core import DatabaseError
from rowx_postgres import connect


async def main() -> None:
    async with Database() as db, db.acquire():
        try:
            await connect(db.dsn)
        except DatabaseError as error:
            assert error.sqlstate == "53300"
            assert error.hint is not None
            assert "max_size=1" in error.hint
        else:
            raise AssertionError("the second session was admitted")


asyncio.run(main())
```

Closing one session and opening the next never sees this: a client that arrives while the previous session is being
torn down (`ROLLBACK` then `DISCARD ALL`) waits for it, so every session starts from clean state. In practice:

- A `Database` is already a pool of one; a pool you build on `db.dsn` needs `max_size=1`.
- Do not hold a `Connection` from `db.dsn` and also use the `Database`. The database waits for the slot, then fails with
  `PoolTimeoutError` whose cause is the `53300` error.
- Code that opens several connections at once (a worker pool, `asyncio.gather` over `connect`) needs a server, or one
  instance per worker.

## Cancellation and timeouts

The engine cannot interrupt a running statement.

- A `CancelRequest` is read and dropped, never served. Cancelling a task (`asyncio.timeout`, `Task.cancel`) returns
  control to you at once, but the statement keeps running inside the engine, and the next statement on that connection
  waits for it to finish. A `pg_sleep(3)` abandoned after 0.3 s held the next query for the remaining 2.7 s.
- `SET statement_timeout` is accepted and shown by `SHOW`, but it never fires: a 300 ms timeout let `pg_sleep(1.5)` run
  to the end. This includes the `statement_timeout` of `[tool.rowx]`, which bounds `rowx check` against a server and has
  no effect here. A statement that never ends hangs the process.
- A close waits for a running statement. Ctrl-C handling in the CLI is described under [the checker](#check).

Tests of cancellation, of timeouts and of anything that measures concurrency belong on a real server.

## Check

`rowx check` against an `embedded://` URL needs no database of yours. Set it as the URL, as for any server:

```sh
DATABASE_URL=embedded:// rowx check
```

- An in-memory URL **implies `--scratch`**. The check builds a database from your migrations, so
  `[tool.rowx.migrate]` is required (without it the exit status is 2). It has no drift gate, because the database is
  built from the files.
- `--jobs N` (default one per CPU, at most four) starts one instance per worker: the migrated database is started
  once, and the other workers boot from a snapshot of it. A run never starts more instances than it has distinct
  statements.
- A directory URL (`embedded:///abs/path`) is checked as it is: its instance starts once, the drift gate runs against it
  (pending migrations are an error, `--no-drift-check` skips it), and every query is described on it with one worker,
  whatever `--jobs` says. `--scratch` with a directory is a usage error.
- `rowx check-compat` accepts `embedded://` as well: each revision's schema is built in memory, with one instance per
  worker (`--jobs`), and no server warning is printed. A directory URL is refused (exit 2), because both schemas are
  built from the migrations.
- A failing migration reads the same as on a server: "the migrations failed on the scratch database: ...".
- Ctrl-C: an in-memory run stops at once. A directory run waits for the running statement and says so; a second Ctrl-C
  aborts, leaving the directory as a crash would (it opens again).

```python test
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PYPROJECT = """
[tool.rowx]
include = ["."]
database_url_env = "DATABASE_URL"

[tool.rowx.migrate]
dir = "migrations"
"""
MIGRATION = "CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL);\n"
APP = """
from rowx import query

type UserRow = tuple[int, str]

users = query[UserRow](t"SELECT id, name FROM users")
"""


def rowx(root: Path, *args: str, url: str = "embedded://") -> subprocess.CompletedProcess[str]:
    code = "import sys; from rowx.check.cli import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=root,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "migrations").mkdir()
    (root / "migrations" / "0001_users.sql").write_text(MIGRATION)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "app.py").write_text(APP)

    # an in-memory URL builds the database from the migrations, for each of four workers
    clean = rowx(root, "check", "-j", "4")
    assert (clean.returncode, clean.stdout, clean.stderr) == (0, "", ""), clean.stdout + clean.stderr

    # a query that does not match the schema is reported
    (root / "app.py").write_text(APP.replace("SELECT id, name", "SELECT id, nickname"))
    broken = rowx(root, "check")
    assert broken.returncode == 1
    assert 'column "nickname" does not exist' in broken.stdout

    # without migrations there is nothing to build the database from
    (root / "app.py").write_text(APP)
    (root / "pyproject.toml").write_text(PYPROJECT.split("[tool.rowx.migrate]")[0])
    refused = rowx(root, "check")
    assert refused.returncode == 2
    assert "needs a `[tool.rowx.migrate]` table" in refused.stderr
```

## Migrate

`rowx migrate run`, `info`, `revert` and `skip` work on a directory URL. The instance is started once, outside any
connection retry, so a directory that another process owns fails at once, and it is closed when the command ends.

```sh
rowx migrate run --database-url embedded:///var/tmp/devdb
```

An in-memory URL is exit 2, because its database would be gone when the command exits. `rowx database` (create, drop,
...) is exit 2 for every embedded URL: there is one database, `postgres`, and it is the engine's.

To migrate from code, run a `Migrator` on a connection or a pool, as for a server:

```python test
import asyncio
import tempfile
from pathlib import Path

from rowx import query
from rowx.embedded import Database
from rowx_postgres import Migrator

with tempfile.TemporaryDirectory() as tmp:
    migrations = Path(tmp) / "migrations"
    migrations.mkdir()
    (migrations / "0001_accounts.sql").write_text("CREATE TABLE accounts (id int PRIMARY KEY);\n")

    async def main() -> None:
        migrator = Migrator(migrations)
        # a directory database keeps its migrations; the second run finds nothing to apply
        for expected in (1, 0):
            async with Database(f"{tmp}/db") as pool:
                assert len(await migrator.run(pool)) == expected
                assert await query[int](t'SELECT count(*) AS "n!" FROM accounts').one(pool) == 0

    asyncio.run(main())
```

## The pytest plugin

`rowx.testing` works on an embedded URL. Point `rowx_database_url_env` (or `database_url_env` in `[tool.rowx]`) at a
variable that holds `embedded://`, and the fixtures are the same:

| Fixture | Value on `embedded://` |
|---|---|
| `rowx_database_url` | The socket DSN of an instance that this test owns. |
| `rowx_pool` | An open `Pool` on it with `max_size=1` and a 15 s acquire timeout. |
| `rowx_connection` | A `Connection` to it. |

Each test boots its own instance from a snapshot of the migrated database, so no test sees another's rows. Booting and
dropping an instance cost about 0.4 s per test on an Apple-silicon Mac: about 0.2 s to boot and 0.2 s to tear down. The
first test of a process also pays about 0.7 s once, and the first run of a project builds the snapshot. There is nothing
to create, so no `CREATEDB` role, and nothing is left behind.

The snapshot is built once from your migrations and cached in pytest's cache directory, in
`<cache>/d/rowx-embedded/<p>_<h>.snapshot`. `<p>` identifies the project (the SHA-256 of the migrations path) and `<h>`
hashes everything the snapshot depends on, so a changed migration, rowx, companion or engine version, extension set or
snapshot format builds a new one. A build takes a file lock per project, so `pytest -n 2` workers build it once. A new
build removes this project's stale snapshots, and `--rowx-keep-templates` keeps them. With `-p no:cacheprovider` the
snapshot is built once per session in a temporary directory and removed at the end.

Things that differ from a server:

- **One session per test.** `rowx_connection` holds the instance's only session, so a test that also uses `rowx_pool`
  waits 15 s and fails with `PoolTimeoutError`, whose cause is the `53300` error and its hint. Use one of the two, or
  build a `Pool` that is not the plugin's.
- `embedded:///dir` is refused (a directory is one persistent database, and the plugin wants a fresh one per test).
- If the companion is not installed, the session fails with the install hint (it does not skip), because an embedded
  URL was asked for.
- A test's instance is abandoned at the end of the test, not closed, so a statement the test left running does not hold
  the session up (it dies with the process).

```python test
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

CONFTEST = 'pytest_plugins = ["rowx.testing"]\n'
PYPROJECT = """
[tool.rowx]
database_url_env = "DATABASE_URL"

[tool.rowx.migrate]
dir = "migrations"
"""
TEST = """
import asyncio

from rowx import query


def test_each_test_gets_a_clean_migrated_database(rowx_pool):
    async def work():
        await query(t"INSERT INTO accounts (id) VALUES (1)").execute(rowx_pool)
        return await query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool)

    assert asyncio.run(work()) == 1


def test_another_test_starts_empty(rowx_pool):
    count = asyncio.run(query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool))
    assert count == 0
"""

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "migrations").mkdir()
    (root / "migrations" / "0001_accounts.sql").write_text("CREATE TABLE accounts (id int PRIMARY KEY);\n")
    (root / "conftest.py").write_text(CONFTEST)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "test_accounts.py").write_text(TEST)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=root,
        env={**os.environ, "DATABASE_URL": "embedded://"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
```

A complete project that is checked and tested this way, with no Docker, is in
[`examples/embedded`](https://github.com/mplemay/rowx/tree/main/examples/embedded):

```sh
cd examples/embedded
DATABASE_URL=embedded:// rowx check
DATABASE_URL=embedded:// pytest
python notes.py          # runs against an in-memory database; no URL needed
```

## How fast

Measured by `bench/embedded.py` on an Apple M3 (8 cores, 16 GB, macOS), a release build of `rowx-postgres`, a
`postgres:18` container on the same machine for the server rows, and the machine otherwise idle. Median of the runs; no
number is a gate.

| What | Embedded | Server (Docker) |
|---|---|---|
| Cold start: `Instance.start()` in a fresh process, to the first query | 0.36 s | |
| Boot from a snapshot (40 MB, the example's schema): first in a process, later ones | 0.72 s, 0.19 s | |
| `rowx check` on `examples/embedded`, whole process, `-j 4` | 1.73 s | 0.21 s (`--scratch`), 0.16 s (migrated database) |
| `rowx check` the same, `-j 1` | 0.75 s | |
| `fetch_100k` (100,000 rows of `int4`, `text`, `timestamptz`) with `all()` | 59 ms | 53 ms |
| the same with `fetch()` (streaming), and with a scalar `int` row | 92 ms, 19 ms | 73 ms, 13 ms |

The engine is not faster than a server, and on a small project `rowx check` is slower on it: the boots dominate (an
interpreter that only imports rowx takes 0.09 s). What it removes is the setup, not the time. Cold start is the engine
building a cluster in memory, and the first snapshot boot of a process also derives a seed from the snapshot, which is
why it costs more than the ones after it.

## Extensions

Every contrib extension the engine ships is built in and available to `CREATE EXTENSION`:

`amcheck`, `auto_explain`, `bloom`, `btree_gin`, `btree_gist`, `citext`, `cube`, `dict_int`, `dict_xsyn`,
`earthdistance`, `file_fdw`, `fuzzystrmatch`, `hstore`, `intarray`, `isn`, `lo`, `ltree`, `pageinspect`,
`pg_buffercache`, `pg_freespacemap`, `pg_surgery`, `pg_trgm`, `pg_visibility`, `pg_walinspect`, `pgcrypto`, `seg`,
`tablefunc`, `tcn`, `tsm_system_rows`, `tsm_system_time`, `unaccent` and `uuid-ossp`.

Nothing outside that set can be loaded. The set is part of the snapshot cache key. `rowx_embedded.EXTENSIONS` lists it
at run time.

## Where it differs from a server

Besides the one session and the missing cancellation and timeouts:

- Authentication is trust, over a Unix socket in a private (`0700`) directory under `TMPDIR`. There is no TCP listener,
  no TLS (`SSLRequest` is answered `N`, so use `sslmode=disable` with any other client) and no password. Keep `TMPDIR`
  short: the socket path must fit a Unix socket's limit (about 100 bytes), and the error says so.
- There is one database, `postgres`. Connecting to another name fails, and `CREATE DATABASE` is of no use. Isolation
  uses snapshots instead of template databases.
- The engine is 32-bit WASM, with no locale data: `lc_monetary` stays `C`, so the scale of `money` differs from a
  server with another locale. The time zone is UTC.
- `inet` and `cidr` binary values use WASI address-family bytes, so rowx's `inet` and `cidr` codecs do not round-trip.
  Cast them to `text` in the query.
- `xml` is not supported (the engine raises `0A000`), and there are no encoding conversion libraries: `SET
  client_encoding = 'SJIS'` fails with `58P01`.
- Every session reports the same `pg_backend_pid()`.
- Closing a directory database does not write a shutdown checkpoint, so the next open recovers it as after a crash. A
  checkpoint just before the close keeps identity and sequence values continuous (no skip of up to 32), but `UNLOGGED`
  tables are emptied by every reopen, where a server keeps them after a clean stop.
- Anything is slower than a native server. In memory, durability is off.

The tests of rowx mark each of these (`server(needs=...)` and `server(divergence=...)`) and skip them on the embedded
lane. When a database behaviour matters, verify it against the Postgres you deploy: the embedded engine is the right
place to check types and plans, and the wrong place to prove concurrency or timing.

## Not for production

Use a real server in production: it handles many sessions, honours `statement_timeout` and cancellation, speaks TLS and
needs passwords. The embedded engine exists so that a checkout can check, migrate and test with one `uv add`, and so
that CI needs no service container for that.

## Environment

- `ROWX_EMBEDDED_LOG=1` traces fallbacks and dropped connections to stderr.
- `TMPDIR` is where each instance makes its private root (`rxe-<pid>-...`). A process that dies without closing leaves
  one, and the next start sweeps the roots of dead processes.
