# User-facing API

## Example

```python
import asyncio
from decimal import Decimal

from rowx import query
from rowx.postgres import ConnectOptions, Pool, connect

type UserRow = tuple[int, str, str | None]          # id, name, email

async def main() -> None:
    async with await connect("postgresql://app@localhost/app") as conn:
        uid = 42
        user = await query[UserRow](
            t"SELECT id, name, email FROM users WHERE id = {uid}"
        ).one(conn)                                  # -> UserRow; raises RowNotFound

        maybe = await query[UserRow](
            t"SELECT id, name, email FROM users WHERE id = {uid}"
        ).optional(conn)                             # -> UserRow | None

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

RESET_QUERY: Final[str]      # "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;"

@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectOptions:
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    database: str | None = None
    sslmode: str | None = None
    sslrootcert: str | None = None
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
| `rowx.errors` | adds `PoolTimeoutError(InterfaceError)`, and `MigrateError(Error)` with its subclasses ([Migrations](migrations.md)); all are re-exported from `rowx` |
| `rowx.testing` | opt-in pytest plugin: `rowx_database_url`, `rowx_connection`, `rowx_pool` ([Test plugin](testing.md#test-plugin-rowxtesting)) |

- **Hard move, no aliases.** `rowx.connect` and `rowx.Connection` are removed, and `rowx.ping` is removed
  (use `Connection.ping`). `type_table()`, `_types.py` and `_vocab.py` move under `rowx.postgres`.
- **`ConnectOptions`** is a frozen, slotted, keyword-only dataclass, and `password` is `repr=False`.
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

The **arity check** happens before execution, against the prepared statement's columns
([Row-type grammar and runtime row plan](#row-type-grammar-and-runtime-row-plan)). A shape mismatch is reported even
when the result is empty.

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
  claims it synchronously and extracts the parameters. `ConnectionBusyError`, the arity
  `RowShapeError` and `EncodeError` are therefore raised from the first `__anext__`.
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
  `DecodeError` or a `DatabaseError` is raised from `__anext__`.
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
grammar. A shared conformance table (`tests/conformance/row_types.py`) runs every entry
through both implementations, so they can't drift apart.

```text
ROW   := NONE                                  # no-rows plan
       | tuple[ELEM, ELEM, ...]                # fixed arity ≥ 1
       | ELEM                                  # scalar plan (exactly one column)
NONE  := None                                  # default T, or explicit query[None]: identical
ELEM  := LEAF | LEAF | None | Optional[LEAF] | Union[LEAF, None]
       | Any | object                          # nullability-neutral
       | None                                  # only as a tuple element, matching a `void` column
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

- Wrappers are unwrapped first: `Annotated[X, …]` becomes `X`, and a non-generic
  `TypeAliasType` (`type X = …`) is followed through `__value__` recursively, up to a depth
  of 16.
- **Rejected** forms raise `InterfaceError` at the first row-returning call at runtime, and are
  `invalid-row-type` in the checker:
  - string or `ForwardRef`, and `TypeVar`;
  - a generic alias (`type Row[X] = …`);
  - bare `tuple`, `tuple[()]` and `tuple[X, ...]`;
  - a union with more than one non-None member, except the two network pairs above, which are written
    `IPv4Interface | IPv6Interface` and `IPv4Network | IPv6Network` and resolve to one leaf each (any other mix, for
    example `IPv4Interface | IPv6Network`, is rejected). The resolver treats a union whose member set equals one of
    these two sets, in either order, as that leaf;
  - `Literal`, any `dict` other than `dict[str, str | None]`, `tuple[X, ...]` in a column position (the row-level
    bare-tuple rule above is unchanged), classes other than the listed `rowx.postgres`
    leaves, and anything else not derivable from the grammar.
- **Each new leaf needs four things**, and a feature PR is not done without them: a grammar line here, a `_rowplan`
  rule, a `check/resolve.py` rule, and rows in `tests/conformance/row_types.py`. `Range[T]` is checked against the
  column's subtype (`Range[int]` matches `int4range` and `int8range`, `Range[date]` matches `daterange`). A leaf that
  does not match the column's leaf is `type-mismatch`, as for any other leaf.
- **Nullable mask.** An element is nullable when it is `None`, a union with `None`, `Any`
  or `object`.

`T` is read **lazily** at the first row-returning call from `self.__orig_class__.__args__[0]`.
Python sets this after `__init__`, so it is not readable inside `__init__`. If the
attribute is missing, `T` is `None`. The compiled `RowPlan(kind, arity, nullable_mask)` is
cached per `T` object.

Runtime validation:

- **Arity.** Before executing, compare `len(stmt.columns())` with the plan's arity. A
  scalar plan needs exactly 1 column. A mismatch raises `RowShapeError`. Columns of type
  `void` count towards arity.
- **NULL check.** A NULL in a position the mask marks non-nullable raises `RowShapeError`
  naming the column index and name. The Rust decoder performs this check.
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
- `{x:ident}` emits a quoted identifier, with any `"` doubled.
  - `x` is a non-empty `str`, or a non-empty `tuple[str, ...]` joined with `.`.
  - A name over 63 UTF-8 bytes, an empty name, a NUL character, or a non-str raises
    `TypeError`. Postgres would otherwise truncate the name silently.
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

## Nullability overrides (SQLx convention)

A result column whose name ends in `!` is forced NOT NULL, and one ending in `?` is forced
nullable. You write it with a quoted alias: `count(*) AS "n!"`.

- The checker strips the suffix ([Overrides](nullability.md#overrides)).
- The runtime ignores names, because plans are positional. A NULL in a `!` column still
  raises `RowShapeError`.
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
├── RowShapeError           # arity mismatch / NULL in non-nullable position
├── DecodeError             # value can't be represented as the Python type (infinity, interval months, …)
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
