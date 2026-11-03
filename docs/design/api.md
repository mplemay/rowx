# User-facing API

## Example

```python
import asyncio
from dataclasses import dataclass
from decimal import Decimal

from rowx import query
from rowx.postgres import ConnectOptions, Pool, connect

type UserRow = tuple[int, str, str | None]          # id, name, email

@dataclass(frozen=True, slots=True, kw_only=True)
class User:                                         # a named row: matched by column name
    id: int
    name: str
    email: str | None = None                        # a field with a default may have no column

async def main() -> None:
    async with await connect("postgresql://app@localhost/app") as conn:
        uid = 42
        user = await query[UserRow](
            t"SELECT id, name, email FROM users WHERE id = {uid}"
        ).one(conn)                                  # -> UserRow; raises RowNotFound

        maybe = await query[UserRow](
            t"SELECT id, name, email FROM users WHERE id = {uid}"
        ).optional(conn)                             # -> UserRow | None

        named = await query[User](
            t"SELECT name, id FROM users WHERE id = {uid}"
        ).one(conn)                                  # -> User; column order is irrelevant

        names = await query[str](
            t"SELECT name FROM users ORDER BY name"
        ).all(conn)                                  # -> list[str]  (scalar)

        async for name in query[str](
            t"SELECT name FROM users ORDER BY name"
        ).fetch(conn):                               # streams rows; name: str
            ...

        ids = [1, 2, 3]
        result = await query(
            t"UPDATE users SET name = {'x'} WHERE id = ANY({ids})"
        ).execute(conn)                              # -> QueryResult
        print(result.rows_affected)

        total = await query[Decimal](
            t'SELECT sum(amount) AS "total!" FROM orders'   # "!" = force NOT NULL
        ).one(conn)

    # a pool is an executor too: acquire, run and release in one call
    pool = Pool("postgresql://app@localhost/app", max_size=20)   # no IO; safe at module level
    async with pool:                                   # open() warms min_size; close() on exit
        user = await query[UserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").one(pool)
        async with pool.acquire() as conn:             # conn: Connection
            ...
        async with pool.transaction() as tx:           # BEGIN ... COMMIT / ROLLBACK
            await query(t"UPDATE users SET name = {'x'} WHERE id = {uid}").execute(tx)
            async with tx.transaction():               # SAVEPOINT
                ...

    opts = ConnectOptions.from_dsn("postgresql://app@localhost/app", statement_cache_size=0)
    async with await connect(opts) as conn:
        await conn.ping()

asyncio.run(main())
```

## Public surface

### `rowx` (`rowx/__init__.py`)

The shared operations. `rowx` never imports `rowx._core` or `rowx.postgres`, so `import rowx` loads no backend.

```python
from dataclasses import dataclass
from string.templatelib import Template
from typing import Self

class _BaseQuery[T]:                       # private; holds all behaviour
    _dynamic: ClassVar[bool]
    def __init__(self, template: Template, /) -> None: ...      # compiles eagerly ([t-string compilation](#t-string-compilation-rowx_templatepy))
    async def one(self, executor: Executor, /) -> T: ...
    async def optional(self, executor: Executor, /) -> T | None: ...
    async def all(self, executor: Executor, /) -> list[T]: ...
    def fetch(self, executor: Executor, /, *, batch_size: int = 1024) -> RowStream[T]: ...
    async def execute(self, executor: Executor, /) -> QueryResult: ...

class query[T = None](_BaseQuery[T]):            _dynamic = False
class query_unchecked[T = None](_BaseQuery[T]):  _dynamic = True

@dataclass(frozen=True, slots=True, kw_only=True)
class QueryResult:
    rows_affected: int

class RowStream[T]:                        # returned by fetch(); users never construct it
    def __aiter__(self) -> Self: ...
    async def __anext__(self) -> T: ...
    async def aclose(self) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...   # aclose()
    @property
    def result(self) -> QueryResult | None: ...

class Executor(Protocol):                  # structural; see below
    @property
    def closed(self) -> bool: ...
    # private SPI called by the query methods: _fetch, _stream, _execute
    # _fetch and _stream take a trailing `shape: RowShape | None`, the plan's named-row description (driver.md)
```

Requirements:

- `query` and `query_unchecked` are **siblings**. Neither subclasses the other, and users
  must not subclass either; the checker reports `indirect-query` if they do.
- Neither class may declare `__slots__` without a `__dict__`. `typing` silently fails to
  set `__orig_class__` on such instances. A test asserts that `__orig_class__` is visible
  on the instance.
- Pyright 1.1.414 and mypy 2.4.0 infer all of the following with no plugin and no
  overloads (verified):
  - `one -> T`, `optional -> T | None`, `all -> list[T]`, `execute -> QueryResult`;
  - `fetch -> RowStream[T]`, with `async for` over it yielding `T`;
  - the scalar, tuple and `type`-alias forms;
  - `query(t"..") -> query[None]`.

  Both reject `str` and f-string arguments.
- **Do not add overloads** that make `one` on `query[None]` return `NoReturn`.
  `query[Any]` matches `self: query[None]` too, so it would also be typed `NoReturn`
  (verified in both checkers). Misuse is caught by `missing-row-type` (checker) and
  `InterfaceError` (runtime).
- Every public async method **MUST** be a Python `async def` that awaits the Rust future
  inside. The pyo3-async-runtimes Future is not a coroutine, and `asyncio.create_task`
  rejects it. A test checks `inspect.iscoroutinefunction`.
  - `fetch` is a plain `def`: it returns a `RowStream` and does no IO.
  - `RowStream.__anext__`, `aclose`, `__aenter__` and `__aexit__` are `async def`. A test
    also checks that `asyncio.create_task(anext(stream))` works.
