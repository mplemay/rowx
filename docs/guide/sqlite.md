# SQLite

rowx runs the same queries against a SQLite file or an in-memory database. The `sqlite` extra installs the backend with
a bundled SQLite ([version](../reference/driver.md#sqlite)), so no system `libsqlite3` is involved.

```sh
uv add "rowx[sqlite]"
```

**Import from `rowx.sqlite`.** `connect`, `ConnectOptions`, `Connection`, `Pool`, `Transaction` and `Migrator` are the
`rowx_sqlite` objects. An install of `rowx-core` and `rowx-sqlite` without the `rowx` command imports them from
`rowx_sqlite`. The distributions are [Packages](../packages.md). Nothing SQLite-specific is added to the top-level
`rowx` namespace.

The `python test` blocks on this page create their own temporary databases and remove them. The rest of the guide
applies unchanged: [Queries](query.md), [Rows](rows.md), [Streaming](streaming.md) and [Errors](errors.md). This page
is what differs. The contract is in the [driver](../reference/driver.md#sqlite), the
[type mapping](../reference/type-mapping.md#sqlite) and the [checker](../reference/checker.md#sqlite) references.

## The URL

| URL | Database |
|---|---|
| `sqlite:///app.db` | `app.db` in the working directory |
| `sqlite:////var/data/app.db` | the absolute path `/var/data/app.db` |
| `sqlite:///C:/data/app.db` | a Windows drive path |
| `sqlite::memory:` | a private in-memory database |

Write an absolute path as `f"sqlite:///{path.as_posix()}"`. The path is percent-decoded, so a literal `?` or `#` is
`%3F` or `%23`. Every other form is rejected with a hint that shows the `sqlite:///` form: `sqlite:app.db`, a bare path
such as `app.db`, `sqlite://host/app.db`, a `file:` URI and any query string. The CLI is narrower about query strings
([Checker](../reference/checker.md#sqlite)): it names only `mode=memory`, `cache=shared`, `immutable` and `vfs`, and any
other query string is a plain unparseable URL, exit 2.

A URL never creates a file. A connection opens an existing file for writing, and a missing file raises `DatabaseError`
`58030` (`SQLITE_CANTOPEN`). Create one with `ConnectOptions(path=..., mode="rwc")`, with `rowx database create`, or by
running a migration on a connection opened that way.

## First query

```python test
import asyncio
import tempfile
from pathlib import Path

from rowx import query
from rowx.sqlite import ConnectOptions, Migrator, connect

type UserRow = tuple[int, str, str | None]

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    migrations = root / "migrations"
    migrations.mkdir()
    (migrations / "0001_users.sql").write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT);\n"
    )
    database = root / "app.db"

    async def main() -> None:
        # `mode="rwc"` creates the file
        async with await connect(ConnectOptions(path=str(database), mode="rwc")) as conn:
            await Migrator(migrations).run(conn)
            added = await query(t"INSERT INTO users (name, email) VALUES ({'alice'}, {None})").execute(conn)
            assert added.rows_affected == 1

        # a DSN opens the file that exists now
        async with await connect(f"sqlite:///{database.as_posix()}") as conn:
            uid = await query[int](t"SELECT id FROM users WHERE name = {'alice'}").one(conn)
            user = await query[UserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").one(conn)
            assert user == (uid, "alice", None)

    asyncio.run(main())
```

`.one`, `.optional`, `.all`, `.fetch` and `.execute` are the methods of [First query](get-started.md). `{name}` is a
bound parameter, numbered `?1`, `?2` and so on. A file database is opened in WAL mode with `synchronous=FULL` and
`foreign_keys=ON`, whatever the defaults of the SQLite build are
([Pragmas](../reference/driver.md#sqlite-pragmas)).

## Values and types

SQLite lets any column hold any kind of value, so rowx reads a column by its **declared type**, the name in
`CREATE TABLE`. `INTEGER`, `REAL`, `TEXT` and `BLOB` are `int`, `float`, `str` and `bytes`. A few conventional names
read as richer types: `BOOLEAN`, `DATE`, `TIME`, `DATETIME` or `TIMESTAMP`, `TIMESTAMPTZ`, `DECIMAL`, `UUID` and
`JSON`. The whole table is [Type tables](../types.md#sqlite).

```python test
import asyncio
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from rowx import DecodeError, EncodeError, query
from rowx.sqlite import ConnectOptions, connect

type Row = tuple[bool, date, datetime, datetime, Decimal, UUID, object]

with tempfile.TemporaryDirectory() as tmp:

    async def main() -> None:
        async with await connect(ConnectOptions(path=f"{tmp}/types.db", mode="rwc")) as conn:
            await query(
                t"""CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    done BOOLEAN NOT NULL,
                    day DATE NOT NULL,
                    at TIMESTAMP NOT NULL,
                    at_utc TIMESTAMPTZ NOT NULL,
                    price DECIMAL(10, 2) NOT NULL,
                    token UUID NOT NULL,
                    payload JSON NOT NULL
                )"""
            ).execute(conn)
            at = datetime(2026, 11, 24, 9, 30)
            at_utc = datetime(2026, 11, 24, 9, 30, tzinfo=UTC)
            payload = {"tags": ["a", "b"]}
            await query(
                t"INSERT INTO events (done, day, at, at_utc, price, token, payload) "
                t"VALUES ({True}, {at.date()}, {at}, {at_utc}, {Decimal('9.5')}, {UUID(int=7)}, {payload})"
            ).execute(conn)
            sql = query[Row](t"SELECT done, day, at, at_utc, price, token, payload FROM events")
            assert await sql.one(conn) == (True, at.date(), at, at_utc, Decimal("9.5"), UUID(int=7), payload)

            # a parameter has no type of its own, so a naive datetime bound to a TIMESTAMPTZ column is stored as it
            # is and fails when it is read back
            await query(t"INSERT INTO events (done, day, at, at_utc, price, token, payload) VALUES (0, '2026-11-24', {at}, {at}, 1, {UUID(int=8)}, 'null')").execute(conn)
            try:
                await sql.all(conn)
            except DecodeError:
                pass
            else:
                raise AssertionError

            # a value SQLite cannot store is refused before it is sent
            try:
                await query(t"INSERT INTO events (done) VALUES ({2**70})").execute(conn)
            except EncodeError:
                pass
            else:
                raise AssertionError

    asyncio.run(main())
```

Four rules explain most surprises:

- **A stored value that disagrees with its column's declared type is an error**, never silently converted. A `BOOLEAN`
  holds `0` or `1`, and `42` raises `DecodeError`.
- **A naive `datetime` is `TIMESTAMP` and an aware one is `TIMESTAMPTZ`.** An aware value is written as UTC and read
  back as UTC.
- **`DECIMAL` and `NUMERIC` columns have numeric affinity**, so SQLite itself turns the text `'9.50'` into the real
  `9.5` before rowx sees it. The `Decimal` you read is `9.5`. To keep exact text, declare the column `TEXT` and read a
  `str`.
- **A table declared `STRICT` accepts only `INT`, `INTEGER`, `REAL`, `TEXT`, `BLOB` and `ANY`.** There the rich types
  above do not exist: a date is a `TEXT` column read as `str`.

A column declared with no type, and every expression, is read by the kind of value stored in each row. The checker
types an expression by what the statement's bytecode proves: `count(*)` is a non-null `int`, `upper(name)` is a `str`,
and `coalesce(nickname, name)` is non-null. A column it cannot type, such as `max(id)` or a non-strict column declared
with no type, is *untyped* and accepts any annotation, and one it cannot prove non-null stays nullable until you say
otherwise: `max(id) AS "m!"`. A compound select is typed by every arm, not only the first.

## Transactions and errors

`transaction()` begins with `BEGIN IMMEDIATE`, so the write lock is taken at the start and a statement inside never
has to upgrade a read lock. A second writer waits up to `connect_timeout` (5 seconds by default).
`transaction(read_only=True)` is a deferred transaction on a connection with `PRAGMA query_only` on. A nested
`transaction()` is a savepoint.

A `DatabaseError` keeps its 5-character `sqlstate`, and `code` names the SQLite result code.

```python test
import asyncio
import tempfile

from rowx import DatabaseError, query
from rowx.sqlite import ConnectOptions, connect


async def failing(conn, sql) -> DatabaseError:
    try:
        await sql.execute(conn)
    except DatabaseError as error:
        return error
    raise AssertionError


with tempfile.TemporaryDirectory() as tmp:

    async def main() -> None:
        async with await connect(ConnectOptions(path=f"{tmp}/bank.db", mode="rwc")) as conn:
            await query(
                t"CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
                t"balance INTEGER NOT NULL CHECK (balance >= 0))"
            ).execute(conn)

            async with conn.transaction() as tx:
                await query(t"INSERT INTO accounts (name, balance) VALUES ({'ada'}, {10})").execute(tx)
                try:
                    async with tx.transaction() as savepoint:
                        await query(t"UPDATE accounts SET balance = balance - {20}").execute(savepoint)
                except DatabaseError as error:
                    assert (error.sqlstate, error.code) == ("23514", "SQLITE_CONSTRAINT_CHECK")
            balance = query[int](t"SELECT balance FROM accounts WHERE name = 'ada'")
            assert await balance.one(conn) == 10

            duplicate = await failing(conn, query(t"INSERT INTO accounts (name, balance) VALUES ({'ada'}, {1})"))
            assert (duplicate.sqlstate, duplicate.code) == ("23505", "SQLITE_CONSTRAINT_UNIQUE")
            missing = await failing(conn, query(t"SELECT * FROM nowhere"))
            assert (missing.sqlstate, missing.code) == ("HY000", "SQLITE_ERROR")

            try:
                async with conn.transaction(read_only=True) as tx:
                    await query(t"DELETE FROM accounts").execute(tx)
            except DatabaseError as error:
                assert (error.sqlstate, error.code) == ("25006", "SQLITE_READONLY")
            else:
                raise AssertionError

    asyncio.run(main())
```

A syntax error, a missing table and a missing column are all `HY000` with `code` `SQLITE_ERROR`; tell them apart by
`code` and the message. The full list is [Errors](../reference/api.md#errors).

## A pool

`Pool` takes a URL or `ConnectOptions`, like `connect`. All its connections read and write. SQLite lets one writer in
at a time, and a transaction that begins `IMMEDIATE` waits for the lock instead of failing at its first write, so
concurrent writers queue behind each other for up to `connect_timeout`. The defaults for a file are those of
[Executors](executors.md#pools). A `sqlite::memory:` pool is one connection, because the database lives and dies with
the connection that opened it.

```python test
import asyncio
import tempfile

from rowx import query
from rowx.sqlite import ConnectOptions, Pool

with tempfile.TemporaryDirectory() as tmp:

    async def main() -> None:
        async with Pool(ConnectOptions(path=f"{tmp}/pool.db", mode="rwc"), max_size=4) as pool:
            await query(t"CREATE TABLE hits (n INTEGER NOT NULL)").execute(pool)

            async def hit(n: int) -> None:
                async with pool.transaction() as tx:
                    await query(t"INSERT INTO hits (n) VALUES ({n})").execute(tx)

            await asyncio.gather(*(hit(n) for n in range(20)))
            assert await query[int](t'SELECT count(*) AS "n!" FROM hits').one(pool) == 20

        async with Pool("sqlite::memory:") as memory:
            assert memory.max_size == 1
            await query(t"CREATE TABLE scratch (n INTEGER)").execute(memory)
            assert await query[int](t'SELECT count(*) AS "n!" FROM scratch').one(memory) == 0

    asyncio.run(main())
```

## Check

`rowx check` opens the file read-only, prepares each checked statement without running it, and compares the result
with the row type. `sqlite::memory:` has no file to read, so it implies `--scratch`: the check builds a temporary
database from `[tool.rowx.migrate]` and describes against that.

```sh
rowx check src --database-url sqlite:///app.db
rowx check src --database-url sqlite::memory:
```

The checker proves less than it does on PostgreSQL, and says so. A column that SQLite does not guarantee to be
`NOT NULL`, and that the statement's bytecode does not prove non-null, is nullable, and the diagnostic note reads
`nullability not proven, assumed nullable` for a column that comes through a view, a subquery, a CTE, a compound
select, an outer join or an expression. Parameters are untyped, so there
is no `param-type-mismatch`. Narrow a column with `AS "col!"`, as in [Parameters](parameters.md#nullability-overrides).

```python test
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from rowx.sqlite import ConnectOptions, Migrator, connect

PYPROJECT = """
[tool.rowx]
include = ["src"]

[tool.rowx.migrate]
dir = "migrations"
"""
MIGRATION = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT);
CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), title TEXT NOT NULL);
"""
OK = """
from rowx import query

type UserRow = tuple[int, str, str | None]

users = query[UserRow](t"SELECT id, name, email FROM users WHERE id = {1}")
"""
JOIN = """
from rowx import query

titles = query[tuple[str, str]](t"SELECT u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id")
"""


def rowx(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    code = "import sys; from rowx.check.cli import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=root,
        env={key: value for key, value in os.environ.items() if key != "DATABASE_URL"},
        capture_output=True,
        text=True,
        check=False,
    )


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "migrations").mkdir()
    (root / "migrations" / "0001_init.sql").write_text(MIGRATION)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "src").mkdir()
    (root / "src" / "queries.py").write_text(OK)
    url = f"sqlite:///{(root / 'app.db').as_posix()}"

    async def build() -> None:
        async with await connect(ConnectOptions(path=str(root / "app.db"), mode="rwc")) as conn:
            await Migrator(root / "migrations").run(conn)

    asyncio.run(build())

    # a file is checked as it is, after the drift gate has compared it with the migrations
    clean = rowx(root, "check", "--database-url", url)
    assert (clean.returncode, clean.stdout, clean.stderr) == (0, "", ""), clean.stdout + clean.stderr

    # a LEFT JOIN makes every column nullable, so `str` is not enough
    (root / "src" / "queries.py").write_text(JOIN)
    joined = rowx(root, "check", "--database-url", url)
    assert joined.returncode == 1
    assert "nullability not proven, assumed nullable" in joined.stdout

    # an in-memory URL builds the database from the migrations, so no file is read
    scratch = rowx(root, "check", "--database-url", "sqlite::memory:")
    assert scratch.returncode == 1
    assert "nullability-mismatch" in scratch.stdout
```

## Migrations

SQL files are the same as for PostgreSQL ([Migrations](migrations.md)), and `rowx migrate` and `rowx database` work on a
file URL. `rowx migrate run --database-url sqlite:///app.db` needs the file to exist, so create it first with
`rowx database create`. An in-memory URL is exit 2, because the database would be gone when the command exits.

Each migration runs in its own `BEGIN IMMEDIATE` transaction and commits with its history row, so two migrators on one
file take turns. A file marked `-- no-transaction` runs outside any transaction, which `VACUUM`, `PRAGMA journal_mode`,
`ATTACH` and a table rebuild with `PRAGMA foreign_keys = OFF` need. A file with its own `BEGIN` or `COMMIT` is rejected.
SQLite has no schemas, so `create_schemas` is rejected. The rules are in the
[reference](../reference/migrations.md#sqlite).

## Tests

`rowx.testing` gives each test a copy of a migrated database file. A `sqlite:` URL in the environment variable selects
the backend, and it is never opened:

```sh
DATABASE_URL=sqlite::memory: pytest
```

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
    (root / "migrations" / "0001_accounts.sql").write_text("CREATE TABLE accounts (id INTEGER PRIMARY KEY);\n")
    (root / "conftest.py").write_text(CONFTEST)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "test_accounts.py").write_text(TEST)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=root,
        env={**os.environ, "DATABASE_URL": "sqlite::memory:"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

The plugin migrates a template file once per session, under the runner's cache directory, and copies it with SQLite's
backup API for each test. Editing a migration builds a new template. The details are
[Test plugin](../contributing/testing.md#test-plugin). SQLite tests need no server, no marker and no Docker.

## What differs from PostgreSQL

- **One writer at a time.** Reads proceed beside a write in WAL mode. A pool is not a way to write in parallel.
- **No schemas, roles or `CREATE DATABASE`.** A database is a file, and `rowx database create` creates one.
- **No arrays or ranges.** A `list`, `tuple` or `dict` parameter is bound as JSON text, so an IN-list is
  `WHERE id IN (SELECT value FROM json_each({ids}))`. `= ANY({ids})` is not SQLite.
- **No `::` casts or `$1`.** Cast with `CAST({x} AS TEXT)`. A `$1` or `:name` in the static text of a query is
  rejected, because SQLite would read it as a named parameter.
- **Parameters are untyped.** SQLite applies the column's affinity when it stores one, so a `str` bound into an
  `INTEGER` column is stored as an integer.
- **`execute` is for statements that return no rows.** A `SELECT`, `INSERT ... RETURNING` or a reading `PRAGMA` is
  `InterfaceError` before it runs. Use `one`, `all` or `fetch`. A string of several statements is `InterfaceError` too,
  and an empty or comment-only query is `DatabaseError` (`HY000`, `SQLITE_MISUSE`) through `execute`, but
  `RowShapeError` through a method that returns rows, because the row type is checked first.
- **Cancellation is connection-wide.** A cancelled statement is interrupted. The details, including what happens
  inside a transaction, are in [Cancellation](../reference/driver.md#sqlite-cancellation).
