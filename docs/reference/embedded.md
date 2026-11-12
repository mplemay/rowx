# Embedded Postgres: reference

The contract of the embedded database (`rowx[embedded]`). The [guide](../guide/embedded.md) shows how to use it; this
page says what it promises. How it is built, why, how it is tested and what it depends on are on the
[Contributing](../contributing/architecture.md#embedded-companion) pages. Each section follows the topic page it
extends.

## Overview

An embedded database is a PostgreSQL 18 that runs inside the Python process: no server, no Docker and no child process.
It is development, checking and tests only. Production keeps a real server.

- An embedded database **is** a live Postgres 18 for the purposes of the checker's "always needs a live database" rule:
  the engine reports `server_version` 18.4 and answers `PREPARE` and describe like a server.
- In code, `rowx.postgres.embedded.Database` opens one. The DSN grammar gains the `embedded` scheme, accepted by the
  URL-taking entry points (`rowx check`, `rowx check-compat`, `rowx migrate` and the pytest plugin), and refused by
  `connect`, `Pool` and `ConnectOptions.from_dsn`.
- It is optional. `rowx-core` keeps zero runtime dependencies and no WASM payload; `import rowx_postgres` never imports
  the engine.

## Architecture

The layout of the companion (`rowx-embedded`, `rowx-embedded-host` and the adapter in `rowx_postgres`), build isolation,
threads and runtime ownership, and the front proxy are under
[Architecture](../contributing/architecture.md#embedded-companion). The rules the front proxy enforces are in
[Driver](#driver).

## API

### `rowx.postgres.embedded`

`rowx.postgres.embedded` re-exports `rowx_postgres.embedded`, which exports only `Database`.

```python
class Database(rowx_postgres.Pool):
    def __init__(
        self,
        directory: str | PathLike[str] | None = None,
        /,
        *,
        options: ConnectOptions | None = None,
        max_size: int = 1,
        acquire_timeout: float = 30.0,
        idle_timeout: float | None = 600.0,
        max_lifetime: float | None = 1800.0,
        max_queries: int | None = None,
        ping_after: float | None = 30.0,
        init: Hook | None = None,
        setup: Hook | None = None,
        reset: Hook | None = None,
        reset_query: str | None = RESET_QUERY,
    ) -> None

    directory: str | None   # the normalised absolute path, or None for memory
    ready: bool             # the instance has started and the native pool is built
    closed: bool
    dsn: str                # a plain Unix-socket DSN
    async def wait_ready(self) -> None   # the same as open()
    # inherited: open, close, terminate, acquire, transaction, size, idle, waiting, min_size, max_size,
    # expire_connections, async with
```

- A `Database` is a `Pool` and an `Executor`: `query(...).one(db)`, `db.acquire()` and `db.transaction()` work, and
  `isinstance(db, Pool)` holds. `Migrator.run(db)` works before `open()`.
- `directory` must be absolute (a relative path is a `ValueError` in the constructor); `.`, `..` and repeated or
  trailing slashes are collapsed, and the root is refused. `None` is a fresh in-memory database.
- `options` carries `application_name`, `server_settings`, `statement_cache_size`, `connect_timeout`, `extra` and
  `database`. A `host`, `port`, `user`, `password` or `sslmode` is a `ValueError` ("... cannot be set for an embedded
  database"). The options are pointed at the instance's socket DSN when it starts.
- `max_size` is 1, and any other value is a `ValueError`. `min_size` is always 0, because a native `min_size` of 1 would
  hold the only session as soon as the pool opens.
- `dsn` is `postgresql://postgres@/postgres?host=<dir>&sslmode=disable`. Reading it before the database has started
  ("call open() first"), after `close`, or in a forked child raises `InterfaceError`. Another client can use it only
  while the database holds no session, checked out or idle; after a query the idle connection holds the session until
  `idle_timeout`, so call `expire_connections()` first.
- Snapshots are not public API in v1: `start_from_snapshot` and `take_snapshot` are used only by the checker and the
  plugin, and the instance type (`EmbeddedDatabase`) is private.

### The `embedded://` grammar

Parsed with `urllib.parse`, by the URL-taking entry points (`rowx check`, `rowx check-compat`, `rowx migrate`, the
scratch service and the pytest plugin). The scheme is `embedded` (case-insensitive), the network location is empty, and
the path is empty (memory) or an absolute path (a directory). These are `ValueError`s, and the message never echoes a
value:

- a host, port, user or password; a relative path; a fragment;
- the query parameters `host`, `hostaddr`, `port`, `user`, `password` and `sslmode`, and a repeated parameter.

Other parameters (`options`, `application_name`, `connect_timeout`, `dbname`, ...) are forwarded to the socket DSN, so
`options=-c search_path=...` works. Rust never receives `embedded://` or a missing host.

`connect`, `Pool` and `ConnectOptions.from_dsn` refuse an embedded URL with an `InterfaceError` that names
`rowx.postgres.embedded.Database(directory)`. Server-only entry points (`rowx database`, the admin service,
`open_checker` and the server provisioner) reject one with an `InterfaceError` too.

### Ownership and the pool

- A `Database` owns one instance, started **on first use** (`open`, `acquire` or a query) in a thread whose future every
  first-use caller shares. The constructor does no IO and never imports the engine, so a module-level database is fine
  and works across event loops. A failed start leaves nothing behind and the next use tries again. Closing or
  terminating the database closes the instance, a close that races the start waits for it, and a database that was
  never opened closes cleanly.
- Until the instance has started, `size`, `idle` and `waiting` are 0, `ready` is false and `closed` reports the
  database's own state.
- A database goes through Python for every query (as a pool with hooks does), because the native pool is built only
  once the instance has started.
- The first use is bounded by the acquire timeout; the start continues in its thread, and a timeout is a
  `PoolTimeoutError`.

### Fork rules and errors

| Condition | Result |
|---|---|
| Companion not installed, supported platform | `InterfaceError`, with `help: uv add "rowx[embedded]"`. |
| Unsupported platform (not macOS arm64, Linux x86_64/aarch64) | `InterfaceError`: "embedded Postgres is not available on this platform". |
| `API_VERSION` mismatch | `InterfaceError` naming both versions. |
| An engine that fails to import or start | `InterfaceError` with the cause. |
| A directory another instance holds | `InterfaceError`: "database directory ... is in use by pid N". |
| A snapshot the engine refuses (corrupt, or from another build) | `InterfaceError` (internally a `SnapshotRejectedError`, which the plugin rebuilds once). |
| A second concurrent session | `DatabaseError` `53300`, with a hint. |
| A `Database` used after a `fork` | `InterfaceError`. The parent's instance survives. |
| `connect`, `Pool` or `ConnectOptions.from_dsn` given an `embedded://` URL | `InterfaceError` naming `rowx.postgres.embedded.Database(directory)`. |
| Grammar violations, a relative directory, `max_size` other than 1, or server options on a `Database` | `ValueError`. |

## Driver

### DSN resolution

A `Database`'s options are resolved to a socket DSN before the driver sees them:
`postgresql://postgres@/postgres?host=<dir>&sslmode=disable`. Authentication is trust. The `dbname` of an embedded URL
is honoured only when it is `postgres`: the engine has one database, and another name fails with
`database "x" does not exist`.

### Embedded

- **One session per instance.** The slot is Idle, Active or Tearing down. A startup that finds it Active is answered
  with `FATAL 53300` ("sorry, too many clients already") and the hint "an embedded database serves one session at a
  time; use max_size=1", at once.
- **The teardown wait.** A startup that arrives while the previous session is rolling back and discarding its state
  waits for the slot, **up to 10 s**, so a sequential close-then-connect never sees `53300`. A startup that arrives
  while the active session's client has already gone but its statement is still running waits up to 30 s for it.
- **Cancels are ignored.** A `CancelRequest` is closed at once and cancels nothing. A running statement cannot be
  interrupted, and `statement_timeout` is accepted but **never fires**. The caller of a cancelled task is released, and
  the next statement on that connection waits for the running one.
- **Socket and auth.** The public socket is in a private root, `<TMPDIR>/rxe-<pid>-<random>`, created `0700` and locked
  for the life of the instance. The socket path must fit a Unix socket (at most 100 bytes), or the start fails and names
  `TMPDIR`. There is no TCP listener, no TLS and no password.
- **The owner lock.** A directory is opened by one instance. The engine's lock error carries no pid, so a directory's
  owner also records itself in `.<name>.rowx-owner` beside the directory (not inside, because the engine refuses a
  directory with entries it does not know). A second opener's error names the pid. A start sweeps the private roots of
  processes that died without closing.
- **Durability.** A directory instance keeps the engine's defaults (`fsync`, `full_page_writes` and `synchronous_commit`
  on), so a directory left by an aborted run opens again. A memory instance, including one booted from a snapshot, runs
  with all three off.
- **Snapshots.** A snapshot is a self-describing container (`ROWXSNAP`, a JSON header, the engine's backup) of a live
  instance, taken without stopping it. The header records the snapshot format, the engine and runtime versions, the
  SHA-256 of the wasm module and the extension set, and the payload digest; an instance refuses a snapshot that does not
  match all of them, before it trusts the payload.
- **Booting a snapshot.** An engine backup needs crash recovery on every boot. So the first boot of a snapshot in a
  process derives a cluster seed from it (restoring it into a private directory, opening and closing that cleanly, and
  packing the result); later boots of the same snapshot start a memory database from the seed. If the seed cannot be
  derived the snapshot is restored into a directory under the private root instead, which is the documented path and is
  slower. `Instance.boot` is `fresh`, `seed` or `restore`. The seed format is not documented by the engine, so it is
  pinned to `oliphaunt-wasix =0.3.2`.

## Checker

- **Selection.** An embedded URL makes the checker use the backend's embedded service (`backend.embedded(url)`), which
  owns the instances. Checking never imports user code.
- **Memory URL.** It implies `--scratch`: the checker starts one instance, runs the migrations on it, and, with `-j N`,
  takes one snapshot and boots `N - 1` more from it, one per worker. The number of workers is
  `min(jobs, distinct statements)`. It needs `[tool.rowx.migrate]` (exit 2 otherwise). There is no drift gate, because
  the database is built from the files.
- **Directory URL.** The instance is started once, outside connection retries, so a directory that is owned elsewhere
  fails at once (exit 2). The drift gate runs against its socket DSN (unless `--no-drift-check`), and every query is
  described on it with **one worker**, whatever `--jobs` says. `--scratch` with a directory URL is a usage error.
- **Errors** read as on a server ("the migrations failed on the scratch database: ..."), and every message has the
  socket DSN and its directory redacted.
- **Interrupts.** A memory run abandons its instances at once. A directory run, on the first Ctrl-C, says that it is
  waiting for the running statement and starts closing; a second Ctrl-C abandons the directory (it is left as a crash
  leaves it, and it opens again) and exits.
- **Ownership at the end.** No instance, socket directory or private root survives a diagnostic, an error or an
  interrupt of the check.
- The checker's `statement_timeout` does not fire on an embedded database.
- **`rowx check-compat`** accepts a memory URL: each revision's schema is built as the memory URL's scratch is (one
  instance per describe worker, `--jobs`), both schemas stay open for the run, and no disposable-server warning is
  printed. A directory URL is a `CheckError` (exit 2), because both schemas are built from the migrations. Messages
  redact the instances' socket DSNs and directories.

## Migrations

`rowx migrate run|info|revert|skip` accept a directory URL. The command starts the instance once, before and outside the
connection retries, runs on its socket DSN and closes it, and an instance that an interrupted run could not close in
time is abandoned. A memory URL is exit 2 ("an in-memory embedded database is gone when the command exits"), and so is
`rowx database ...` for any embedded URL. `rowx migrate add` touches no database.

Migrations also run from code (`Migrator.run` on a connection, pool or `Database`). The `PostgresMigrations` service
takes an embedded URL only so that a scratch can be built from it: it never connects to one, and `open` refuses.

## Testing

### The plugin

`rowx.testing` asks the backend for a provisioner, and the backend decides what a template is. On `embedded://`:

- **Snapshot mode.** The plugin builds a snapshot of the migrated database once, and each test boots its own instance
  from it (`rowx_database_url` is that instance's socket DSN, `rowx_connection` and `rowx_pool` connect to it). Dropping
  the database abandons its instance.
- **The pool** is `max_size=1` with a 15 s acquire timeout. That is the wait for the single slot, so a query that queues
  behind a statement of the same test for longer fails with `PoolTimeoutError`. A test that holds `rowx_connection` and
  uses `rowx_pool` fails after 15 s with the `53300` hint.
- **The cache.** The snapshot is `<cache>/d/rowx-embedded/<p>_<h>.snapshot` in pytest's cache directory
  (`config.cache.mkdir("rowx-embedded")`). `<p>` is the project prefix of the server template name, and `<h>` is the
  same hash, extended with the template's identity: the `rowx-embedded` version, the engine, runtime and wasm digest,
  the snapshot format, the sorted extension set and the URL's forwarded query. A file is the snapshot followed by its
  SHA-256, written beside its final name and renamed into it, so a reader sees all of a snapshot or none. A build takes
  an exclusive `flock` on `<p>.lock` per project (polled, so a cancelled wait leaves nothing locked), so `-n 2` builds
  once.
- **Stale snapshots.** A build removes the same project's other `<p>_*.snapshot` files, never another project's, unless
  `--rowx-keep-templates`.
- **No cacheprovider** (`-p no:cacheprovider`). The snapshot is built once per session in a temporary directory that is
  removed at the end.
- A snapshot that the engine refuses is rebuilt once, until a boot has worked.
- `embedded:///dir` is a usage error ("rowx.testing boots a fresh database from a snapshot for every test"), and a
  missing companion fails the session with the install hint.

The cost per test is about one boot and one teardown. Measured on macOS arm64 (a snapshot of 40 MB): the first boot of a
process is about 0.72 s (it derives the seed), later boots about 0.19 s, a close about 0.21 s, and the plugin adds about
0.4 s per test. See `bench/embedded.py`.

The markers, the lanes and the recorded divergences are under [Testing](../contributing/testing.md#embedded-lane).

## Dependencies

The engine is the WASIX build of PostgreSQL 18 and Wasmer, in `rowx-embedded` only. The verdicts and reasons are in
[Dependency evaluation](../contributing/dependencies.md).

## Security

- Each instance's socket lives in a private directory created `0700` under `TMPDIR` and locked for the instance's life.
  Authentication is trust, and the directory's permissions are the only access control. There is no network listener.
- A DSN of an embedded instance carries no secret, and every CLI message redacts it and its socket directory.
- There is no TLS (`SSLRequest` is answered `N`) and no password authentication, so the engine must not be used as a
  stand-in where either matters.
- A memory instance runs with `fsync` off; a directory is as durable as the engine's defaults.

## Non-goals

- Production use of the engine.
- Several concurrent sessions per instance, query interruption and `statement_timeout`.
- `CREATE DATABASE` workflows (isolation uses snapshots), TLS, password authentication and a TCP listener.
- Windows and macOS on Intel (no engine ships; the extra still installs).
- A public snapshot API.
- Extensions outside the shipped contrib set.

## Decisions

The decision table E1 to E12 is under [Decisions](../contributing/decisions.md#embedded-postgres-194).