- **`Executor`** is a `typing.Protocol` over the private SPI that the shared query methods
  call: `_fetch`, `_stream` and `_execute`, plus a `closed` view. `Connection`, `Pool` and
  `Transaction` implement it.
  - `rowx` never imports `rowx.postgres`.
  - `RowStream` uses `closed`; on a pool it is per lease.
  - `_stream` returns a handle whose first `_next()` claims a connection: a synchronous
    claim on a Connection or Transaction, an async acquire on a Pool. The lazy claim of
    [Streaming](#streaming-fetch) is preserved.
  - The query methods take `Executor`, which widens the former `Connection` parameter
    (backward compatible for callers).

### `rowx.postgres`

```python
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from os import PathLike
from pathlib import Path
from typing import Final, Literal, Self

RESET_QUERY: Final[str]      # "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL; DISCARD TEMP;"

@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectOptions:
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    database: str | None = None
    sslmode: str | None = None
    sslrootcert: str | None = None
    channel_binding: Literal["disable", "prefer", "require"] | None = None
    connect_timeout: float | None = None
    statement_cache_size: int = 256
    application_name: str | None = None
    server_settings: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, str] = field(default_factory=dict)
    @classmethod
    def from_dsn(cls, dsn: str, /, **overrides: object) -> Self: ...

async def connect(options: str | ConnectOptions, /) -> Connection: ...

class Connection:                                          # an Executor
    async def close(self) -> None: ...                     # on a pooled connection: releases it
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...   # closes
    async def ping(self) -> None: ...                      # an empty simple query
    async def reset(self) -> None: ...                     # runs RESET_QUERY
    def transaction(self, *, isolation: str | None = None, readonly: bool = False,
                    deferrable: bool = False) -> Transaction: ...
    def detach(self) -> None: ...                          # pooled connections only
    @property
    def closed(self) -> bool: ...

class Pool:                                                # an Executor
    def __init__(self, options: str | ConnectOptions, /, *, min_size: int = 0, max_size: int = 10,
                 acquire_timeout: float = 30.0, idle_timeout: float = 600.0,
                 max_lifetime: float = 1800.0, max_queries: int | None = None,
                 ping_after: float = 30.0, init: Hook | None = None, setup: Hook | None = None,
                 reset: Hook | None = None, reset_query: str | None = RESET_QUERY) -> None: ...
    async def open(self) -> None: ...
    async def close(self, *, timeout: float | None = None) -> None: ...
    def terminate(self) -> None: ...                       # synchronous
    def acquire(self) -> PoolAcquireContext: ...           # awaitable and an async context manager
    async def release(self, conn: Connection, /) -> None: ...
    def transaction(self, *, isolation: str | None = None, readonly: bool = False,
                    deferrable: bool = False) -> Transaction: ...
    def expire_connections(self) -> None: ...
    async def __aenter__(self) -> Self: ...                # open()
    async def __aexit__(self, *exc: object) -> None: ...   # close()
    size: int; idle: int; waiting: int; min_size: int; max_size: int; closed: bool   # properties

class Transaction:                                         # an Executor
    def transaction(self) -> Transaction: ...              # SAVEPOINT; no isolation arguments
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    async def __aenter__(self) -> Self: ...                # BEGIN / SAVEPOINT
    async def __aexit__(self, *exc: object) -> None: ...   # COMMIT / ROLLBACK
```

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Migration:                                           # one entry per version
    version: int
    description: str
    kind: Literal["simple", "reversible"]
    path: Path                                             # the simple file, or the .up.sql
    down_path: Path | None                                 # the .down.sql of a reversible migration
    checksum: bytes                                        # SHA-384 of the up file's raw bytes
    down_checksum: bytes | None
    no_transaction: bool

@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationResult:
    migration: Migration
    elapsed: timedelta | None                              # None for a dry run or a skip

@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationStatus:
    version: int
    description: str
    state: Literal["applied", "pending", "modified", "missing", "dirty"]
    kind: Literal["simple", "reversible"] | None = None    # None when the migration is missing locally
    installed_on: datetime | None = None                   # applied (and modified, dirty) rows
    execution_time: timedelta | None = None                # None when not applied, or recorded as -1
    local_checksum: bytes | None = None
    applied_checksum: bytes | None = None

class Migrator:
    def __init__(self, path: str | PathLike[str], /, *, table: str = "_rowx_migrations",
                 create_schemas: Iterable[str] = (), locking: bool = True, ignore_missing: bool = False,
                 allow_out_of_order: bool = False) -> None: ...
    path: Path; migrations: tuple[Migration, ...]          # properties; migrations sorted by version
    async def run(self, executor: Connection | Pool, /, *, target: int | None = None,
                  dry_run: bool = False) -> tuple[MigrationResult, ...]: ...
    async def revert(self, executor: Connection | Pool, /, *, target: int | None = None, all: bool = False,
                     dry_run: bool = False) -> tuple[MigrationResult, ...]: ...
    async def skip(self, executor: Connection | Pool, /, *, target: int | None = None,
                   dry_run: bool = False) -> tuple[MigrationResult, ...]: ...
    async def info(self, executor: Connection | Pool, /) -> tuple[MigrationStatus, ...]: ...
```

`type Hook = Callable[[Connection], Awaitable[None]]`.

| Module | Exports |
|---|---|
| `rowx` | `query`, `query_unchecked`, `QueryResult`, `RowStream`, `Executor`, errors |
| `rowx.postgres` | `connect`, `ConnectOptions`, `Connection`, `Pool`, `Transaction`, `RESET_QUERY`, and the value types `Range`, `Interval`, `Money`, `Cube`, `Point`, `Line`, `LineSegment`, `Box`, `Path`, `Polygon`, `Circle` (added by the type-parity features, [Type mapping](type-mapping.md)), plus `Migrator`, `Migration`, `MigrationStatus` and `MigrationResult` ([Migrations](migrations.md)) |
| `rowx.errors` | adds `PoolTimeoutError(InterfaceError)`, `RowConstructError(DecodeError)` ([Runtime validation](#runtime-validation)), and `MigrateError(Error)` with its subclasses ([Migrations](migrations.md)); all are re-exported from `rowx` |
| `rowx.testing` | opt-in pytest plugin: `rowx_database_url`, `rowx_connection`, `rowx_pool` ([Test plugin](testing.md#test-plugin-rowxtesting)) |

- **Hard move, no aliases.** `rowx.connect` and `rowx.Connection` are removed, and `rowx.ping` is removed
  (use `Connection.ping`). `type_table()`, `_types.py` and `_vocab.py` move under `rowx.postgres`.
- **`ConnectOptions`** is a frozen, slotted, keyword-only dataclass, and `password` is `repr=False`.
  - **Redacted `__repr__`.** `ConnectOptions` has a custom `__repr__` that shows each `extra` key with the value
    `'<redacted>'` (`extra={'sslpassword': '<redacted>'}`), as the Rust `Debug` does, because `extra` can hold secrets
    such as `sslpassword`. `server_settings` stay visible. The userinfo of any DSN echoed in an exception or a checker
    diagnostic is redacted too, and the `repr()` of a wrapped cause never contains the password.
  - **`channel_binding`** is parsed from the DSN (`channel_binding=require`) and maps to tokio-postgres's
    `ChannelBinding`. `None` leaves the driver default (`prefer`). `require` with `sslmode=disable` is an
    `InterfaceError` at parse time, because nothing can be bound to a channel that has no TLS
    ([Connection and DSN](driver.md#connection-and-dsn-rowx-postgressrcdsnrs-tlsrs-connrs)). The recommendation for
    production is `sslmode=verify-full&channel_binding=require` ([Security](security.md#tls-and-authentication)).
  - **Validation.** Keys and values are validated when the options are converted
    ([Option validation](driver.md#option-validation)).
  - `ConnectOptions.from_dsn(dsn, **overrides)` parses with the Rust DSN parser, so there is one parser. `connect` and
    `Pool` accept `str | ConnectOptions`, and a pool parses a DSN only once.
  - **`server_settings`.** tokio-postgres puts only `user`, `database`, `application_name`, `client_encoding` and
    `options` in the startup packet, so `server_settings` travel inside `options` as `-c k=v`, with libpq escaping.
    Because they are startup settings, they survive `RESET ALL`, unlike a `SET` run in `init`.
  - **Nothing in the DSN is dropped.** `from_dsn` parses the `-c` pairs in `options` into `server_settings`, and
    overrides take precedence. Every other key the DSN parser keeps (keepalives, `target_session_attrs`, multiple
    hosts) goes in `extra` and is passed through. A DSN with `options=-c search_path=…` (the test `schema` fixture)
    **MUST** round-trip.
- **`Connection` methods.** `ping()` runs an empty simple query. `reset()` runs `RESET_QUERY`. `detach()` takes a pooled
  connection out of the pool permanently. On a pooled connection `close()` releases it to the pool. One operation per
  connection and `ConnectionBusyError` are unchanged.
- **`Pool`**: semantics are in [Pool](driver.md#pool).
- **`Transaction`**: semantics are in [Transactions](driver.md#transactions).
- **`Migrator`** is constructed synchronously and resolves and validates the files immediately, with no database IO, so
  a bad file fails at startup. Its semantics are in [Migrations](migrations.md).
  - The path is resolved against the cwd. A missing path, or one that isn't a directory, raises `MigrationSourceError`;
    an empty directory is valid.
  - `run`, `revert`, `skip` and `info` take a `Connection` or a `Pool`, claim one connection for the whole operation,
    and raise `InterfaceError` for a `Transaction` or a `Connection` with an open transaction.
  - `run`, `revert` and `skip` return one `MigrationResult` per migration that the call applied, reverted or skipped,
    in the order it happened. `elapsed` is `None` for a dry run or a skip. With `dry_run=True` nothing is executed
    and the results are those the call would produce. `info` returns one `MigrationStatus` per version.
  - `target` is a version: `run(target=None)` applies everything. `revert(target=None)` reverts the latest applied
    version only, `revert(target=V)` every applied version above `V`, and `revert(all=True)` everything. `target` with
    `all`, an unknown target and a target above the latest applied version are `ValueError`s.
  - **`revert` replaces `undo`**, a hard rename with no alias, because 0.1.0 is untagged.
  - `table` and `create_schemas` follow Postgres identifier rules ([History table](migrations.md#history-table)), and
    anything invalid raises `ValueError`.
  - `Migration` has one entry per version: a reversible pair is a single `Migration` with `down_path` and
    `down_checksum`. `checksum` is the up file's, and the down checksum is never compared against the database.
  - `Migration`, `MigrationResult` and `MigrationStatus` are frozen, slotted, keyword-only dataclasses.
- **Pool operations never raise `ConnectionBusyError`**: waiting longer than `acquire_timeout` raises
  `PoolTimeoutError`, and a closed pool raises `InterfaceError`.

## Method semantics

| Method | Result | Behaviour |
|---|---|---|
| `one` | `T` | Returns the first row, and raises `RowNotFound` if there are none. Extra rows are not an error (sqlx's `fetch_one`). After the first row the Rust side drops the row stream; the server still sends the remaining rows and the connection task drains and discards them. DML side effects (`INSERT … RETURNING`) always run to completion. The docs recommend `LIMIT 1` for large results. |
| `optional` | `T \| None` | Returns the first row, or `None` when there are none. Extra rows are not an error, and they drain as for `one`. |
| `all` | `list[T]` | Returns every row. The docs recommend `fetch` for results without a known upper bound. |
| `fetch` | `RowStream[T]` | Streams the rows ([Streaming](#streaming-fetch)). |
| `execute` | `QueryResult` | `rows_affected` is the count from `CommandComplete`, or 0. It is allowed on `query[T]`: the row plan is not built and the rows are discarded. |

- Every method except `execute` needs a row type. On `query[None]` they raise
  `InterfaceError` ([Row-type grammar and runtime row plan](#row-type-grammar-and-runtime-row-plan)). `fetch` raises it
  from the call itself, not from the first `__anext__`.
- `QueryResult` has no command tag (`"INSERT 0 5"`), because tokio-postgres does not expose
  it.
- **`optional` on a nullable scalar.** For `query[int | None]`, `None` means either "no row"
  or a NULL value. This matches asyncpg's `fetchval` and SQLAlchemy's
  `scalar_one_or_none`. The docs say to use a tuple row type (`query[tuple[int | None]]`)
  when the difference matters. The checker does not report it.

The **shape check** (arity, and for a named row the column-to-field mapping) happens before execution, against the
prepared statement's columns ([Row-type grammar and runtime row plan](#row-type-grammar-and-runtime-row-plan)). A shape
mismatch is reported even when the result is empty.

## Streaming (`fetch`)

`fetch(conn)` returns a single-use `RowStream[T]`. It is an async iterator, and an async
context manager for callers that want release not to depend on garbage collection.

```python
async for row in q.fetch(conn):
    ...

async with q.fetch(conn) as rows:
    async for row in rows:
        ...
    print(rows.result)          # QueryResult once exhausted, else None
```

- **Claiming.** `fetch` does no IO and doesn't claim the connection. The first `__anext__`
  claims it synchronously and extracts the parameters. `ConnectionBusyError`, the shape
  `RowShapeError` (arity or mapping) and `EncodeError` are therefore raised from the first `__anext__`.
- **Busy while open.** From the first `__anext__` until the stream ends, the connection is
  Busy, and any other operation on it raises `ConnectionBusyError` (asyncpg semantics). The
  docs say this prominently: a loop body can't run queries on the connection it is
  streaming from.
- **The stream ends** when it is exhausted, raises, or is closed. Closing means `aclose()`,
  leaving `async with`, or garbage collection of an unexhausted stream. After it ends,
  `__anext__` raises `StopAsyncIteration`.
- **`batch_size`** (default 1024, `ValueError` below 1) caps how many rows cross into
  Python per trip to Rust.
  - Each trip hands over the rows already buffered, up to the cap, and waits only for the
    first. A slow query yields its first row without waiting for a full batch.
  - It does not change round trips. The statement runs with no row limit, as in sqlx,
    because a non-zero `Execute` limit disables parallel query. The server sends rows as
    fast as the client consumes them.
  - Memory is bounded by one batch in Rust and one in Python, plus socket buffers.
- **`result`** is `None` until the stream is exhausted, then
  `QueryResult(rows_affected=…)`. It stays `None` after an early exit or an error.
- **Early exit drains.** Closing an unexhausted stream drops the row stream, as `one`
  does, and as sqlx does (it reads to `ReadyForQuery` before the next operation).
  - The server finishes the statement, and the connection discards the remaining rows.
    No CancelRequest is sent, so DML side effects run to completion.
  - The connection is Idle before `aclose()` returns (or when the stream is collected), and
    the next operation queues behind the drain on the wire. After a `break`, a stream still
    referenced by a variable stays open until it is collected, which is why `async with`
    is recommended. The docs say to bound large reads with `LIMIT`, or to cancel the task, when
    stopping early must be fast.
- **Cancellation.** Cancelling the task while it awaits `__anext__` cancels the statement,
  exactly as for the other methods ([Cancellation](driver.md#cancellation-cancelrs)).
  - A cancelled stream is over, even if it is still referenced: the next `__anext__`
    raises `StopAsyncIteration`, `result` stays `None`, and the connection is usable as soon as
    the statement is cancelled (psycopg and asyncpg also treat a cancelled cursor as dead).
  - A cancel that lands in the loop body is an ordinary early exit, so the stream drains.
- **Errors partway through.** A NULL in a non-nullable column (`RowShapeError`), a
  `DecodeError`, a `RowConstructError` or a `DatabaseError` is raised from `__anext__`.
  - Rows before the failing row are yielded first, and rows already yielded stay yielded.
  - The stream ends, and the connection drains and returns to Idle, or to Broken on an IO
    error.
- **Schema-change retry** ([Execution path](driver.md#execution-path-execrs)) applies only before the first row is
  delivered. In practice the server reports `0A000` at Bind/Execute, before any `DataRow`.
- **Closing the connection.** `conn.close()` while a stream is open cancels and settles,
  as for any Busy operation. The next `__anext__` raises `InterfaceError`.
- **On a pool**, `fetch(pool)` acquires at the first `__anext__`, holds the connection until the stream ends or is
  closed, then releases it. `closed` is per lease. A stream created inside `async with pool.acquire()` and iterated
  after the block raises `InterfaceError`, because its lease was revoked
  ([Leases](driver.md#leases)). Early exit drains the remaining rows before the release reset runs.
- A stalled consumer keeps the server backend in the middle of the statement, holding its
  snapshot and locks. This is documented.

## Row-type grammar and runtime row plan

Both the runtime (`_rowplan.py`) and the checker (`check/resolve.py`) implement this
grammar. A shared conformance table (`src/rowx/__tests__/conformance/row_types.py`) runs every entry
through both implementations, so they can't drift apart.

```text
ROW   := NONE                                  # no-rows plan
       | tuple[ELEM, ELEM, ...]                # fixed arity ≥ 1
       | ELEM                                  # scalar plan (exactly one column)
       | NAMED                                 # named plan; the ROW level only
NONE  := None                                  # default T, or explicit query[None]: identical
NAMED := @dataclass class                      # stdlib; matched by column name
       | typing.NamedTuple class or call       # matched by position, and the names must match
       | typing.TypedDict / typing_extensions.TypedDict class or call   # matched by key
       | pydantic.BaseModel subclass           # pydantic >= 2.11; validated by the model
       | pydantic.dataclasses.dataclass class  # validated by pydantic
       | a generic class or generic alias of any of these, subscripted (Generics)
FIELD := ELEM                                  # after unwrapping Annotated, ReadOnly, Required, NotRequired
ELEM  := LEAF | LEAF | None | Optional[LEAF] | Union[LEAF, None]
       | Any | object                          # nullability-neutral
       | None                                  # only as a tuple element or a named field, matching a `void` column
LEAF  := int | float | str | bool | bytes
       | decimal.Decimal | datetime.date | datetime.time | datetime.datetime
       | datetime.timedelta | uuid.UUID
       | ipaddress.IPv4Interface | ipaddress.IPv6Interface   # inet: the pair is ONE leaf
       | ipaddress.IPv4Network | ipaddress.IPv6Network       # cidr: the pair is ONE leaf
       | rowx.postgres.Interval | Money | Cube | Point | Line | LineSegment | Box | Path | Polygon | Circle
       | rowx.postgres.Range[RLEAF]            # ranges; RLEAF is the range's subtype leaf
       | dict[str, str | None]                 # hstore only
       | list[ELEM]                            # arrays; nested lists = multi-dim
RLEAF := int | decimal.Decimal | datetime.date | datetime.datetime
```

- Wrappers are unwrapped first: `Annotated[X, …]` becomes `X`, and a `TypeAliasType` (`type X = …`) is followed
  through `__value__` recursively, up to a depth of 16. A generic alias is substituted first ([Generics](#generics)).
- **Leaves are detected before named types.** The `rowx.postgres` value classes (`Range`, `Money`, `Interval`, the
  geometric classes and `Cube`) are frozen dataclasses, and they stay leaves. This order holds in both `_rowplan` and
  `resolve.py`.
- **Rejected** forms raise `InterfaceError` at the first row-returning call at runtime, and are
  `invalid-row-type` in the checker:
  - string or `ForwardRef`, and a `TypeVar` that isn't bound by a substitution;
  - bare `tuple`, `tuple[()]` and `tuple[X, ...]`;
  - a union with more than one non-None member, except the two network pairs above, which are written
    `IPv4Interface | IPv6Interface` and `IPv4Network | IPv6Network` and resolve to one leaf each (any other mix, for
    example `IPv4Interface | IPv6Network`, is rejected). The resolver treats a union whose member set equals one of
    these two sets, in either order, as that leaf;
  - `Literal`, any `dict` other than `dict[str, str | None]`, `tuple[X, ...]` in a column position (the row-level
    bare-tuple rule above is unchanged), classes other than the listed `rowx.postgres`
    leaves and the named forms below, and anything else not derivable from the grammar;
  - the named-row rejections in [Rejected named forms](#rejected-named-forms).
- **Each new leaf needs four things**, and a feature PR is not done without them: a grammar line here, a `_rowplan`
  rule, a `check/resolve.py` rule, and rows in `src/rowx/__tests__/conformance/row_types.py`. `Range[T]` is checked
  against the column's subtype (`Range[int]` matches `int4range` and `int8range`, `Range[date]` matches `daterange`). A
  leaf that does not match the column's leaf is `type-mismatch`, as for any other leaf.
- **Each new named form needs the same four things:** a `NAMED` line here, a `_rowplan` rule, a `check/resolve.py`
  rule, and conformance rows. The expected-plan format carries `(form, matching, ((key, names, nullable, required),
  ...))` for a `named` plan. A construct that only one side can see (see
  [Checker-only rejections](#checker-only-rejections)) gets a row that records the two verdicts.
- **Nullable mask.** An element is nullable when it is `None`, a union with `None`, `Any`
  or `object`.

`T` is read **lazily** at the first row-returning call from `self.__orig_class__.__args__[0]`. Python sets this after
`__init__`, so it is not readable inside `__init__`. If the attribute is missing, `T` is `None`. The compiled `RowPlan`
is cached per `T` object. A tuple or scalar plan is `(kind, arity, nullable_mask)`. A `named` plan adds the field specs
and a `_core.RowShape` ([Driver](driver.md#named-rows)), built once with the plan.

### Named row types

A `NAMED` row type maps each result column to a field of the class, builds each row through the class's constructor,
and is checked in the same way as a tuple, field by field.

**Forms.**

| Form | Matching | Construction | Required fields |
|---|---|---|---|
| `@dataclass` (stdlib) | by name | keyword arguments (`keyword`) | a field with no `default` or `default_factory` |
| `typing.NamedTuple` | by position, and names must match | positional arguments (`positional`) | a field not in `_field_defaults` |
| `TypedDict` (`typing` or `typing_extensions`) | by key | the row is the dict (`dict`) | `__required_keys__` |
| pydantic `BaseModel` or pydantic dataclass | by name | `__pydantic_validator__.validate_python(row_dict)` for a model, `cls(**row_dict)` for a pydantic dataclass (`validator`) | `FieldInfo.is_required()` |

- **Dataclasses** may use `slots`, `frozen`, `kw_only`, `field(kw_only=…)` and `KW_ONLY`. The fields are the entries of
  `__dataclass_fields__` that are plain fields with `init=True`.
- **NamedTuple and TypedDict** are supported in class form and in functional form (`NamedTuple("X", [...])`,
  `TypedDict("X", {...})`). A `typing_extensions.TypedDict` is detected through `sys.modules`, so rowx doesn't import
  it.
- **TypedDict class keywords.** The known keywords are `total`, `closed` and `extra_items`. `closed` and `extra_items`
  add no fields and don't relax `unexpected-column`. Optionality follows `__required_keys__`: `Required[...]` overrides
  `total=False`, `NotRequired[...]` overrides `total=True`, and `total` applies to the declaring class only. The
  wrappers count inside `Annotated` and `ReadOnly`, in a quoted annotation, and under
  `from __future__ import annotations`. `typing` supports `closed` and `extra_items` on 3.15 (PEP 728), and
  `typing_extensions` on 3.14. Any other class keyword on a non-pydantic form is a
  [checker-only rejection](#checker-only-rejections).
- **Pydantic** is never imported by rowx. A pydantic class is detected through `sys.modules.get("pydantic")`, as
  `Range` is. A pydantic dataclass is checked before the stdlib dataclass rule, because `dataclasses.is_dataclass` is
  true for it.
- **Inheritance.** Fields come from the bases in the order the library itself uses. A field redeclared in a child
  replaces the base's field in place. A project base that is not a row type (a plain mixin) contributes no fields. A
  NamedTuple's fields are only those of the class that directly subclasses `NamedTuple` (`_fields`), and annotations a
  subclass adds are ignored. The checker follows the real C3 method resolution order over project classes, so an
  inconsistent MRO or an inheritance cycle is `unresolved-row-type`; there is no inheritance depth cap. A private name
  `__x` is keyed by its mangled name, as the class mangles it.

**Which members are fields.** A field is an annotated member that the constructor accepts. These are **not** fields:
`ClassVar`, `KW_ONLY`, `dataclasses.field(init=False)`, pydantic private attributes (`_x`, `PrivateAttr`),
`computed_field`, and `Field(init=False)` on a pydantic dataclass (on a `BaseModel` it is an ordinary field). A field's
annotation is exactly an `ELEM`, after `Annotated`, `ReadOnly`, `Required` and `NotRequired` are unwrapped. A field
typed `None` matches a `void` column, as a tuple element does. A field is **optional** (the constructor fills it, or a
TypedDict lacks the key) when it has a dataclass `default` or `default_factory`, a pydantic default, `NotRequired`, or
`total=False`.

**Column names.** The name a column is matched under is the server's name after the override suffix is stripped
([Nullability overrides](#nullability-overrides-sqlx-convention)).

- One function in `rowx-core` (`row_shape::column_name`) strips a single trailing `!` or `?`, and exempts `?column?`.
  `describe.rs` and the mapping both call it. `id!!` becomes `id!`.
- Matching is case-sensitive.
- The checker passes the **raw** names to the same function through `_core.postgres.map_columns`, so nothing is
  stripped twice, and there is no Python port.

**Matching by name** (dataclass, TypedDict, pydantic). Column order is irrelevant. The mapping is a bijection, where
defaults relax one side:

- every column needs a field, or it is `unexpected-column`. This holds for a pydantic model with `extra="allow"` and a
  TypedDict with `extra_items` (`typing_extensions` only on 3.14);
- every field without a default needs a column, or it is `missing-column`;
- two columns of a by-name row with the same stripped name (`?column?` included), or, for pydantic, two accepted names
  of one field, are `duplicate-column`. An example of the latter is `id` and `user_id` for
  `id: int = Field(alias="user_id")` under `populate_by_name`, where pydantic would silently keep one. A name that
  appears on two or more columns is `duplicate-column` whether or not a field has that name (`field` is `None` when
  none does, as for `?column?`). Columns reported in a `duplicate-column` are not also reported as
  `unexpected-column`;
- one column that matches the names of two or more fields is `ambiguous-column`: equal aliases, or an alias equal to
  another field's name, with or without `populate_by_name`. Only pydantic can produce it. For example,
  `x: int = Field(alias="y")` next to `y: int` makes pydantic fill both from one `y`.

**Matching by position** (NamedTuple). Field *i* faces column *i*.

- The column count must be between the number of required fields and the total, or it is `arity-mismatch`. Trailing
  fields with defaults may have no column.
- A column whose stripped name differs from its field's name is `column-name-mismatch`, reported for each position.
  Names are errors, not hints, so `SELECT name, id` against `(id, name)` fails twice.
- When the arity is wrong, no name rules run, as for a tuple.
- Positional matching has no `missing-column` or `unexpected-column`.

**All errors are collected.** The mapping reports every violation, not the first. The runtime `RowShapeError` message
names the row type and lists them. The checker emits one diagnostic for each.

- **Order.** Column-indexed issues come first, by column index, then `missing-column` by field declaration order. A
  `duplicate-column` sorts by its second (reported) column index, which is also its JSON `column_index`; an
  `ambiguous-column` sorts by its column. The runtime message uses the same order.
- **Message formats.** The checker's diagnostic messages and the runtime issue messages are the same text, in the
  `column N "name"` style (columns are 1-based). `type_name` is `cls.__name__`, not the qualname, and for a substituted
  generic it renders the arguments (`Page[int]`).

  | Issue | Message |
  |---|---|
  | `unexpected-column` | ``column 3 "age" matches no field of `User` `` |
  | `missing-column` | ``field "name" of `User` has no column`` |
  | `duplicate-column` | ``columns 1 and 3 are both named "id"``; for `?column?`, ``columns 1 and 3 are both unnamed (`?column?`)``; pydantic two names, ``columns 1 "id" and 2 "user_id" both map to field "id" of `M` `` |
  | `ambiguous-column` | ``column 2 "y" matches fields "x" and "y" of `M` `` |
  | `column-name-mismatch` | ``column 2 "login" faces field "email" of `UserRow` (position 2)`` |
  | NamedTuple `arity-mismatch` | ``the query returns 4 columns but `UserRow` takes between 2 and 3`` (``takes exactly N`` when min equals max) |
  | `type-mismatch` | ``column 2 "age" is `int` but field "age" of `User` is `str` `` |
  | `nullability-mismatch` | ``column 3 "email" may be NULL but field "email" of `User` is `str` `` |

  `RowShapeError` reads ``{type_name}: {msg}; {msg}``, one message per issue. A tuple or scalar arity mismatch keeps its
  existing wording.

**Pydantic.** Rows are validated. For a `BaseModel`, Rust calls `__pydantic_validator__.validate_python(row_dict)`; for
a pydantic dataclass it calls the class, `cls(**row_dict)`, which runs the same validator through `__init__`. The dict
is keyed by column name and includes pass-through columns, so validators, constraints, aliases and a custom `__init__`
behave as in `Model.model_validate`.

- **Why a dataclass is called.** `validate_python(dict)` on a pydantic dataclass's validator raises
  `dataclass_exact_type` under `ConfigDict(strict=True)` (checked on pydantic 2.14.2), while `cls(**row_dict)` works
  under `strict=True` with aliases, `AliasChoices` and strict types intact. A failure of either call is a
  `ValidationError`, wrapped in `RowConstructError` the same way.
- An incomplete model (`__pydantic_complete__` is false) gets `model_rebuild(raise_errors=False)` (for a pydantic
  dataclass, `pydantic.dataclasses.rebuild_dataclass(cls, raise_errors=False)`) while the plan is built, and
  `__pydantic_complete__` is tested afterwards. If it is still incomplete, that is an `InterfaceError` naming the class
  (and the missing name where pydantic reports it). The plan caches `__pydantic_validator__`, so a later
  `model_rebuild(force=True)` isn't seen by a plan that is already built.
- **Column names** of a field: its `str` `validation_alias`, else its `str` `alias`, else its name. `validate_by_name`
  (or `populate_by_name`) adds the field name, and `validate_by_alias=False` keeps only the name. Config is read with
  `.get`, so 2.11's keys and the older `populate_by_name` both work. Both sides mirror pydantic's
  `_build_effective_config`: an explicit `validate_by_name` wins; else `populate_by_name` forces `validate_by_alias` on
  and sets `validate_by_name` to its own value; else `validate_by_alias=False` implies `validate_by_name`. The legacy
  `class Config: allow_population_by_field_name` is ignored, because pydantic 2.14 ignores it.
- **Field metadata** (`Field(...)`, aliases, defaults) is read through plain and `TypeAlias` aliases. Inside a PEP 695
  `type` alias it is ignored, as pydantic ignores it. On a model only, `= ...` clears a default that `Annotated`
  metadata gave. A model's plain mixins contribute their annotations and their before/wrap validators.
- **Class keywords are config.** `class M(BaseModel, populate_by_name=True)` is pydantic config, not an unknown class
  keyword: literal class keywords on a model merge into its config, and the child wins. A non-literal one makes the
  names unknown, in the checker. The merge order is pydantic's: the direct bases left to right (the last wins), then
  the class's own `model_config` or `class Config` (the last assignment wins), then the class keywords. A pydantic
  dataclass's config does not merge: it is the decorator's `config=`, else the nearest base's.
- **Pydantic dataclass config** is the decorator's literal `config=ConfigDict(...)` in the checker (a non-literal value
  makes the names unknown), and `__pydantic_config__` at runtime. `pydantic.dataclasses.dataclass` accepts only
  `init=False` (it means "keep pydantic's own `__init__`", and `init=True` raises), so a literal `init=False` on its
  decorator is **accepted** on both sides. Only an `InitVar` field is rejected (the runtime checks
  `__dataclass_fields__`); `Field(init=False)` on a field just makes it a non-field.
- **Unknown names.** This is a deliberate exception to "uncheckable is an error", for parity with sqlty. It has two
  scopes:
  - An `AliasChoices`, an `AliasPath` or a non-literal alias makes **that field's** names unknown.
  - A `model_validator(mode="before" | "wrap")`, or one with a non-literal `mode`, anywhere in the MRO (a plain mixin
    and an assigned `model_validator(...)(...)` included) makes **every** field's names unknown. So does an
    `alias_generator`, or a `model_config` the checker can't read, **in the checker only**: at runtime the generated
    aliases are plain strings, and are checked. Config or a validator inside a conditional block makes every field
    unknown too, because the checker can't follow it.
  - If any field is unknown, the four by-name rules (`missing-column`, `unexpected-column`, `duplicate-column`,
    `ambiguous-column`) are skipped for the whole model. The checker adds `note: names not checked: <reason>` to the
    diagnostics it emits on that call, and to nothing else: unknown names stay errors-only, so an otherwise clean model
    prints nothing, even when all its names are unknown.
    Type and nullability checks still run for columns that map to a field with known names.
  - At runtime, every column goes into the dict, and the NULL check applies only to columns that match a known name.
    A pass-through column decodes with the default decoder (no `Interval` flag), and two columns with one name collapse
    to the last one in the dict ([known limits](../known-limits.md)).
- **The checker stays exact** ([decision 17](decision-log.md)). Lax-mode coercions, such as a `numeric` column into an
  `int` field, are reported as `type-mismatch`. There is no pydantic type table.
- **Pydantic-only types** (`EmailStr`, `AnyUrl`, `SecretStr`) are real classes and not `ELEM`s, so they are
  `invalid-row-type` on both sides. Annotate the column's Python type and validate with
  `Annotated[str, AfterValidator(...)]`. The constraint aliases that are `Annotated[<leaf>, ...]` (`PositiveInt`,
  `constr()`) are different: the runtime strips the `Annotated` metadata and accepts them, and the checker rejects them
  ([Checker-only rejections](#checker-only-rejections)).
- The `rowx.postgres` value classes work as pydantic field types without `arbitrary_types_allowed`, and the decoded
  instance passes through validation unchanged, even under `strict=True`.

**Annotations.**

- **At runtime**, annotations come from `typing.get_type_hints(cls, include_extras=True)` (or pydantic's resolved field
  info). An annotation that doesn't resolve raises `InterfaceError` naming the field and the missing name.
- **In the checker**, annotations come from the class body's AST. A string annotation in a class body, and every
  annotation under `from __future__ import annotations`, is parsed as an expression. A string as `T` itself stays
  `invalid-row-type`.
- A name that resolves only through a `TYPE_CHECKING` import is `invalid-row-type` inside a named-type field, with the
  help "import `datetime` at runtime", because `get_type_hints` would fail on it (ruff's TC003 produces exactly this).
  Only the selected fields count: a `ClassVar`, an `init=False` field and a NamedTuple-subclass extra are not resolved.
  A **row-level** alias whose value names a `TYPE_CHECKING`-only import is `invalid-row-type` too, with the same help,
  because the runtime always fails to build that plan (an `InterfaceError`). In a class body the checker skips the
  `if TYPE_CHECKING:` body and follows its `else:`, as the runtime does.

#### Generics

Generic classes (`class Page[T]`, `Generic[T]`) and generic aliases (`type Rows[T] = tuple[int, T]`) are
supported on both sides, once subscripted.

- Substitution is recursive, through a mapping from each `__type_params__` (or `__parameters__`) entry to its argument.
  It is never `__value__[args]`, because `type Swap[A, B] = tuple[B, A]` reports its parameters in order of use.
- The argument count is checked explicitly, because Python doesn't check it. Inherited parameters are rebound by
  walking `__orig_bases__`, so `class Child[U](Base[U])` binds `Base`'s own `T`.
- Every parameter must be bound, by an argument or a PEP 696 default. An unbound one, a `TypeVarTuple`, a `ParamSpec`,
  a wrong argument count, or a substitution deeper than the alias depth limit is `invalid-row-type`.
- **Depth limit.** One hop is one alias or class substitution along a chain (nesting depth, not breadth). The limit is
  16, shared with `MAX_ALIAS_DEPTH`, and the conformance rows sit at 16 (accepted) and 17 (rejected).
- `TypeVar` bounds and constraints are not enforced; the leaf check does the work. A generic alias of a generic class
  (`type B[T] = Box[T]`) and a bare generic class whose parameters all have PEP 696 defaults are supported.
- **Parameter order.** A legacy generic's parameters are collected depth first, by first appearance over
  `__orig_bases__`, and an explicit `Generic[...]` overrides that order. `Annotated` metadata and string forward refs
  are not collected. A `TypeVar` imported from another module is matched by identity, and its default is read in the
  defining module. A PEP 696 default may refer to an earlier parameter, and is substituted through the mapping.
- **Old-style aliases.** `Row = tuple[T, str]` and `X: TypeAlias = ...` are generic aliases like a `type` alias. A bare
  plain generic alias is `invalid-row-type` even when its parameters have defaults, because Python gives a plain alias
  none; `Pair[()]` takes the defaults.
- **Hops.** A `type` alias, a class substitution and a re-export each count; a plain assignment does not. A class at the
  end of an alias chain is one hop, and a `Generic[...]` or `Protocol[...]` base is none. Running out of hops on a
  generic chain is `invalid-row-type`; any other chain stays `unresolved-row-type`. A generic class chain is capped at
  16 subscripted bases, and pydantic models are exempt.
- `type_name` renders the arguments (`Box[int]`), in one canonical text on both sides: `X | None` for an Optional or a
  Union with `None`, unions flattened and deduplicated, `Annotated` metadata dropped, a plain alias as its target, a
  `type` alias by name, a `TypeVar` by name, and `...` for an `Ellipsis`. A bare generic with defaults shows them.
- **Known limit.** A pydantic dataclass `Box[int]` is built by calling the origin class, so pydantic validates the field
  as the unparametrized `T`: values pass through, and a `TypeVar` bound is enforced by pydantic there but not by the
  checker ([known limits](../known-limits.md)).
- The constructor is the origin class, not the subscripted alias, so `__orig_class__` isn't set on every row. A pydantic
  `Page[int]` is already a concrete subclass with resolved fields, and is used as it is.

#### Rejected named forms

Rejected on **both sides** (`invalid-row-type` in the checker, `InterfaceError` at the first row-returning call):

- `Protocol`, `collections.namedtuple` (untyped), attrs, msgspec, `RootModel` and pydantic v1 (`pydantic.v1`);
- a stdlib dataclass with `init=False`, and a dataclass `InitVar` field (also on a pydantic dataclass, whose decorator
  accepts `init=False`);
- a named row type with no fields (`invalid-row-type`, "has no fields");
- a nullable named row (`User | None`), with the help "use .optional()";
- a named type anywhere below the row level: in a tuple, a list, a union, or as a field type;
- an unbound or unsupported generic parameter ([Generics](#generics)).

#### Checker-only rejections

The checker rejects these as `invalid-row-type` because it can't see what they do to the constructor. The runtime
accepts them, and any failure surfaces as `RowConstructError`:

- a class decorator other than the dataclass decorators and `typing.final`;
- a `metaclass=`, or another class keyword the resolver doesn't know (pydantic config keywords are known);
- a hand-written `__init__` on a stdlib dataclass;
- a pydantic constraint alias that is `Annotated[<leaf>, ...]` (`PositiveInt`, `constr()` and the like). The runtime
  strips the `Annotated` metadata, and the checker sees only a pydantic name it can't look through (there is no pydantic
  type table);
- `pydantic.Json[...]`;
- mixed forms: a model with a stdlib dataclass base, a stdlib dataclass over a pydantic dataclass, and a pydantic
  dataclass over a stdlib one;
- an undecorated subclass of a pydantic dataclass;
- an external stdlib base (`abc.ABC`, `Exception`), because inheritance is project-only;
- a field declared inside a conditional block, which the runtime runs and the checker can't follow (the
  `if TYPE_CHECKING:` body is skipped and its `else:` is live);
- a plain alias over a `TypeVarTuple`, which the runtime builds.

The runtime can't see a decorator or a class keyword once the class exists. It can see a hand-written `__init__`, but it
doesn't reject one, because a failing constructor already has a runtime error. A pydantic model **may** define
`__init__`, because the validator calls it. Conformance rows record these as runtime-ok and checker-invalid.

### Runtime validation

- **Arity.** Before executing, compare `len(stmt.columns())` with the plan's arity. A
  scalar plan needs exactly 1 column. A mismatch raises `RowShapeError`. Columns of type
  `void` count towards arity.
- **Name mapping (named rows).** In the same step, the prepared statement's columns are mapped to the fields. A
  mismatch raises `RowShapeError` before execution, even for an empty result, and names the row type and each error. A
  cached statement whose mapping fails is re-prepared once, as for an arity mismatch, before the error is raised.
- **NULL check.** A NULL in a position the mask marks non-nullable raises `RowShapeError`
  naming the column index and name. The Rust decoder performs this check. For a named row the mask is per mapped
  field; a column that maps to no known field (unknown names) counts as nullable. The named message names the field and
  the column: ``{type_name}: NULL in column 2 "email" for non-nullable field "email" of `User` (row {k})``, where `k` is
  0-based, like `RowConstructError.row_index`. The tuple message is
  unchanged. In `all` and `one` the NULL checks run while the rows are collected, before any constructor runs. In a
  stream, earlier rows are constructed and yielded first.
- **Construction errors.** An exception from the constructor or validator (a pydantic `ValidationError`, a raising
  `__post_init__`, a `TypeError` from a hand-written `__init__`) raises `RowConstructError(DecodeError)`. It carries
  `type_name` and `row_index`, the 0-based position in the returned rows (counted across stream batches, and always 0
  for `one` and `optional`). The message is ``{type_name} row {row_index}: {cause!r}``. The original exception is its
  `__cause__`. Stream semantics are those of `DecodeError`: rows before the failing one are yielded first. It wraps
  `Exception` only: `CancelledError`, `KeyboardInterrupt` and `SystemExit` pass through.
- **Types are not checked at runtime**, except by pydantic. `query_unchecked[T]` gets the same runtime checks.
- **No-rows plan.** `one`, `optional`, `all` and `fetch` raise
  `InterfaceError("query without a row type; use .execute() or add query[T]")`.
- Element types and nullability inside `list[...]` are **not** checked at runtime. A NULL
  array element decodes to `None`.

## t-string compilation (`rowx/_template.py`)

Compilation happens **eagerly in `__init__`**, because it doesn't need `T`. The result is
stored on the instance. There are two layers.

```python
@dataclass(frozen=True, slots=True)
class Compiled:
    sql: str
    placeholders: tuple[tuple[int, int], ...]   # (start, end) of each "$n" in sql
    segments: tuple[tuple[int, int], ...]       # (start, end) in sql of each static string i
                                                # (len == n_interpolations + 1)

def compile_parts(strings: Sequence[str]) -> Compiled: ...      # shared with the checker
def compile_template(t: Template, *, dynamic: bool) -> tuple[Compiled, list[object]]: ...
```

**`compile_parts`** emits static strings verbatim and the n-th interpolation as `$n`,
counting from 1. The same expression used twice becomes two parameters.

**Adjacency rule.** A single space is inserted before `$n` when the previous SQL character
is an identifier character (`[A-Za-z0-9_$]` or non-ASCII). A space is inserted after `$n`
when the next character is one. Without this, `x{y}` becomes the identifier `x$1`, and
`{a}{b}` becomes `$1$2`. The spaces are reflected in `segments` and `placeholders`.

**`compile_template` in `query`** (`dynamic=False`) raises `TypeError` for:

- a non-`Template` argument;
- any interpolation whose value is a `Template`;
- any `conversion`, including `{x=}`, which arrives as conversion `r`;
- any `format_spec`.

**`compile_template` in `query_unchecked`** (`dynamic=True`):

- A nested `Template` is flattened recursively, and its parameters are renumbered.
- `{x:ident}` emits a quoted identifier, with any `"` doubled ([`:ident`](#ident-identifiers)).
- The [compile limits](#compile-limits) apply to the flattened result.
- Any other conversion or format spec raises `TypeError`. There is **no** raw-SQL spec.

**Writing guidance** (in the docs and in checker help text):

- **IN-lists.** Write `= ANY({seq})`, where `seq` is a list bound as an array.
- **Literal braces** (JSON or array literals in SQL) are written `{{`/`}}`.
- **Don't interpolate inside a string literal or comment.** `LIKE '%{q}%'` puts the text
  `$1` inside the literal. Bind the whole pattern instead: `LIKE {pat}`. The checker reports
  this as `param-count-mismatch`, and at runtime the parameter count disagrees and raises
  `InterfaceError`.
- **Cast parameters whose type the server can't infer.** For example `{x}::int IS NULL`.
  Without a cast, Postgres fails with 42P18 or 42P08.
- Don't write `$n` by hand. It causes the same count mismatch.

### `:ident` identifiers

`x` is a non-empty `str`, or a non-empty `tuple` of them joined with `.`. `{x:ident}` stays `query_unchecked`-only.
The values, and the **types** of the values, are untrusted
([Threat model](security.md#threat-model)), so the compiler never calls a method on the value that the caller could
override.

- **Exact normalisation.** Any `str` instance is accepted, so a `StrEnum` allowlist works. The first step for each part
  is `raw = str.encode(name)`, which is strict UTF-8, so a lone surrogate raises `TypeError`. Every later check and the
  quoting (`raw.replace(b'"', b'""')`) run on `raw`, and nothing is called on `name` again. A `str` subclass that
  overrides `__contains__`, `__len__`, `encode`, `replace`, `__iter__`, `__add__` or `__radd__` therefore has no effect.
- **Tuples** are iterated with `tuple.__iter__(value)`, so a `tuple` subclass can't override iteration, and each part
  is normalised as above.
- **Rejected with `TypeError`:**
  - a non-`str` value or part, an empty name, an empty tuple or an empty part;
  - a part longer than 63 UTF-8 bytes (Postgres would truncate it silently);
  - a part with a C0 or C1 control character (U+0000 to U+001F, and U+007F to U+009F), NUL included.
- **A runtime `ident` spec is not a separate path.** In `{x:{spec}}`, a `spec` that evaluates to `"ident"` turns a bound
  value into a quoted identifier. At runtime that can't be told apart from a literal `:ident`, so it takes the same
  path and gets the same checks. The checker is what flags it: it inspects the interpolations of `query_unchecked`
  calls, and a non-literal format spec there is `dynamic-query`
  ([Discovery](checker.md#discovery-checkdiscoverpy)).
- **Identifiers are not restricted to enum members.** Quoting makes any allowed name safe as an identifier, but not
  harmless as a choice: `ORDER BY {col:ident}` with an attacker-chosen column is a sort-order oracle. The guide shows
  allowlisting.

### Compile limits

`compile_template` raises `TypeError` before any IO, like every other template misuse. The connection stays usable.

- The compiled SQL contains a NUL character: `TypeError("SQL text must not contain a NUL character")`. This applies in
  `query` too, because static text can hold one.
- More than 65535 parameters after nested templates are flattened:
  `TypeError("a query takes at most 65535 parameters, got N")`, where `N` is the count. The protocol carries the
  parameter count in 16 bits.

## Nullability overrides (SQLx convention)

A result column whose name ends in `!` is forced NOT NULL, and one ending in `?` is forced
nullable. You write it with a quoted alias: `count(*) AS "n!"`.

- The checker strips the suffix ([Overrides](nullability.md#overrides)).
- For tuple and scalar rows the runtime ignores names, because plans are positional. A NULL in a `!` column still
  raises `RowShapeError`.
- For a named row the runtime strips one trailing `!` or `?` through `row_shape::column_name` before it matches a name
  (`"id!"` maps to the field `id`), and `?column?` is exempt. The suffix still doesn't change the NULL check: a NULL in
  a `!` column raises `RowShapeError`.
- Postgres truncates identifiers at 63 bytes, which would silently drop the suffix. The
  checker reports a described column name of exactly 63 bytes whose quoted alias in the
  source SQL ends in `!`/`?` as `invalid-override`.
- Type overrides (`"col: Type"`) are deferred.

## Errors (`rowx.errors`, re-exported from `rowx`)

```text
rowx.Error(Exception)
├── InterfaceError          # misuse / client-side: closed or broken connection, param-count
│   │                       # mismatch, unsupported or absent row type at fetch, DSN/network/TLS failure,
│   │                       # closed pool, use after fork, use of a revoked lease or ended Transaction
│   ├── ConnectionBusyError # concurrent operation on one connection (Connection state machine)
│   └── PoolTimeoutError    # acquire waited longer than acquire_timeout
├── DatabaseError           # server ErrorResponse: .sqlstate .message .detail .hint .position
│                           # (incl. auth failures 28P01, unknown database 3D000, query canceled 57014)
├── RowNotFound             # one with zero rows
├── RowShapeError           # arity or column-to-field mismatch / NULL in non-nullable position
├── DecodeError             # value can't be represented as the Python type (infinity, interval months, …)
│   └── RowConstructError   # .type_name .row_index: a named row's constructor or validator raised; __cause__ is the original
├── EncodeError             # Python value not accepted for the param's type (Type mapping); names "$n" and both types
└── MigrateError            # Migrator failures; typed attributes per subclass
    ├── MigrationSourceError    # .path .version: bad filename, duplicate version, unpaired up/down, non-UTF-8 file,
    │   │                       # missing or non-directory path
    │   └── IrreversibleMigrationError  # .version: revert reaches a simple migration
    ├── MigrationChecksumError  # .version .local_checksum .applied_checksum: an applied migration's file changed
    ├── MigrationMissingError   # .versions (tuple): applied migrations with no local file
    ├── MigrationOrderError     # .version .latest: a pending version is below the highest applied one
    ├── DirtyMigrationError     # .version: a history row with success = false
    └── MigrationFailedError    # .version .path .dirty .applied .error: the file failed; __cause__ is .error
```

- **Construction-time misuse** raises a plain `TypeError` from `__init__`. Examples: a
  non-Template argument, or a dynamic interpolation in `query`.
- **Fetch-time row-type problems** raise `InterfaceError`.
- **`RowConstructError`** keeps keyword constructors and pickles as `DatabaseError` does; pickling keeps `type_name`
  and `row_index`, and `__cause__` isn't pickled. It is re-exported from `rowx`.
- `DatabaseError.sqlstate` is a 5-character `str`, and `.position` is an `int | None`
  holding the 1-based character position.
- **Migration errors** ([Migrations](migrations.md)) are all `MigrateError`s, defined in `rowx.errors` and re-exported
  from `rowx` (each is in `__all__`). Each keeps keyword constructors and pickles as `DatabaseError` does.
  - A server failure while a migration file runs is `MigrationFailedError(version, path, dirty, applied, error)`.
    `error` is the `DatabaseError` (also `__cause__`), `dirty` says whether a history row was left `success = false`,
    and `applied` is the `tuple[MigrationResult, ...]` completed before the failure. A failure in the lock or
    history-table SQL is a plain `DatabaseError`.
  - A revert that reaches a simple migration raises `IrreversibleMigrationError` before anything is reverted.
  - Misuse (a `Transaction` argument) is `InterfaceError`; an invalid `table`, `create_schemas` entry or target is
    `ValueError`.
- Sqlstate-specific subclasses are deferred.
- No error message or `repr` may include the DSN password.
