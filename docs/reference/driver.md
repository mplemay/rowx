# Driver

PostgreSQL connections, execution, codecs and pooling. Open them from `rowx.postgres`. The `rowx-postgres`
distribution imports the same objects from `rowx_postgres`. The query methods that run on an executor are in
[API](api.md). The SQLite driver, opened from `rowx.sqlite`, is [SQLite](#sqlite); the sections before it are
PostgreSQL.

The native module is `rowx_postgres._core`. Pure Rust lives in `crates/rowx-postgres/src/` (`dsn`, `tls`, `conn`,
`stmt_cache`, `exec`, `cancel`, `pool`, `wire/`) and in `crates/rowx-core/src/` (`state.rs` for claim and settle,
`cancel.rs` for cancellation plumbing, `row_shape.rs` for column-to-field mapping). The PyObject codecs and bindings
live under `packages/rowx-postgres/src/`. `crates/rowx-postgres` and `crates/rowx-core` do not use PyO3.

The driver rules for an embedded database (DSN resolution, one session, cancel and timeouts) are in
[Embedded Postgres](embedded.md#driver).

## Connection and DSN

```python
from rowx.postgres import ConnectOptions, Pool, connect

async with await connect("postgresql://app@localhost/app") as conn:
    ...

async with Pool("postgresql://app@localhost/app", max_size=20) as pool:
    ...
```

`connect` and `Pool` take `str | ConnectOptions`. `ConnectOptions.from_dsn` uses the parser in
`crates/rowx-postgres/src/dsn.rs`. `password` is omitted from `repr`. `extra` values render as `'<redacted>'`.
Keyword DSNs (`host=…`) are accepted. `options=-c search_path=…` round-trips into `server_settings` and is sent as
startup options, so it survives `RESET ALL`.

`sslmode` defaults to `prefer`, which encrypts and does not verify the server. `verify-full` checks the certificate
and the host. `channel_binding=require` refuses an authentication method that cannot be bound to the TLS channel,
and it is an error together with `sslmode=disable`. `verify-full` and `require` do not fall back to plaintext when
the server refuses SSL. A cancel request uses the same TLS mode as the connection. The TLS stack is rustls with the
ring provider (`crates/rowx-postgres/src/tls.rs`).

For a verified connection use `sslmode=verify-full` and `channel_binding=require`. Client certificates are
[#304](https://github.com/mplemay/rowx/issues/304).

## Option validation

Keys and values are checked before any socket is opened. A `server_settings` key must match the GUC grammar. An
`extra` key must match `^[a-z_]+$`. A NUL or other C0 control in a value is `InterfaceError`, and the message names
the key and not the value. `statement_cache_size` defaults to 256. `0` disables the cache.

No exception message, `repr` or checker diagnostic may contain a password. Redaction is by secret value and also
masks URL userinfo and `password=` tokens, so a DSN that fails to parse is still covered. It does not load a
database backend to do it. See [Connect options](security.md#connect-options).

## Execution

One operation runs on a connection at a time. A second concurrent call raises `ConnectionBusyError`. Pool checkout
is the exception: the pool waits, and a timeout is `PoolTimeoutError`.

Each call compiles SQL with that executor's compiler
(`packages/rowx-postgres/src/rowx_postgres/_compiler.py`). Capturing the t-string does not. The shape check (arity,
and for a named row the column-to-field mapping from `crates/rowx-core/src/row_shape.rs`) runs against the prepared
statement before execution, including when the result is empty.

A stale cached plan (`0A000` from `RevalidateCachedQuery`) is prepared again once. A vanished statement (`26000`)
clears the cache and runs once more. There is no second retry. Inside a transaction a failure that aborts the
transaction (`25P02`) is not retried. The error hint is `schema changed inside a transaction; ROLLBACK and retry`.

`fetch` claims the connection on the first `__anext__`. Streaming, early exit and cancellation of a stream are in
[Streaming](api.md#streaming).

## Codecs

Wire codecs in `crates/rowx-postgres/src/wire/` are pure functions over bytes. The PyObject codecs in
`packages/rowx-postgres/src/codec/` are the boundary. Dispatch uses the `tokio_postgres` type from the statement, not
the bare OID, so a domain uses its base codec. The type rules are [Type mapping](type-mapping.md).

A value that does not decode is `DecodeError`. A value that does not encode is `EncodeError`. The connection stays
usable. A parameter or message of 2³¹ bytes or more is the same: an error, not a broken connection.

## Binary arrays and ranges

Arrays and ranges are always sent in binary format. There is no text literal for an element or a range bound, so
untrusted element text cannot change the array's shape. Scalar parameters may still use text format. A scalar has
no element grammar, so a text-format scalar can corrupt only itself. `json.dumps` stays ASCII-only.

Nested lists must be rectangular. The encoder takes each dimension's length from the first element at that depth
and raises `EncodeError` (`the nested lists are ragged`) when another element has a different length or a
different depth. A NULL element is still binary: the wire length is `-1` and there is no payload. An element is
never sent as text. A list deeper than six levels is `EncodeError`. An empty list at any level, once the shape is
rectangular, encodes as an empty array.

## Session encoding watch

tokio-postgres sets `client_encoding=UTF8` at startup and does not notice a later `SET`. When `client_encoding`
leaves UTF-8, or `standard_conforming_strings` turns `off`, the connection is tainted. Every later call raises
`InterfaceError`, and a pool discards the connection. `UTF8` and `UNICODE` both count as UTF-8. A role or database
default of `standard_conforming_strings=off` taints the connection at connect. The taint lasts for the life of the
connection. `DateStyle` and `IntervalStyle` are not watched. Every decoder rowx uses for a value it cares about is
binary.

## Statement cache

Each connection keeps an LRU of prepared statements (`crates/rowx-postgres/src/stmt_cache.rs`), sized by
`statement_cache_size`. The checker's own connection sets the size to 0.

Behind a transaction-mode pooler, a prepared statement can land on a different server connection than the one that
prepared it. Connect with `statement_cache_size=0`, or use a pooler that tracks prepared statements. Session-mode
pooling keeps one server connection per client connection, and the default cache is safe there. The migrator also
needs a direct or session-mode connection, because its lock is session-level.

## Cancellation

Cancelling the task that is awaiting a query moves the connection from busy to settling
(`crates/rowx-core/src/state.rs`). The abandoned operation drains to `ReadyForQuery`, or to an error, before the
next operation starts. A cancel that arrives between operations does not sit on the connection and spoil a later
query. The Postgres cancel request is `crates/rowx-postgres/src/cancel.rs`, on the shared plumbing in
`crates/rowx-core/src/cancel.rs`. The request uses the connection's TLS settings. Leaving a `fetch` loop early does
not cancel. It drains. See [Streaming](api.md#streaming).

## Pool

`rowx.postgres.Pool` does no IO in the constructor. `async with` calls `open` and `close`. Defaults are
`min_size=0`, `max_size=10`, `acquire_timeout=30`, `idle_timeout=600`, `max_lifetime=1800`, `max_queries=None` and
`ping_after=30`. `min_size` must sit between 0 and `max_size`. A closed pool raises `InterfaceError`.

`acquire` waits up to `acquire_timeout` and raises `PoolTimeoutError`. The connection is released when the block
ends. A query method on the pool checks a connection out for that call only.

Idle connections older than `idle_timeout`, or older than `max_lifetime`, are closed instead of reused. `None`
disables that limit. After `ping_after` seconds idle, a connection is checked before use. `max_queries` closes a
connection after that many successful checkouts.

The pool core is `crates/rowx-core/src/pool/`. The Postgres connector is `crates/rowx-postgres/src/pool.rs`. The
Python constructor is `packages/rowx-postgres/src/rowx_postgres/_pool.py`, and the binding is
`packages/rowx-postgres/src/pool.rs`.

### Release

On release the pool runs `reset_query`, which defaults to `RESET_QUERY`:

```text
RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;
```

That drops the previous checkout's advisory locks, cursors, `LISTEN` subscriptions, `SET` values, role, session
authorization and temporary tables. The batch starts with `RESET SESSION AUTHORIZATION`, which `RESET ALL` does not
cover, and the unlock is schema-qualified. When rowx believes no transaction is open, an idle guard runs with the
reset. A custom `reset_query` is a separate round trip. `reset_query=None` sends no reset, and is unsafe when
untrusted SQL can run on a connection that will be reused. A reset that fails discards the connection.
`DEALLOCATE ALL` is not part of the reset. The statement cache is per connection and stays.

`init` runs once, when the connection is first established. `setup` runs after checkout and before the caller sees
the connection. `reset` runs before the connection returns to the idle set. A hook that raises discards the
connection.

## Transactions

`conn.transaction()` starts a transaction and returns an executor. Nested `transaction()` on that object is a
savepoint. A savepoint rejects `isolation`, `readonly` and `deferrable`. Those arguments belong to the outer
transaction only. Committing or rolling back the outer transaction ends the savepoints. A `Transaction` is not a
valid target for `Migrator`. Use the connection or the pool.

The binding is `packages/rowx-postgres/src/transaction.rs`. The Python type is
`packages/rowx-postgres/src/rowx_postgres/_transaction.py`.

## SQLite

The SQLite driver opens from `rowx.sqlite`. The `rowx-sqlite` distribution imports the same objects from `rowx_sqlite`:
`connect`, `ConnectOptions`, `Connection`, `Pool`, `Transaction` and `Migrator`. The query methods are [API](api.md).
The native module is `rowx_sqlite._core`. Pure Rust lives in `crates/rowx-sqlite/src/` (`dsn`, `conn`, `exec`, `cancel`,
`timeout`, `describe`, `pool`, `migrate`, `admin`, `types` and `value/`), and the bindings in
`packages/rowx-sqlite/src/`. `crates/rowx-sqlite` does not use PyO3. SQLite is bundled (`rusqlite` with
`libsqlite3-sys`, SQLite 3.51.3), so the driver never links the system library. Extension loading is compiled in and
stays disabled.

The type rules are [Type mapping](type-mapping.md#sqlite). The checker is [Checker](checker.md#sqlite), and migrations
are [Migrations](migrations.md#sqlite).

### SQLite URLs

`connect` and `Pool` take `str | ConnectOptions`. A string is parsed by `crates/rowx-sqlite/src/dsn.rs`, and the same
grammar is the one `rowx check`, `rowx migrate` and `rowx.testing` apply to a `sqlite:` URL.

| URL | Target |
|---|---|
| `sqlite:///relative.db` | a path relative to the working directory |
| `sqlite:////abs/path.db` | an absolute path |
| `sqlite:///C:/x.db` | a Windows drive path; `sqlite://///server/share/x.db` is a UNC path |
| `sqlite::memory:` | a private in-memory database |

The scheme is case-insensitive. One leading `/` is removed from the text after `sqlite://`. The path is
percent-decoded, and a NUL, an invalid escape or a result that is not UTF-8 is rejected. Everything else is
`InterfaceError`, and every message ends with a hint that shows the `sqlite:///` form. The message never repeats the
URL.

- A URL has no host (`sqlite://host/x.db`), no query string and no fragment. `mode`, `immutable`, `vfs` and `cache` are
  named in the message (`cache=shared` is the shared-cache mode, which is not supported). A literal `?` or `#` in a path
  is written `%3F` or `%23`.
- An empty path, `sqlite:///:memory:` (write `sqlite::memory:`), `sqlite:path`, a bare path, and a `file:` URI are
  rejected. A bare `file:` name that arrives through percent-decoding is prefixed with `./`, so SQLite opens a plain
  file and never parses it as a URI, although the bundled build compiles with `SQLITE_USE_URI`.
- A bare string that is a lone token ending in `.db`, `.sqlite`, `.sqlite3` or `.db3` is rejected as a bare path. Every
  other string without a scheme stays a PostgreSQL keyword DSN.
- SQLite needs the `rowx[sqlite]` extra. Without it, `import rowx.sqlite` raises `ImportError`, and a `sqlite:` URL
  given to the CLI exits 2 with a hint to install `rowx[sqlite]`.

A URL never says how to open the file: the caller does. `ConnectOptions.from_dsn(dsn, **overrides)` parses the URL and
replaces fields.

| Field | Default | Meaning |
|---|---|---|
| `path` | none | The file. Exactly one of `path` and `memory` is set. |
| `memory` | `False` | An in-memory database, which is always opened read-write and created. |
| `mode` | `"rw"` | `"ro"` opens an existing file read-only, `"rw"` an existing file for writing, `"rwc"` creates it as well. |
| `connect_timeout` | `None` | Seconds a statement waits for a lock, which is SQLite's busy timeout. Unset is 5 seconds. |
| `statement_timeout` | `None` | Seconds a statement may run before it is interrupted. `0` and `None` mean no limit. |
| `statement_cache_size` | 256 | Prepared statements kept per connection. `0` disables the cache. |
| `read_only` | `False` | Opens the file read-only and sets `PRAGMA query_only = ON`. It wins over `mode`. |

A negative or non-finite number of seconds is `ValueError`. A `mode` that is not one of the three is `InterfaceError`,
and so is a missing path or a path together with `memory`. A missing file with `rw` is `DatabaseError` `58030`
(`SQLITE_CANTOPEN`). A DSN cannot ask for creation: `rwc` exists only on `ConnectOptions`, and `rowx database create`
and a migration are the other ways to make a file. `ConnectOptions.with_connect_timeout(seconds)` returns a copy.

### SQLite pragmas

Every setting is applied explicitly when the connection opens, because the bundled build changes SQLite's defaults
(`SQLITE_DEFAULT_FOREIGN_KEYS=1`):

- `busy_timeout` from `connect_timeout`, always set.
- `foreign_keys=ON`, `recursive_triggers=OFF` and `synchronous=FULL`, so durability matches PostgreSQL's default.
- The `DQS_DML` and `DQS_DDL` configuration flags are off, so a mistyped `"colum"` is an error and not a string literal.
- `journal_mode=WAL` for a file opened read-write. A switch that does not take effect fails the open: it is
  `SQLITE_READONLY` (`25006`) when SQLite reports that, and otherwise `SQLITE_ERROR` (`HY000`) naming the mode the
  database stayed in. `:memory:` stays in `memory`. WAL is not supported on a network filesystem.
- A read-only handle (`mode="ro"` or `read_only=True`) never switches the journal mode and keeps what it finds. Opening
  a WAL file read-only needs a usable `-shm` and `-wal`, or a writable directory.
- `read_only=True` also turns `PRAGMA query_only` on.

`rowx check` opens its own session with `trusted_schema=OFF` and the defensive flag as well
([Security](security.md#sqlite)).

### SQLite execution

Each connection owns one worker thread, named `rowx-sqlite`, that opens the database and runs every call as a job.
SQLite never runs on a tokio worker or while the GIL is held, and the GIL is released around every call. The worker
ends and closes the database when the connection is closed or dropped.

One operation runs on a connection at a time, as on PostgreSQL: a second raises `ConnectionBusyError`, and an open
stream holds the connection until it ends.

Each call compiles SQL with the SQLite compiler (`packages/rowx-sqlite/src/rowx_sqlite/_compiler.py`):

- Placeholders are `?1`, `?2`, ... numbered densely from 1. The same expression twice is two parameters.
- The compiler tracks strings, quoted names (`"x"`, `` `x` ``, `[x]`) and comments with SQLite's lexer, which has no
  dollar quotes and no escape strings.
- A parameter sigil in static text is a `TypeError`: `?`, `?N`, `:x`, `@x`, `$x` and `#x`. SQLite would read it as a
  parameter. `$` inside a name such as `a$b` is an identifier character, not a sigil. An interpolation inside a string
  literal, a quoted name or a comment is a `TypeError` as well, where the PostgreSQL compiler emits it as text.
- The adjacency space rule is the one of [API](api.md#t-string-compilation), with `?n` in place of `$n`.
- `{x:ident}` in `query_unchecked` has no 63-byte limit. The limit is `SQLITE_MAX_LENGTH`.
- There is no fixed parameter ceiling. A statement with more parameters than the connection's
  `SQLITE_LIMIT_VARIABLE_NUMBER`, which is read when it connects, is `InterfaceError` before it runs.

The shape check runs against the prepared statement before execution, as on PostgreSQL. A stale prepared statement is
prepared again by SQLite itself. The statement cache is per connection.

- `execute` runs one statement and returns `QueryResult(rows_affected)`. It is `InterfaceError` for a statement that
  returns rows (a `SELECT`, `INSERT ... RETURNING`, a `PRAGMA` that answers), and it refuses before the statement
  runs, so nothing is written. Use `one`, `all` or `fetch` for those. `rows_affected` is `0` for DDL.
- A string of several statements is `InterfaceError` at run time, and an empty one is too. A query is one statement.
- Stored values that do not decode are `DecodeError`, and a value that does not encode is `EncodeError`. The
  connection stays usable.

`statement_timeout` is per statement, control statements such as `BEGIN` and `COMMIT` included. The clock starts when
the statement is about to step and not during prepare. For a stream it is per batch, and it does not run while the
worker waits for the consumer. Migration scripts are exempt. An expired timeout is `DatabaseError` `57014`
(`SQLITE_INTERRUPT`).

### SQLite transactions

`conn.transaction(mode=None, read_only=False)` and `pool.transaction(...)` return a `Transaction`, an executor, with
the semantics of [Transactions](#transactions): a clean exit commits, an exception or a cancel rolls back, and
`commit()` and `rollback()` end it early.

| Call | Begins |
|---|---|
| `transaction()` | `BEGIN IMMEDIATE`, which takes the write lock at once and waits for it up to `connect_timeout` |
| `transaction(mode="deferred")` | `BEGIN DEFERRED` |
| `transaction(mode="exclusive")` | `BEGIN EXCLUSIVE`, which is the same as `IMMEDIATE` in WAL mode |
| `transaction(read_only=True)` | `BEGIN DEFERRED` with `PRAGMA query_only = ON` for its length |

`mode` is `"deferred"`, `"immediate"` or `"exclusive"`, and anything else is `InterfaceError`. `read_only=True` with a
mode other than `"deferred"` is `InterfaceError`. On a connection that is itself `read_only`, the default is
`BEGIN DEFERRED`. A deferred transaction that writes after it read can fail at once with `SQLITE_BUSY_SNAPSHOT`
(`40001`), without waiting for the busy handler: begin a transaction that writes as `IMMEDIATE`.

A nested `transaction()` on a `Transaction` is a `SAVEPOINT` and takes no `mode` or `read_only` (`InterfaceError`). The
outermost level is always an explicit `BEGIN`, and a savepoint exists only inside an open transaction. After
`ROLLBACK TO` the savepoint stays open and still needs its `RELEASE`, which leaving the block does. A rejected
`COMMIT` rolls back. A `Transaction` is not a valid target for `Migrator`.

A failed statement does not end the transaction, as it does on PostgreSQL. The helper still refuses to commit a block
that swallowed an error: leaving it cleanly raises `InterfaceError` after rolling back, and rolling back to a savepoint
clears that mark. Only an interrupted write aborts a transaction, which is the next section.

### SQLite cancellation

`sqlite3_interrupt` is connection-wide, and a no-op when nothing runs. Cancelling the task that awaits a call
interrupts the statement and settles the connection before the next call, as on PostgreSQL
([Cancellation](#cancellation)). Leaving a `fetch` early drains.

- An interrupted **write** inside an explicit transaction rolls the whole transaction back, and SQLite returns the
  connection to autocommit. Later statements in that `transaction()` raise `DatabaseError` `25P02` with `code`
  `SQLITE_INTERRUPT`, the exception PostgreSQL raises for an aborted transaction. The connection itself stays usable.
  An interrupted **read**, and a failed statement, leave the transaction open.
- A cancel that lands before a statement steps fails it with `SQLITE_INTERRUPT` and leaves an explicit transaction
  open. A cancelled `BEGIN` that was waiting for the lock leaves no transaction behind.
- The busy handler does not wake on an interrupt. A cancel or a `statement_timeout` during a lock wait ends after
  about `connect_timeout` with `SQLITE_BUSY` (`55P03`), not `57014`.
- `close()` while a call runs interrupts it and raises `InterfaceError`.

### SQLite pool

`rowx.sqlite.Pool(options, *, min_size, max_size, acquire_timeout, idle_timeout, max_lifetime, max_queries, ping_after,
init, setup, reset)` is the pool of [Pool](#pool) with these differences:

- It is one pool. Every connection reads and writes, and SQLite serialises the writers: a transaction takes the write
  lock up front and waits for it up to `connect_timeout`. There is no separate reader pool.
- The defaults for a file are those of the PostgreSQL pool, including `max_size=10`.
- There is no `reset_query`. On release the pool rolls back an open transaction and puts `query_only` back to what the
  connection had when it connected, then runs the `reset` hook. A connection that cannot be returned to autocommit is
  discarded.
- A `sqlite::memory:` pool holds one connection, created when the pool opens, with no idle timeout, no lifetime and no
  query cap, because the database lives and dies with that connection. Options left unset resolve to
  `min_size=1`, `max_size=1`, `idle_timeout=None`, `max_lifetime=None` and `max_queries=None`. An explicit value that
  contradicts that is `ValueError`. The pool connects once and refuses to reconnect: when the connection is lost, every
  acquire fails at once with `InterfaceError` saying the pool is permanently broken, instead of silently handing out an
  empty database.

### SQLite concurrency

One writer is in at a time, and readers proceed beside it in WAL mode. Writers wait for the lock for up to
`connect_timeout`, then fail with `SQLITE_BUSY` (`55P03`). `SQLITE_LOCKED` has the same SQLSTATE, and `code` tells the
two apart. A file on a network filesystem is not supported, because WAL needs shared memory. A pool used in a forked
child raises `InterfaceError`: create it after the fork.
