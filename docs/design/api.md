# User-facing API

## Example

```python
import asyncio
from decimal import Decimal

import rowx
from rowx import query

type UserRow = tuple[int, str, str | None]          # id, name, email

async def main() -> None:
    async with await rowx.connect("postgresql://app@localhost/app") as conn:
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

asyncio.run(main())
```

## Public surface (`rowx/__init__.py`)

```python
from dataclasses import dataclass
from string.templatelib import Template
from typing import Self

class _BaseQuery[T]:                       # private; holds all behaviour
    _dynamic: ClassVar[bool]
    def __init__(self, template: Template, /) -> None: ...      # compiles eagerly ([t-string compilation](#t-string-compilation-rowx_templatepy))
    async def one(self, conn: Connection, /) -> T: ...
    async def optional(self, conn: Connection, /) -> T | None: ...
    async def all(self, conn: Connection, /) -> list[T]: ...
    def fetch(self, conn: Connection, /, *, batch_size: int = 1024) -> RowStream[T]: ...
    async def execute(self, conn: Connection, /) -> QueryResult: ...

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

async def connect(dsn: str, /, *, connect_timeout: float | None = None,
                  statement_cache_size: int = 256) -> Connection: ...

class Connection:
    async def close(self) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...   # closes
    @property
    def closed(self) -> bool: ...
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
- `Connection` is the only executor. When the pool or transaction helper lands, the
  parameter type widens to a protocol, which is backward compatible for callers.

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
  - The connection is Idle at once, and the next operation queues behind the drain on the
    wire. The docs say to bound large reads with `LIMIT`, or to cancel the task, when
    stopping early must be fast.
- **Cancellation.** Cancelling the task while it awaits `__anext__` cancels the statement,
  exactly as for the other methods ([Cancellation](driver.md#cancellation-cancelrs)). A cancel
  that lands in the loop body is an ordinary early exit, so the stream drains.
- **Errors partway through.** A NULL in a non-nullable column (`RowShapeError`), a
  `DecodeError` or a `DatabaseError` is raised from `__anext__`.
  - Rows before the failing row are yielded first, and rows already yielded stay yielded.
  - The stream ends, and the connection drains and returns to Idle, or to Broken on an IO
    error.
- **Schema-change retry** ([Execution path](driver.md#execution-path-execrs)) applies only before the first row is
  delivered. In practice the server reports `0A000` at Bind/Execute, before any `DataRow`.
- **Closing the connection.** `conn.close()` while a stream is open cancels and settles,
  as for any Busy operation. The next `__anext__` raises `InterfaceError`.
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
       | list[ELEM]                            # arrays; nested lists = multi-dim
```

- Wrappers are unwrapped first: `Annotated[X, …]` becomes `X`, and a non-generic
  `TypeAliasType` (`type X = …`) is followed through `__value__` recursively, up to a depth
  of 16.
- **Rejected** forms raise `InterfaceError` at the first row-returning call at runtime, and are
  `invalid-row-type` in the checker:
  - string or `ForwardRef`, and `TypeVar`;
  - a generic alias (`type Row[X] = …`);
  - bare `tuple`, `tuple[()]` and `tuple[X, ...]`;
  - a union with more than one non-None member;
  - `Literal`, `dict`, classes, and anything else not derivable from the grammar.
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
│   │                       # mismatch, unsupported or absent row type at fetch, DSN/network/TLS failure
│   └── ConnectionBusyError # concurrent operation on one connection (Connection state machine)
├── DatabaseError           # server ErrorResponse: .sqlstate .message .detail .hint .position
│                           # (incl. auth failures 28P01, unknown database 3D000, query canceled 57014)
├── RowNotFound             # one with zero rows
├── RowShapeError           # arity mismatch / NULL in non-nullable position
├── DecodeError             # value can't be represented as the Python type (infinity, interval months, …)
└── EncodeError             # Python value not accepted for the param's type (Type mapping); names "$n" and both types
```

- **Construction-time misuse** raises a plain `TypeError` from `__init__`. Examples: a
  non-Template argument, or a dynamic interpolation in `query`.
- **Fetch-time row-type problems** raise `InterfaceError`.
- `DatabaseError.sqlstate` is a 5-character `str`, and `.position` is an `int | None`
  holding the 1-based character position.
- Sqlstate-specific subclasses are deferred.
- No error message or `repr` may include the DSN password.
