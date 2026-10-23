# rowx — Design Document (v1)

> Status: **Reviewed, ready for implementation** · Revision 2 · 2026-10-23
>
> rowx is typesafe raw SQL for Python 3.14+, with no ORM. You write queries as PEP 750
> t-strings with an explicit row type, `query[tuple[int, str]](t"...")`. A Rust driver
> built on tokio-postgres runs them. A static checker, `rowx check`, validates every query
> against a **live** Postgres 18 database, the same way SQLx's macros do, by preparing and
> describing it.

This document is the **contract**. The implementation work is broken into tasks in
[`docs/tasks.md`](tasks.md).

- Where the spec says **MUST**, the behaviour is required and has a test.
- "Decision" callouts record a choice and the reason for it.
- An implementing agent who finds the spec ambiguous MUST NOT guess silently. They add an
  entry to `docs/decisions.md` in the same PR (§16).

Revision 2 folds in three design reviews: driver and Postgres, Python API and checker, and
plan and testability. The review decisions are logged in §15.2.

---

## 1. Goals and non-goals

### 1.1 Goals (v1)

1. **Runtime API.** An async Python API runs t-string SQL against Postgres and decodes rows
   into the declared tuple or scalar type `T`.
2. **Owned driver.** A Rust core (PyO3, abi3-py314) owns the connection. It is built on
   `tokio-postgres`.
3. **Static checker.** `rowx check` finds every `query[T](t"...")` call. For each call it:
   - prepares the SQL on a live Postgres 18 database. A query the server rejects is an
     error.
   - compares the described result columns with `T`: count, Python type and nullability
     must match **exactly**.
   - compares interpolated values whose Python type is statically evident with the
     parameter types the server inferred (as sqlty does).
4. **Nullability** follows SQLx's model: catalog NOT NULL, plus a plan walk for outer joins.
   Unknown columns count as nullable, and `"col!"`/`"col?"` overrides apply. rowx uses its
   own corrected implementation.
5. **One source of truth** for the type mapping. The decoder, the encoder and the checker
   all read the same table (§6), and one row-type grammar (§3.4) serves both the runtime
   and the checker.

### 1.2 Non-goals for v1

These are deferred, not rejected; §14 lists them in priority order.

- Backends other than PostgreSQL. Only Postgres **18** is supported and tested.
- A sync API, and the free-threaded build (`3.14t`); abi3 wheels don't install there.
- Connection pooling, a transaction helper, pipelining, LISTEN/NOTIFY and COPY.
- An offline cache. The checker always needs a live database.
- `--fix` and rewriting, warnings, severities and ignore comments. Every diagnostic is an
  error.
- Named row types (dataclass, TypedDict, NamedTuple, Pydantic, Protocol). Only tuples and
  scalars are supported.
- `fetch_optional` and streaming `fetch`.
- Migrations.
- A client-side SQL parser (§11).
- Type-checker plugins.
- libpq environment variables (`PG*`) and `.pgpass`.

---

## 2. Background and prior art

| Tool | Approach | What rowx takes from it |
|---|---|---|
| **SQLx** (Rust) | Proc macros Parse/Describe on a live DB, with `pg_attribute` + `EXPLAIN` nullability and an offline `.sqlx` cache. | It validates against a live DB. rowx takes its nullability model, the `!`/`?` overrides, its reasons for not parsing SQL (FAQ), and its method names. |
| **sqlty** (Py+Rust) | Offline sqlparser-rs analyser over DDL, with `SQL[T]` annotations. | rowx takes the annotation as the contract, static argument types (`_params.py`), docs-as-tests and fidelity tests, and `query[T]` as a **class**. Its config ergonomics come from `sqlty/_config.py`. |
| **SafeQL** (TS) | ESLint check of tagged templates against a live DB. | It is the closest prior art, and it shows the approach works. |
| **psqlpy** | A PyO3 driver on tokio-postgres + deadpool. | It shows the stack works in production. rowx avoids its binary-only parameter bugs (§7.3). |
| **sql-tstring, t-sql** | Turn t-strings into parameterised SQL, with no checking. | The niche of checking t-string SQL against a live DB is open. |

The research reports, spike code and review reports are in the design session's scratchpad.
They are not part of the repo. The key evidence is reproduced in §8.4 and §11.

---

## 3. User-facing API

### 3.1 Example

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
        ).fetch_one(conn)                            # -> UserRow; raises RowNotFound

        names = await query[str](
            t"SELECT name FROM users ORDER BY name"
        ).fetch_all(conn)                            # -> list[str]  (scalar)

        ids = [1, 2, 3]
        n = await query(
            t"UPDATE users SET name = {'x'} WHERE id = ANY({ids})"
        ).execute(conn)                              # -> int (rows affected)

        total = await query[Decimal](
            t'SELECT sum(amount) AS "total!" FROM orders'   # "!" = force NOT NULL
        ).fetch_one(conn)

asyncio.run(main())
```

### 3.2 Public surface (`rowx/__init__.py`)

```python
from string.templatelib import Template
from typing import Self

class _BaseQuery[T]:                       # private; holds all behaviour
    _dynamic: ClassVar[bool]
    def __init__(self, template: Template, /) -> None: ...      # compiles eagerly (§3.5)
    async def fetch_one(self, conn: Connection, /) -> T: ...
    async def fetch_all(self, conn: Connection, /) -> list[T]: ...
    async def execute(self, conn: Connection, /) -> int: ...

class query[T = None](_BaseQuery[T]):            _dynamic = False
class query_unchecked[T = None](_BaseQuery[T]):  _dynamic = True

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
  - `fetch_one -> T`, `fetch_all -> list[T]`, `execute -> int`;
  - the scalar, tuple and `type`-alias forms;
  - `query(t"..") -> query[None]`.

  Both reject `str` and f-string arguments.
- **Do not add overloads** that make `fetch_one` on `query[None]` return `NoReturn`.
  `query[Any]` matches `self: query[None]` too, so it would also be typed `NoReturn`
  (verified in both checkers). Misuse is caught by `missing-row-type` (checker) and
  `InterfaceError` (runtime).
- Every public async method **MUST** be a Python `async def` that awaits the Rust future
  inside. The pyo3-async-runtimes Future is not a coroutine, and `asyncio.create_task`
  rejects it. A test checks `inspect.iscoroutinefunction`.

### 3.3 Method semantics

| Method | Result | Behaviour |
|---|---|---|
| `fetch_one` | `T` | Returns the first row, and raises `RowNotFound` if there are none. Extra rows are not an error. After the first row the Rust side drops the row stream; the server still sends the remaining rows and the connection task drains and discards them. DML side effects (`INSERT … RETURNING`) always run to completion. The docs recommend `LIMIT 1` for large results. |
| `fetch_all` | `list[T]` | Returns every row. |
| `execute` | `int` | Returns the rows affected from `CommandComplete`, or 0. It is allowed on `query[T]`: the row plan is not built and the rows are discarded. |

The **arity check** happens before execution, against the prepared statement's columns
(§3.4). A shape mismatch is reported even when the result is empty.

### 3.4 Row-type grammar and runtime row plan

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
- **Rejected** forms raise `InterfaceError` at the first `fetch_*` at runtime, and are
  `invalid-row-type` in the checker:
  - string or `ForwardRef`, and `TypeVar`;
  - a generic alias (`type Row[X] = …`);
  - bare `tuple`, `tuple[()]` and `tuple[X, ...]`;
  - a union with more than one non-None member;
  - `Literal`, `dict`, classes, and anything else not derivable from the grammar.
- **Nullable mask.** An element is nullable when it is `None`, a union with `None`, `Any`
  or `object`.

`T` is read **lazily** at the first `fetch_*` from `self.__orig_class__.__args__[0]`.
Python sets this after `__init__`, so it is not readable inside `__init__`. If the
attribute is missing, `T` is `None`. The compiled `RowPlan(kind, arity, nullable_mask)` is
cached per `T` object.

Runtime validation:

- **Arity.** Before executing, compare `len(stmt.columns())` with the plan's arity. A
  scalar plan needs exactly 1 column. A mismatch raises `RowShapeError`. Columns of type
  `void` count towards arity.
- **NULL check.** A NULL in a position the mask marks non-nullable raises `RowShapeError`
  naming the column index and name. The Rust decoder performs this check.
- **No-rows plan.** `fetch_*` raises
  `InterfaceError("query without a row type; use .execute() or add query[T]")`.
- Element types and nullability inside `list[...]` are **not** checked at runtime. A NULL
  array element decodes to `None`.

### 3.5 t-string compilation (`rowx/_template.py`)

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

### 3.6 Nullability overrides (SQLx convention)

A result column whose name ends in `!` is forced NOT NULL, and one ending in `?` is forced
nullable. You write it with a quoted alias: `count(*) AS "n!"`.

- The checker strips the suffix (§8.3).
- The runtime ignores names, because plans are positional. A NULL in a `!` column still
  raises `RowShapeError`.
- Postgres truncates identifiers at 63 bytes, which would silently drop the suffix. The
  checker reports a described column name of exactly 63 bytes whose quoted alias in the
  source SQL ends in `!`/`?` as `invalid-override`.
- Type overrides (`"col: Type"`) are deferred.

### 3.7 Errors (`rowx.errors`, re-exported from `rowx`)

```text
rowx.Error(Exception)
├── InterfaceError          # misuse / client-side: closed or broken connection, param-count
│   │                       # mismatch, unsupported or absent row type at fetch, DSN/network/TLS failure
│   └── ConnectionBusyError # concurrent operation on one connection (§7.5)
├── DatabaseError           # server ErrorResponse: .sqlstate .message .detail .hint .position
│                           # (incl. auth failures 28P01, unknown database 3D000, query canceled 57014)
├── RowNotFound             # fetch_one with zero rows
├── RowShapeError           # arity mismatch / NULL in non-nullable position
├── DecodeError             # value can't be represented as the Python type (infinity, interval months, …)
└── EncodeError             # Python value not accepted for the param's type (§6); names "$n" and both types
```

- **Construction-time misuse** raises a plain `TypeError` from `__init__`. Examples: a
  non-Template argument, or a dynamic interpolation in `query`.
- **Fetch-time row-type problems** raise `InterfaceError`.
- `DatabaseError.sqlstate` is a 5-character `str`, and `.position` is an `int | None`
  holding the 1-based character position.
- Sqlstate-specific subclasses are deferred.
- No error message or `repr` may include the DSN password.

---

## 4. Architecture

```text
┌──────────────────────────── Python (rowx package) ────────────────────────────┐
│  rowx/__init__.py, _query.py   query, query_unchecked, connect, Connection    │
│  rowx/errors.py                exception hierarchy (§3.7)                     │
│  rowx/_template.py             compile_parts / compile_template (shared)      │
│  rowx/_rowplan.py              T → RowPlan (grammar §3.4)                     │
│  rowx/_types.py                typed view of _core.type_table() (shared)      │
│  rowx/check/                   checker front-end (stdlib ast), CLI            │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │ PyO3 0.28 (abi3-py314)   rowx._core
┌──────────────────────────────────▼────────────────────────────────────────────┐
│  crates/rowx-py   bindings + ALL PyObject conversion                          │
│     conn.rs, exec.rs, describe.rs, errors.rs (single exception-mapping point) │
│     codec/{mod,extract,int,float,numeric,text,bytes,uuid,json,temporal,array} │
│  crates/rowx-pg   PURE RUST (no pyo3): testable with plain `cargo test`       │
│     dsn.rs, tls.rs, conn.rs, state.rs, cancel.rs, stmt_cache.rs, exec.rs      │
│     types.rs  (THE type table metadata)  wire/{numeric,temporal,array,...}.rs │
│     describe.rs, explain.rs                                                   │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │ tokio-postgres 0.7.x on a multi-thread tokio runtime
                                   ▼
                              PostgreSQL 18
```

Decisions:

- **`rowx-pg` is pure Rust.** It owns connections, the state machine, cancellation, the
  statement cache, TLS, describe, the EXPLAIN walker, the type-table metadata, and the
  byte-level wire codecs. Those return plain Rust values, such as numeric digits and
  microseconds.
- **`rowx-py` owns all PyObject work.** It extracts Python values into a neutral Rust enum
  (§7.3), builds Python objects from wire values, and maps errors in one place.
- **The checker front-end is Python** and uses stdlib `ast` and `tokenize`. It gets exact
  PEP 750 support and spans, and it can read type aliases from source without importing
  user code. All database work goes through `rowx._core`, over the same connection type the
  runtime uses.
- `rowx._core` **MUST** import, and `type_table()` **MUST** work, with no database. The
  checker's AST-only phases depend on this.

---

## 5. Packaging and toolchain

- There is one distribution, `rowx`. It is built with maturin and abi3-py314, with
  `requires-python = ">=3.14"`. Wheels target cp314-abi3: macOS arm64 and x86_64, and Linux
  manylinux x86_64 and aarch64. Windows is best effort, and `3.14t` is unsupported.
- Console script: `rowx = "rowx.check.cli:main"`.
- **Workspace layout.** A root `Cargo.toml` defines the workspace, with members
  `crates/rowx-pg` and `crates/rowx-py`. The `rowx-py` crate has `lib.name = "_core"` and
  `crate-type = ["cdylib"]`.
- **Maturin.** `[tool.maturin]` sets `manifest-path = "crates/rowx-py/Cargo.toml"`,
  `module-name = "rowx._core"`, `python-source = "src"`, and
  `features = ["pyo3/extension-module"]`. `extension-module` must **not** be hard-coded in
  `Cargo.toml`, because that breaks `cargo test` and `cargo clippy` linking.
- **uv cache keys.** `[tool.uv] cache-keys` must list `pyproject.toml`,
  `crates/**/*.rs`, `crates/**/Cargo.toml`, `Cargo.toml`, `Cargo.lock` and
  `rust-toolchain.toml`. Otherwise `uv run` silently tests a stale build.
- **Rust toolchain.** `rust-toolchain.toml` pins an exact stable release (≥ 1.85 for
  edition 2024) with `rustfmt` and `clippy`. `Cargo.lock` is committed.
- **Rust dependencies**, pinned to exact minor versions:
  - `tokio-postgres` 0.7.x, keeping its default `runtime` feature;
  - `tokio` with `rt-multi-thread`;
  - `pyo3` 0.28 with `pyo3-async-runtimes` 0.28 (tokio). These two minors always move
    together. 0.29 exists, and the upgrade is a separate task.
  - `tokio-postgres-rustls`, plus `rustls` with `default-features = false` and
    `features = ["ring","std","tls12"]`. There must be exactly one `CryptoProvider`.
    Linking two makes `ClientConfig::builder()` panic, and aws-lc-sys complicates
    manylinux-aarch64 builds.
  - `webpki-roots`, `rustls-pemfile`, `serde_json`, `lru`, `bytes`, `futures-util` and
    `thiserror`.
  - **Not** `sqlx` and **not** `sqlparser` (§11).
- **Python** has no runtime dependencies. The dev group holds `pytest`, `pytest-asyncio`
  (with `asyncio_mode = "auto"`), `pyright` and `mypy` (both version-pinned), and `ruff`.
  An optional `bench` group holds `asyncpg`.
- **Builds.** `maturin develop` and `uv run` produce **debug** builds, which are fine for
  tests. Benchmarks **MUST** use `maturin develop --release`. Never run a plain
  `cargo build` of the cdylib, because it doesn't link on macOS.
- **Datetime C-API.** abi3 doesn't expose it. Temporal decoding calls the Python
  constructors, cached with `PyOnceLock`. The spike's numbers were measured this way.

---

## 6. Type mapping (single source of truth)

`crates/rowx-pg/src/types.rs` defines one static table of `TypeInfo` rows. `rowx._core`
exports it as `type_table() -> list[TypeInfo]`. `rowx/_types.py` wraps it, and both the
checker and `_rowplan` read from there. **No other file may hard-code OID↔Python
mappings.**

```rust
pub struct TypeInfo {
    pub key: &'static str,                 // "int4", "timestamptz", "enum", "citext", "array", ...
    pub matcher: Matcher,                  // Oid(u32) | EnumKind | NamedSimple("citext") | ArrayKind | DomainKind
    pub py_result: &'static str,           // "int", "decimal.Decimal", "datetime.datetime", ...; "" if unsupported
    pub tz: TzRule,                        // None | Aware | Naive   (documentation + encode checks)
    pub accepted_params: &'static [&'static str],   // Python type names the encoder & checker accept
    pub decode: Codec, pub encode: Codec,  // enum variant per family; Unsupported allowed
    pub encode_format: Format,             // Binary | Text (default per row; arrays decide per value)
}
```

Codec dispatch uses the `tokio_postgres::types::Type` taken from `Statement::params()` and
`Column::type_()`, not the bare OID. tokio-postgres already resolves non-builtin OIDs at
prepare time into `Kind::{Enum, Domain, Array, …}`. rowx resolves recursively:

- `Domain(b)` uses the codec of `b`;
- `Enum` uses the text-label codec;
- `Array(e)` uses the codec of `e`.

Result columns arrive with the **base** type OID for domains. Parameters and array elements
carry the **domain** OID, so a binary array encode writes `e.oid()`, the element OID as
declared.

| Postgres type | Result type (checker leaf) | Decode | Accepted param types (checker = runtime) | Encode |
|---|---|---|---|---|
| `bool` | `bool` | binary | `bool` | binary |
| `int2`, `int4`, `int8` | `int` | binary | `int` (never `bool`) | binary, range-checked → `EncodeError` |
| `oid` | `int` | binary | `int` | binary |
| `float4`, `float8` | `float` | binary | `float`, `int` | binary (`int` > 2^53 loses precision; documented) |
| `numeric` | `decimal.Decimal` | binary → Decimal (NaN, ±Infinity) | `Decimal`, `int` | text (`Decimal('sNaN')` → `EncodeError`) |
| `text`, `varchar`, `bpchar`, `name`, `citext` | `str` | binary UTF-8 | `str` **only** | binary |
| `"char"` (OID 18) | `str` | binary (1 byte) | `str` (len 1) | binary |
| `bytea` | `bytes` | binary | `bytes`, `bytearray`, `memoryview` | binary |
| `date` | `datetime.date` | binary | `date` (not `datetime`) | binary |
| `time` | `datetime.time` (naive) | binary | naive `time` | binary |
| `timetz` | `datetime.time` (aware) | binary; the zone is stored as seconds **west** of UTC, so negate it | aware `time` (naive → `EncodeError`) | text |
| `timestamp` | `datetime.datetime` (naive) | binary | naive `datetime` (aware → `EncodeError`) | binary |
| `timestamptz` | `datetime.datetime` (UTC-aware) | binary | aware `datetime` (naive → `EncodeError`) | binary |
| `interval` | `datetime.timedelta` | binary | `timedelta` | binary |
| `uuid` | `uuid.UUID` | binary | `UUID`, `str` | binary (`UUID`), text (`str`) |
| `json`, `jsonb` | any annotation (§9.5) | `json.loads` (jsonb: strip the `0x01` version byte) | any JSON-serialisable value, always `json.dumps`-ed. A `str` becomes a JSON **string**; to bind raw JSON text, write `{s}::jsonb` | text |
| enum | `str` | binary (the label) | `str` | text |
| domain | as its base type | as its base type | as its base type | as its base type |
| `X[]` | `list[X']`; nested lists for multi-dim | binary arrays | `list`/`tuple` of accepted X (`str`/`bytes` are never sequences) | binary if every element encodes binary, else a text literal (§7.3) |
| `void` | `None` (tuple element only; §9.5) | → `None` | – | – |

Decode errors (`DecodeError`, each with a hint to cast to `text`):

- `date`, `timestamp` or `timestamptz` that is ±infinity, or whose year is outside 1..9999;
- `interval` with a non-zero months component, or ±infinity.

**Unsupported types.** Any other type is `unsupported-type` in the checker and `DecodeError`
at runtime. Examples: `money`, `inet`/`cidr`, ranges, composites, `xml`, `hstore`, `regclass`,
`aclitem`.

- A type with no binary send function (`pg_type.typsend = 0`, e.g. `aclitem`) makes the
  whole query fail at Bind with 42883. The checker therefore also reports these as
  `unsupported-type` (§8.1).
- Adding a type later means appending a row to the table.

Notes:

1. `None` (SQL NULL) is accepted for every parameter. Parameter nullability is never
   checked.
2. **Array element nullability** works as in SQLx. `list[int]` matches `int4[]`, and
   `list[int | None]` is also accepted; this is the only relaxation of exact matching. The
   rule applies at every nesting level.
3. **Extracting parameters.** `bool` is checked before `int`, and `datetime` before `date`,
   because they are subclasses.

---

## 7. Rust driver

### 7.1 Connection and DSN (`rowx-pg/src/dsn.rs`, `tls.rs`, `conn.rs`)

rowx **pre-parses** the DSN, in both URL and key=value forms. It removes `sslmode`,
`sslrootcert`, `sslcert`, `sslkey` and `sslcrl`, and passes everything else to
`tokio_postgres::Config`. This is needed because tokio-postgres rejects `verify-ca`,
`verify-full` and `sslrootcert`.

| `sslmode` | tokio-postgres mode | rustls verifier |
|---|---|---|
| `disable` | `Disable` | – |
| `prefer` (default) | `Prefer` | accepts any cert, as libpq does |
| `require` | `Require` | accepts any cert, but verifies the CA chain when `sslrootcert` is given (libpq) |
| `verify-ca` | `Require` | chain verified against `sslrootcert`, else webpki roots; hostname not checked |
| `verify-full` | `Require` | full WebPKI verification, including the hostname |

- `sslcert`/`sslkey` (client certs) and `sslcrl` raise `InterfaceError("not supported in
  v1")`.
- Each connection spawns its tokio-postgres connection task through the runtime `Handle`
  stored at module init. When that task fails, the state becomes `Broken`.
- **Error mapping.** DSN parse errors, IO errors, TLS errors and timeouts become
  `InterfaceError`. A server `ErrorResponse` becomes `DatabaseError`.
- `close()` works as follows:
  - If the state is `Busy` or `Settling`, it cancels and settles first (§7.6).
  - It then drops the client and awaits the connection task. Close is idempotent.

### 7.2 Execution path (`exec.rs`)

The binding signatures (in `_core.pyi`) are:

```text
Connection._fetch(sql: str, params: list[object], mask: list[bool] | None,
                  scalar: bool, limit_one: bool, arity: int | None) -> list[object]
Connection._execute(sql: str, params: list[object]) -> int
```

1. **Under the GIL**, synchronously in the `#[pymethods]` body:
   - claim the connection, Idle → Busy (§7.5);
   - extract every parameter into the neutral `PyParam` enum (§7.3).
2. **Without the GIL**, in a spawned tokio task:
   - Look up the statement cache; on a miss, `prepare(sql)` with no parameter types, so
     the server infers them.
   - Check that `params.len()` equals `stmt.params().len()`, else raise `InterfaceError`.
   - Check that `arity` equals `stmt.columns().len()`, else raise `RowShapeError`.
   - Encode each parameter against its `Type` (§7.3).
3. Run `query_raw`. Per-parameter formats come from each value's
   `ToSql::encode_format(&Type)`, and tokio-postgres always requests binary results.
4. Collect the raw rows, `Vec<Option<Bytes>>` with types. Apply the `mask` NULL check while
   collecting. With `limit_one`, drop the stream after the first row.
5. **With the GIL**, in one pass, build Python tuples (or bare values when `scalar`).
6. Move the state Busy → Idle **before** the result is delivered to Python, so
   `await a(); await b()` never sees Busy.

**Schema-change retry.** This applies when the server answers `0A000` and
`DbError::routine() == "RevalidateCachedQuery"` ("cached plan must not change result type").

- Evict the cache entry, re-prepare and retry **once**.
- If the retry fails with `25P02` (inside a transaction), raise the **original** error as
  `DatabaseError`, with the hint "schema changed inside a transaction; ROLLBACK and retry".
- On `26000` ("prepared statement does not exist", e.g. after `DISCARD ALL`), clear the
  whole cache and retry once.

### 7.3 Codec (`rowx-py/src/codec/`, `rowx-pg/src/wire/`)

**Extract (GIL held).** Python values are converted to a `Send` enum:

```rust
enum PyParam { Null, Bool(bool), Int(i64), BigInt(String), Float(f64), Str(String),
  Bytes(Vec<u8>), Decimal(String), Date{..}, Time{.., utcoffset: Option<i32>},
  DateTime{.., utcoffset: Option<i32>}, Timedelta{days,secs,micros}, Uuid([u8;16]),
  Seq(Vec<PyParam>), Json(String) /* json.dumps result, produced lazily: see below */,
  Other(Py<PyAny>) }
```

`dict`, and any value that is not one of the above, becomes `Other`.

**Encode (no GIL).** Each value is encoded against its parameter `Type`, using the
`accepted_params` and `encode_format` from §6.

- A type not in `accepted_params` raises `EncodeError`, naming `$n`, the Python type and the
  Postgres type.
- When the target is `json`/`jsonb`, the encoder reacquires the GIL (`Python::attach`) to
  run `json.dumps` on the original object.

**Array text literal** (used when an element needs text format):

- Build it recursively: `{` elements joined by `,` `}`.
- Every non-NULL element is double-quoted, with `\` and `"` backslash-escaped. NULL is the
  bare `NULL`, and an empty list is `{}`.
- Nested lists must be rectangular; a ragged list raises `EncodeError`.
- This is still a bound parameter, so an escaping bug can corrupt values but can never
  inject SQL.

**Decode.** A custom `FromSql`, `Raw<'a>(Option<&'a [u8]>, &Type)`, accepts every type. Pure
wire parsers in `rowx-pg/src/wire/` produce Rust values, and `rowx-py/src/codec/` turns them
into Python objects.

- Constructors for `Decimal`, `UUID`, `datetime`, `date`, `time`, `timedelta` and
  `timezone.utc` are cached.
- jsonb strips its version byte, then calls `json.loads`.

**Logging.** tokio-postgres's `log` debug output prints parameters through `Debug`. The
parameter wrapper's `Debug` impl **MUST** redact values.

**Performance bar (v1).** Fetching 100k rows of (int4, text, timestamptz) with a release
build **should** take no longer than asyncpg does on the same machine. The spike measured
35.7 ms against asyncpg's 46.5 ms. This is tracked by a non-gating benchmark job.

### 7.4 Statement cache (`stmt_cache.rs`)

- An LRU per connection, keyed by SQL string, with capacity `statement_cache_size`
  (default 256). Setting it to `0` disables caching: every call uses an unnamed prepare.
- A `Statement` is `Clone`, backed by an `Arc`. tokio-postgres sends Close only when the
  **last** clone drops, so evicting an entry while a query using it is in flight is safe.
- A future cancelled during `prepare` may leak a named server statement until the
  connection closes. This is documented and accepted.
- **External poolers.** In transaction mode, users need `statement_cache_size=0`, or
  PgBouncer ≥ 1.21 with `max_prepared_statements`. This is documented.

### 7.5 Connection state machine (`state.rs`)

The states are `Idle | Busy | Settling | Broken | Closed`, held in an atomic and changed by
compare-and-swap in the synchronous part of each `#[pymethods]` call.

| Call arrives while | Result |
|---|---|
| `Idle` | → `Busy`, proceed |
| `Busy` | raise `ConnectionBusyError` immediately (asyncpg semantics; no queueing) |
| `Settling` | await the settle notification, then retry the claim |
| `Broken` / `Closed` | raise `InterfaceError` |

Decision: there is one operation per connection. Transactions are written as
`query(t"BEGIN").execute(conn)` and `COMMIT`/`ROLLBACK`, and the user **MUST** own the
connection from a single task. The docs say this.

### 7.6 Cancellation (`cancel.rs`)

1. Each operation runs as a spawned tokio task that owns the request: the `Statement` and
   the `RowStream`. The Python-facing future awaits a oneshot from that task.
2. A **guard** is armed from "request sent" to "response fully consumed", and is disarmed
   on **every** completion path, success or error. Only dropping the Python-facing future
   while the guard is armed counts as a cancellation. Error paths never send cancels.
3. **On cancellation**, the state becomes `Settling`, and a detached task, spawned with the
   stored `Handle` because `Drop` may run outside a runtime, does the following:
   - (a) It opens a connection to the server, using the same TLS settings. It writes the
     16-byte CancelRequest (from the backend key data), then **reads until EOF**. The
     server closes this socket only after it has signalled the backend.
     tokio-postgres's `cancel_query` doesn't wait for this, so rowx implements it itself.
   - (b) It keeps draining the abandoned operation until `ReadyForQuery` or an error.
   - (c) It moves the state to `Idle`, or to `Broken` on an IO error, and notifies waiters.

Without (a) and (b), a stale cancel kills the *next* query. The review measured 166 of 300
follow-up queries failing with 57014.

A cancelled statement inside `BEGIN` aborts the transaction, which then gives 25P02. The user
must `ROLLBACK`; this is documented.

**Acceptance:**

- Cancelling `pg_sleep(3)` frees the backend. A poll of `pg_stat_activity` on another
  connection shows the sleep gone within 2 s.
- The next query on the same connection succeeds.
- In a 300-iteration stress test, a ~5 ms query is cancelled at a random point and a query
  follows immediately. **Zero** follow-ups may fail.

---

## 8. Describe and nullability (`rowx-pg/src/describe.rs`, `explain.rs`)

### 8.1 Interface and checker session

```rust
pub struct Describe { pub params: Vec<ParamInfo>, pub columns: Vec<ColumnInfo> }
pub struct ParamInfo  { pub oid: u32, pub type_name: String, pub type_key: Result<&'static str, Unsupported> }
pub struct ColumnInfo {
    pub name: String,                     // override suffix stripped
    pub raw_name: String,                 // as returned by the server
    pub oid: u32, pub type_name: String, pub type_key: Result<&'static str, Unsupported>,
    pub table_oid: Option<u32>, pub attnum: Option<i16>,
    pub nullable: bool,                   // final, after defaulting
    pub reason: NullReason,
}
pub enum NullReason { NotNullColumn, NullableColumn, NotValidatedConstraint, ForeignOrView,
    OuterJoin { node: String }, GroupingSets, ScanBoundary { alias: String },
    ReturningOldNew, Computed, ExplainFailed, OverrideBang, OverrideQuestion }
```

The Python binding is `rowx._core.describe(conn, sql) -> Describe`. When the server
rejects the SQL it raises `DatabaseError` with `.position` set.

**Checker session.** On connect, the checker runs:

- `SET default_transaction_read_only = on`;
- `SET statement_timeout = <cfg, default 10s>`;
- `SET lock_timeout = '2s'`;
- `SET idle_in_transaction_session_timeout = '30s'`;
- `SET application_name = 'rowx-check'`.

**Safety statement** (this exact wording goes in the docs):

- The checker performs Parse/Describe, catalog lookups and `EXPLAIN` only, never
  `ANALYZE`. It does not execute your statements.
- Postgres may still **evaluate user-defined IMMUTABLE and STABLE functions while
  planning**, and it takes locks on referenced relations.
- Run it as a dedicated non-superuser role against a CI or disposable database.

### 8.2 Algorithm

1. **Prepare** with `client.prepare(sql)`.
   - A server error is `sql-error`, carrying its position.
   - Multi-statement strings fail here, and this is a security invariant: step 3
     concatenates the SQL into a simple-protocol query, so it relies on Parse having
     rejected anything with a second command. It is covered by a test.
   - `ParamInfo` and `ColumnInfo` come from `Statement::params()`/`columns()`, using
     `Column::table_oid()`/`column_id()`. Unsupported types are resolved through
     `pg_type.typsend`.
2. **Base nullability.** One batched catalog query covers every column that has a table
   origin. It joins `pg_attribute a`, `pg_class c ON c.oid = a.attrelid` and
   `pg_constraint`.
   - **NotNull** (`NotNullColumn`) requires all of:
     - `a.attnotnull`;
     - `c.relkind IN ('r','p','m')`;
     - `NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = a.attrelid AND k.contype = 'n'
       AND NOT k.convalidated AND k.conkey = ARRAY[a.attnum])`.
       PG18 `NOT VALID` NOT NULL constraints set `attnotnull` even though NULL rows still exist.
   - A foreign table or view gets `ForeignOrView` (Nullable). A non-validated constraint
     gets `NotValidatedConstraint` (Nullable). Any other column with a table origin gets
     `NullableColumn`.
   - A column with no table origin gets `Computed` (Unknown).
3. **Plan pass.** This step is skipped when the statement returns 0 columns.
   - Run `EXPLAIN (GENERIC_PLAN, VERBOSE, COSTS OFF, FORMAT JSON)` + sql through
     **`simple_query`**. Over the extended protocol, EXPLAIN would demand values for `$n`.
   - If EXPLAIN fails, keep the base results and mark Unknown columns `ExplainFailed`.
     This is non-fatal. Utility statements that return columns, such as `SHOW`, `CALL` and
     `FETCH`, fail EXPLAIN with 42601.
   - The walk is described in §8.2.1. Its result can only promote a column to Nullable,
     never the reverse.
4. **Overrides** (§8.3).
5. **Defaulting.** An Unknown column becomes Nullable (SQLx). Computed columns therefore
   need `"col!"`.

#### 8.2.1 Plan walk (`explain.rs`, a pure function over `serde_json::Value`)

Input is the plan JSON and the column count. Output is `Vec<Option<NullReason>>`, the
promotions for each top-level column position.

- **Top outputs.** Take the top node's `Output` list. Every top position whose expression
  string equals a marked expression is promoted. Duplicate columns are each promoted.
- **Join marking.** At a node with `Join Type`, mark the nullable children:
  - `Full`: both children;
  - `Left`: the child whose `Parent Relationship` is `Inner`;
  - `Right`: the child whose `Parent Relationship` is `Outer`.

  Semi and anti joins never null-extend. A marked subtree stays marked, and its `Output`
  expressions become marked.
- **Descend through every node type.** That includes Sort, Limit, Aggregate, Hash,
  Materialize, Gather, Append and SubPlans/InitPlans, not only joins at the top.
- **Scan boundaries.** These cover a `Subquery Scan`, `CTE Scan` or `WorkTable Scan` with
  alias `x`. For a CTE, the source subtree is the InitPlan whose `Subplan Name` is
  `CTE x`.
  - If that source subtree contains any null-extending join, or a grouping-sets aggregate,
    every top output that starts with `x.` is promoted (`ScanBoundary`).
  - This is deliberately conservative. Mapping positions precisely is future work.
- **Grouping sets.** An `Aggregate` node with a `"Grouping Sets"` key holding more than
  one set marks its own `Output` (`GroupingSets`).
- **RETURNING old/new (PG18).** On a `ModifyTable` top node:
  - outputs starting with `old.` are promoted for `Operation` Insert or Merge;
  - outputs starting with `new.` are promoted for Delete or Merge.

  If a `RETURNING WITH (OLD AS o, NEW AS n)` alias makes the prefix ambiguous, promote
  every output for Merge, and promote by the declared aliases otherwise.

### 8.3 Overrides

If the raw column name ends in `!`, strip the suffix and set NotNull (`OverrideBang`). If it
ends in `?`, strip it and set Nullable (`OverrideQuestion`). Overrides are applied last and
always honoured.

### 8.4 Evidence: why not sqlx's describe

Measured on Postgres 18 in the spike and the review. The schema is
`users(id pk, name not null, email null)` and `posts(id pk, user_id not null, title not null)`.

| Query | Column | Truth | sqlx 0.9.0 | rowx (§8.2) |
|---|---|---|---|---|
| `users u LEFT JOIN posts p … WHERE u.id = $1` | p.title | nullable | **NOT NULL** ✗ | nullable |
| `posts p RIGHT JOIN users u` | p.title | nullable | **NOT NULL** ✗ | nullable |
| `LEFT JOIN … ORDER BY p.title` | p.title | nullable | **NOT NULL** ✗ | nullable |
| trivial `(SELECT … LEFT JOIN …) x` (the planner elides it) | x.title | nullable | **NOT NULL** ✗ | nullable |
| `(SELECT p.title, row_number() over () … LEFT JOIN …) x` | x.title | nullable | ✗ | nullable (ScanBoundary) |
| `WITH c AS MATERIALIZED (… LEFT JOIN …) SELECT title FROM c` | title | nullable | ✗ | nullable (ScanBoundary) |
| `SELECT name FROM users GROUP BY ROLLUP(name)` | name | nullable | ✗ | nullable (GroupingSets) |
| `INSERT … RETURNING old.name` | name | nullable | ✗ | nullable (ReturningOldNew) |
| `SELECT p.title, p.title … LEFT JOIN` | both | nullable | ✗ | both nullable |
| column with `NOT NULL … NOT VALID` | x | nullable | ✗ | nullable |
| `FULL JOIN` | both | nullable | nullable | nullable |
| `LEFT JOIN … WHERE p.title IS NOT NULL` (the planner reduces it to an inner join) | p.title | not null | – | not null ✓ |
| `upper(name)`, `count(*)`, `coalesce(..)`, `1` | – | – | unknown | unknown → nullable |

sqlx gets these wrong for three reasons:

1. It EXPLAINs `EXECUTE stmt(NULL…)` without forcing a generic plan, so `= NULL`
   constant-folds the join away.
2. It treats `Inner` as the nullable side of a right join.
3. It only walks joins at the top level.

Its `describe` is also `#[doc(hidden)]` and feature-gated. rowx keeps sqlx's **model**.

### 8.5 Known limits (pinned by tests, documented)

- Matching expression strings is heuristic. LATERAL, window functions over outer joins,
  and nested CTE chains are covered by the conservative scan-boundary rule. They are
  pinned by `tests/describe/test_known_limits.py`.
- **Views** are always Nullable, because `attnotnull` is false for view columns.
- **Set operations and recursive CTEs** have `table_oid = 0`, so they are Unknown →
  Nullable. That is safe.
- **Computed columns** are always Unknown → Nullable. NOT NULL narrowing for computed
  columns would need a parser (§11).

---

## 9. Static checker (`rowx check`)

### 9.1 CLI

```text
rowx check [PATHS...] [--database-url URL] [--config FILE] [--output-format text|json]
           [--statistics] [-j N]
```

- **Config.** `pyproject.toml` is found by searching upward from the current directory,
  unless `--config` is given. A missing `[tool.rowx]` table means defaults.
- **Paths.** Explicit `PATHS` are resolved relative to the current directory. They
  override `include` but still honour `exclude`.
- **Database URL.** It comes from `--database-url`, else the env var named by
  `database_url_env`.
- **Concurrency.** `-j` defaults to `min(4, os.cpu_count())`. Parsing and discovery run
  sequentially, and describes run concurrently over N connections, each with one operation
  in flight.
  - Describe results are cached per distinct SQL string, and diagnostics are reported per
    call site.
  - If a connection is lost mid-run (57P01 or 08xxx), the describe is retried once on a
    fresh connection; if that fails, the checker exits 2.
  - Ctrl-C cancels outstanding describes.
- **Output.** Diagnostics are sorted by (path, line, col, rule) before printing, so
  `-j 1` and `-j 8` produce byte-identical output.
- **Exit codes:**
  - **0:** no diagnostics, including when no queries were found;
  - **1:** one or more diagnostics;
  - **2:** a usage or config error, a missing or unreachable database, a describe failure
    that is not an SQL error (timeout 57014, connection loss), an internal error, or a
    Python older than 3.14.
- There are no warnings, severities, ignore comments or `--fix`.
- **`--statistics`** reports:
  - files scanned and call sites found;
  - call sites checked, and distinct SQL strings described;
  - `query_unchecked` calls skipped;
  - diagnostics per rule.

  It goes to stdout in text mode and to stderr in JSON mode.

### 9.2 Config (`pyproject.toml`)

```toml
[tool.rowx]
include = ["src"]                 # default ["."]
exclude = ["**/migrations/**"]    # always also: .git, .venv, venv, node_modules, __pycache__, site-packages, build, dist
database_url_env = "DATABASE_URL"
statement_timeout = "10s"         # integer + unit: ms|s|min
```

Unknown keys are an error (exit 2), with a did-you-mean suggestion; port the logic from
`sqlty/_config.py`. `.gitignore` is honoured through `git ls-files --others --exclude-standard
--cached`, with a fallback to the exclude globs when git isn't available.

### 9.3 Discovery (`check/discover.py`)

- Every file is read as bytes and decoded as UTF-8, honouring a BOM and any `# coding:`
  line. It is parsed with `ast.parse(source, filename)`, with no `feature_version`.
- A decode failure or `SyntaxError` is a `syntax-error` diagnostic at the reported
  position, and the file is skipped.

**Name resolution is lexical.**

- An import (`from rowx import query [as q]`, `import rowx [as rx]`) binds the name in the
  scope where it appears, whether that is module level, function level, under
  `if TYPE_CHECKING:` or inside `try:`. The innermost scope wins.
- If the name is also bound any other way in that scope (by assignment, a parameter, a
  `for` target, and so on), the name is ambiguous, and each use of it is `indirect-query`.

**Allowed uses** of a name that resolves to `rowx.query` or `rowx.query_unchecked`:

- (a) `NAME[T](arg)` or `NAME(arg)`, where the `Call`'s `func` is exactly that `Subscript`
  or `Name` (or `rx.query…` as an `Attribute`). Chaining (`….fetch_one(conn)`, `await …`)
  is irrelevant, because the inner `Call` is the match.
- (b) Annotation position, and `isinstance` or `issubclass` arguments.
- Any other use is `indirect-query`. That covers an assignment right-hand side
  (`Q = query[T]`), a call argument (`partial(query, …)`), a base class, a `return`
  value, `from rowx import *`, and `getattr(rowx, …)`.

**Call shape.**

- For `query`, the call must have exactly one positional argument and no keywords,
  starred arguments or `**`. That argument must be an inline `ast.TemplateStr`, optionally
  parenthesised. Anything else is `dynamic-query`.
- For `query_unchecked`, the call is counted and its SQL is skipped. Its `T` is still
  resolved, and is reported as `invalid-row-type`/`unresolved-row-type` when it fails.

**Interpolations in `query`.**

- A `conversion` other than -1 (this includes `{x=}`) is `dynamic-query`.
- Any `format_spec` is `dynamic-query`.
- An interpolation whose expression is an inline `TemplateStr`, or whose evident type
  (§9.6) is `string.templatelib.Template`, is `dynamic-query`.
- Any other nested `Template` is caught only at runtime, as a `TypeError`.

#### 9.3.1 Source map (normative algorithm, `check/sourcemap.py`)

`ast` offsets alone aren't enough:

- They are UTF-8 byte columns.
- `Constant` spans cover raw source, including escapes and `{{`.
- Implicit concatenation merges pieces into one `Constant`, whose span includes the quote
  seams.
- CRLF line endings are normalised in values.

The algorithm:

1. **Read lines.** Keep each line's original terminator. A diagnostic's `line` is 1-based,
   and its `col` is a 1-based **Unicode code point** index on that line. JSON output uses
   the same units, with the end exclusive. Byte columns from `ast` are converted with
   `len(line_bytes[:col].decode())`.
2. **Collect tokens.** For a query's `TemplateStr` node N, take the `tokenize` tokens that
   start within N's span. Walk them at brace depth 0, counting depth with `OP '{'` and
   `OP '}'`, and collect the `TSTRING_MIDDLE` tokens. Prefix letters come from the
   preceding `TSTRING_START`; `r` means raw. Nested strings inside interpolations sit at
   depth > 0 and are ignored.
3. **Decode each middle token** into characters, each with a (line, col) position.
   Escapes are handled as follows:
   - `\<newline>` is dropped;
   - `\\ \' \" \a \b \f \n \r \t \v`, `\ooo`, `\xhh`, `\uXXXX`, `\UXXXXXXXX` and `\N{…}`
     each produce 1 character;
   - an unknown escape such as `\q` produces 2 characters;
   - `\r\n` becomes one `\n`;
   - in raw strings, every character maps to itself.

   For `{{`/`}}`, the token already holds a single brace, and the second source brace is a
   position gap that is ignored.
4. **Verify.** Split the decoded characters into segments at the interpolations. Each
   segment **MUST** equal the corresponding `ast.Constant.value`, with adjacent Constants
   concatenated. On any mismatch, mark the segment `inexact`: its carets fall back to the
   start of the segment. A wrong caret is never emitted.
5. **Compile.** Call `_template.compile_parts(segment_strings)`, which gives
   `Compiled.segments` and `.placeholders`. Then build `sql_index → source position`.
6. **Map server positions.** The server's `position` is a 1-based **character** index into
   the SQL. Take `p − 1`:
   - inside a placeholder range, it maps to the whole `{expr}` span, braces included;
   - inside an exact segment, it maps to that character's position;
   - inside an inexact segment, it maps to the segment start;
   - `p − 1 == len(sql)` (an error at end of input) maps to the end of the last segment;
   - a space inserted by the adjacency rule maps to the following placeholder.

Required fixtures:

- leading non-ASCII text;
- `\n`, `\x41` and `é`;
- `{{ }}`;
- `rt""`;
- a triple-quoted multi-line string;
- a CRLF file;
- parenthesised multi-line `t"" t""` concatenation;
- an error located after `{x}`;
- an error at end of input.

### 9.4 Resolving T (`check/resolve.py`)

`T` is the subscript expression, or `None` when there isn't one. It is parsed into the
§3.4 grammar.

- Leaf names are matched by their **resolved import**: `int`, `decimal.Decimal`,
  `datetime.date`, `typing.Any`/`typing_extensions.Any`, `typing.Optional`/`Union`/
  `Annotated`, and so on. This works whatever the local alias is (`import datetime as dt;
  dt.date`).
- Quoted strings inside T are `invalid-row-type`.

**Aliases.** A name is resolved in the module where it appears. Supported binding forms
are `type X = …`, `X: TypeAlias = …`, and a module-level `X = <pure type expression>` (no
calls). The import table is followed through:

- absolute and relative imports;
- `import m as x; x.Name`;
- `__init__` re-exports;
- `from m import *`, within project modules only, using `__all__` when it is a literal.

Additional rules:

- A name bound more than once at module level is `unresolved-row-type`.
- Generic aliases are `invalid-row-type`.
- At most 16 hops are followed. A cycle is `unresolved-row-type`.
- `TYPE_CHECKING` branches are followed, and the first binding wins.

**Module graph.**

- The roots are the `include` directories. For a `src/` layout, the directory containing
  the file's top-level package is also a root.
- `sys.path` is **never** read, and user code is **never** imported or executed. A test
  uses a module that raises at import, and asserts that `sys.modules` is unchanged.

**Diagnostic location.** The primary span is the `T` expression at the call site. When `T`
came from an alias, a `note: alias defined at path:line:col` is added.

### 9.5 Rules (all errors)

| Rule | Condition |
|---|---|
| `syntax-error` | The file can't be decoded or parsed. |
| `indirect-query` | A disallowed use of `query`/`query_unchecked` (§9.3). |
| `dynamic-query` | The argument is not one inline t-string, or it has a conversion, format spec, or evident nested Template. |
| `unresolved-row-type` | `T` can't be resolved statically. |
| `invalid-row-type` | `T` isn't derivable from the §3.4 grammar. |
| `sql-error` | The server rejected the SQL at Parse. The diagnostic carries sqlstate, message, detail and hint. Help text is added for 42P18/42P08 ("cast the parameter: `{x}::int`") and for 42501. |
| `param-count-mismatch` | `len(describe.params) ≠` the number of interpolations. It is reported at the first interpolation whose `$n` the server doesn't reference, or at the t-string. Help: "an interpolation inside a string literal or comment is not a parameter". |
| `missing-row-type` | `T` is `None` and the statement returns at least one non-`void` column. |
| `unexpected-row-type` | `T` is not `None` and the statement returns 0 columns, counting a statement whose columns are all `void` as 0. |
| `arity-mismatch` | A tuple's arity differs from the column count, or a scalar `T` doesn't face exactly 1 column. When this fires, no per-element rules run for that call. |
| `unsupported-type` | A column or parameter type isn't in the table, or has `typsend = 0`. |
| `type-mismatch` | The column's Python type (§6) differs from the element's leaf, ignoring `None`. `bool` and `int` are distinct. Naive versus aware datetimes are not checked. |
| `nullability-mismatch` | The column is nullable but the element lacks `None`, **or** the column is NOT NULL but the element includes `None` (exact). |
| `invalid-override` | An alias ending in `!`/`?` was truncated at 63 bytes (§3.6). |
| `param-type-mismatch` | An interpolation's evident Python type (§9.6) isn't in that parameter's `accepted_params` (§6). |

Exceptions to exact matching:

- `Any` and `object` match any column type and either nullability; they are the only
  nullability-neutral annotations.
- JSON columns accept any leaf annotation. Nullability rules still apply unless the
  element is `Any` or `object`.
- An array element may be `X` or `X | None`, at every nesting level.
- A `void` column matches only the tuple element `None`. A scalar `T` facing a `void`
  column is `type-mismatch`, with help "use .execute()".

### 9.6 Parameter type inference (`check/params.py`, sqlty-style)

The type is **evident** only in these cases. In every other case it is unknown, and an
unknown type is **never** reported.

- **Literals:** `int`, `float`, `str`, `bytes`, `bool`, `None`, an inline t-string (giving
  `Template`), and list or tuple literals whose elements are all of one evident type.
  (`list[int]` matches an array parameter element-wise.)
- **Calls of known constructors:**
  - `decimal.Decimal(...)`, `uuid.UUID(...)`;
  - `datetime.date(...)`, `datetime.datetime(...)`, `datetime.timedelta(...)`;
  - `datetime.datetime.now(...)` gives `datetime`.

  Constructors are matched by their resolved import.
- **Names.** The name must have exactly one binding in its innermost function or module
  scope. That binding must be one of:
  - an annotated function parameter;
  - an `AnnAssign`;
  - an `Assign` whose right-hand side is evident.

  The type is unknown if anything in that scope uses an `AugAssign`, `global`, `nonlocal`,
  `del`, walrus, loop target, `with … as`, unpacking target or `except … as` on the name.
  Comprehension variables have their own scope. Class-body names are invisible to methods.
  Closures follow normal lexical lookup. In an annotation, `X | None` counts as `X`, and a
  union of several non-None types counts as unknown.

### 9.7 Output

Text format:

```text
error[nullability-mismatch]: column 3 "email" may be NULL but tuple element 3 is `str`
  --> app/users.py:14:43
   |
14 |         user = await query[tuple[int, str, str]](
   |                                            ^^^ element 3
   |
   = note: nullable because users.email has no NOT NULL constraint
   = help: annotate `str | None`, or alias the column as "email!" if it can't be NULL
```

- Carets for element rules point at that **element** when `T` is inline, and at the `T`
  expression, plus an alias note, otherwise.
- `sql-error` carets come from §9.3.1.
- Nullability notes are rendered from `NullReason`. Examples: "right side of LEFT JOIN
  (Hash Left Join)" and "computed expression; add AS \"col!\"".

JSON format: stdout carries **one JSON array**, which is `[]` when the run is clean. Each
item has these fields:

```text
{ "rule", "message", "file" (project-relative, forward slashes), "line", "col",
  "end_line", "end_col" (1-based code points, end exclusive), "notes": [str],
  "help": str|null, "sqlstate": str|null, "sql": str|null, "column_index": int|null }
```

Exit-2 failures go to stderr as text. The DSN or password is never printed.

---

## 10. Testing strategy

- **Provisioning.**
  - `scripts/pg.sh up [--ssl]`, `down` and `dsn` run `postgres:18` on a random localhost
    port, with `-c max_connections=200`, and print the DSN.
  - `scripts/gen-certs.sh` generates the TLS material: a CA, a server certificate for
    localhost and 127.0.0.1, and a wrong-host certificate.
  - Tests read `ROWX_TEST_DSN`. When it is unset, DB tests **skip** locally, and they
    **fail** when `ROWX_REQUIRE_DB=1`, which CI sets.
  - Docker Desktop, OrbStack or colima is needed locally on macOS.
- **Isolation.** Each test gets a schema `t_<hex>`, created from `tests/schema.sql` and
  selected with `search_path`, set through the DSN `options=-c search_path=…`. Rollback-only
  isolation is not enough, because the cancel, TLS and busy tests need real sessions.
- **Seed schema** (`tests/schema.sql`) includes:
  - `users`, `posts` and `orders` (§8.4);
  - an enum, a domain and a domain array;
  - the `citext` extension;
  - a table with every §6 type;
  - a `NOT VALID` NOT NULL column;
  - a foreign table (via `file_fdw`) or a view.

  Seed data (`tests/seed_data.sql`) holds NULL-heavy rows.
- **Markers.** `db` marks tests that need Postgres, and `timing` marks the cancel, busy
  and timeout tests. Timing tests synchronise with `pg_stat_activity` polling, not sleeps,
  and use generous CI bounds.
- **Rust.**
  - Pure unit tests: the walker on captured plan fixtures, wire codecs, the LRU, the state
    machine and DSN parsing.
  - Integration tests (`crates/rowx-pg/tests/*.rs`) use `tests/common/mod.rs` for the DSN
    and a fresh schema.
- **Plan fixtures.** `scripts/capture-explain.sh` writes
  `crates/rowx-pg/tests/fixtures/plans/*.{sql,json}`. `--check` regenerates them and diffs
  the result.
- **Goldens.** There is one file per type family in `tests/golden/`, holding round-trip
  values, edge cases and expected errors. A meta-test asserts that every `type_table()` row
  has decode and encode goldens, or an explicit exemption.
- **Conformance.** `tests/conformance/row_types.py` runs the shared §3.4 table through
  `_rowplan` and the checker's resolver.
- **Checker cases.**
  - `tests/check/cases/<rule>.py` files carry inline markers: `# error: [rule]` or
    `# error: [rule1, rule2]` on the line where the diagnostic starts.
  - Caret goldens are `tests/check/expected/*.txt` (text) and `*.json`, compared byte for
    byte.
  - A meta-test asserts that every rule in §9.5 has at least one case.
- **Fidelity.** This runs **only against the test suite's own seeded database**, and is
  the one place rowx tests execute checked queries. Every checked query in the fixtures is
  executed in a rolled-back transaction over the NULL-heavy seed data. The test asserts
  that no NULL appears where `describe` said NOT NULL.
- **Typing.** `tests/typing/cases.py` holds `# reveal: <type>` expectations, plus negative
  cases (`query("…")` and `query(f"…")` must error). It runs `pyright --outputjson` and
  `mypy`, with versions pinned and Python version 3.14.
- **CI.**
  - Linux runs everything, with a `postgres:18` service and `ROWX_REQUIRE_DB=1`.
  - macOS arm64 runs the build, `cargo test --workspace --lib` and `pytest -m "not db"`.
  - Lint gates: `cargo fmt --check`, `cargo clippy --workspace --all-targets -D warnings`,
    `ruff check`, `ruff format --check`, `pyright` (strict for `src/rowx`) and `mypy`.
  - The benchmark is a separate, non-blocking job.

---

## 11. Dependency evaluation

| Candidate | Use | Verdict |
|---|---|---|
| **sqlx** as the runtime driver | Connections, codecs | **No.** Parameters are binary-only, so every Python type and Postgres type pair needs a hand-written encoder, and `int`→`numeric` and `str`→`date` failed. Its `&mut` connection serialises everything. It has no public cancel API: a cancelled `pg_sleep(3)` kept running. It pulls in 180 crates and builds in 19 s, against 90 crates and 13 s. |
| **sqlx** `describe` | Nullability | **No.** It is `#[doc(hidden)]`, `offline`-gated and only reachable through `macros`, and its signature changed in 0.9. It gives unsafe answers (§8.4). rowx copies its *model* and ports its test cases with attribution; sqlx is MIT/Apache-2.0. |
| **tokio-postgres** | Runtime and describe | **Yes.** It has a public describe API (`Statement::params/columns`, `Column::table_oid/column_id`), raw `FromSql`, a per-parameter `encode_format`, `Kind` resolution and cancel tokens, and it can pipeline later. psqlpy shows the stack works in production. |
| **deadpool-postgres** | Pool | Deferred along with the pool. |
| **datafusion-sqlparser-rs** | Client-side parsing | **Not in v1.** The server already provides validity, types, parameter inference and outer-join nullability, and t-strings give placeholder positions. A parser would add dialect gaps, so queries that Postgres accepts would need a fallback path. **Revisit for:** (a) NOT NULL narrowing of computed columns (`coalesce`, `count(*)`, `WHERE x IS NOT NULL`), building on sqlty's `nullability.rs` (MIT); (b) detecting the statement kind before connecting; (c) finer positional mapping across scan boundaries (§8.2.1). |
| **sqlty** | Reference | MIT, borrowed with attribution: `_params.py` rules, `_config.py` ergonomics and the fixture style. Not a dependency. |
| **ruff_python_parser** | A checker in Rust | **No.** Its API is unstable, and stdlib `ast`/`tokenize` are exact for 3.14. |

---

## 12. Security and safety

- Values are always bound as server-side parameters, never interpolated into SQL text.
  - `query` rejects every dynamic construct.
  - In `query_unchecked`, nested templates also bind their values, and `:ident` is quoted
    and length-checked.
  - There is no raw-SQL escape.
- The checker's guarantees and limits are in §8.1, including plan-time evaluation of
  functions and locking. The docs recommend a dedicated role on a CI or disposable
  database.
- The checker's EXPLAIN runs over the simple protocol only after Parse has accepted the
  SQL as a single statement (§8.2, step 1). This invariant is tested.
- Neither diagnostics nor exceptions may include a DSN password. Parameter values are
  redacted from Rust debug logs (§7.3).
- TLS defaults to `prefer`, which is encrypted but not verified, as in libpq. The docs
  recommend `verify-full`.

---

## 13. Milestones (the detailed tasks are in `docs/tasks.md`)

| Phase | Outcome | Tasks |
|---|---|---|
| **P0 Foundation** | The workspace builds; DB test infra, CI, type-table metadata and the `_core` contract exist | T00–T05 |
| **P1 Vertical slice** | `await query[int](t"SELECT 1").fetch_one(conn) == 1` end to end | T06–T11 |
| **P2a Codecs** | Every §6 row round-trips; params encode per §6/§7.3 | T12–T19 |
| **P2b Driver** | Statement cache, state machine, cancellation, TLS, benchmark, e2e and typing suites | T20–T27 |
| **P2c Describe** | §8 complete, with the regression and known-limits suites | T28–T34 |
| **P2d Checker** | §9 complete: DB-free front-end first, then CLI integration and the rule suites | T35–T44 |
| **P3 Release** | Docs, a CI-checked example app, wheels | T45–T47 |

The P2 lanes run in parallel once P1 is done. The checker's DB-free tasks can start right
after P0.

---

## 14. Deferred work (by expected value)

1. **Named row types** (dataclass, NamedTuple, TypedDict, Pydantic, Protocol), matched by
   column name.
2. A **transaction helper** (`async with conn.transaction()`), then opt-in **pipelining**.
3. **Pool**, using deadpool-postgres.
4. **Offline cache** (`rowx prepare`, `.rowx/query-<sha256>.json`, `--check` drift).
5. `fetch_optional`, and streaming `fetch` as an async iterator.
6. `--fix`, warnings and ignore comments.
7. **Type overrides** (`"col: T"`) and custom codecs (domains, composites, ranges, `inet`,
   `money`).
8. Parser-based nullability narrowing (§11), and a cross-version `breaking --against`
   check.
9. LISTEN/NOTIFY, COPY, client-cert TLS, `PG*` env vars and `.pgpass`, and a free-threaded
   build.
10. Other backends.

---

## 15. Decision log

### 15.1 Design interview (2026-10-23)

| # | Topic | Decision |
|---|---|---|
| 1 | Backends | PostgreSQL only; 18 only |
| 2 | Runtime | An owned driver on a Rust library, which is tokio-postgres |
| 3 | Sync/async | Async only |
| 4 | Types | An explicit `query[T]` verified by the checker; no codegen, no plugin |
| 5 | Nullability | SQLx's model with rowx's own corrected walker; unknown counts as nullable; `!`/`?` overrides |
| 6 | Param checking | Static, best effort, sqlty-style |
| 7 | Dynamic SQL | Static only in `query`; `query_unchecked` is the escape hatch |
| 8 | Row types | Tuples and scalars only |
| 9 | Checker split | Python front-end with the Rust core doing DB work |
| 10 | Offline | None; a live DB is required |
| 11 | Diagnostics | Errors only, no fix |
| 12 | Methods | `fetch_one` (raises), `fetch_all`, `execute` |
| 13 | No T | `T = None`; only `execute` |
| 14 | Runtime scope | A single connection only |
| 15 | Migrations | Out of scope |
| 16 | Type map | asyncpg-like |
| 17 | Strictness | Exact match |
| 18 | Uncheckable queries | An error unless written as `query_unchecked` |
| 19 | T sources | Inline, or project aliases |
| 20 | Runtime validation | Arity plus NULL checks |
| 21 | sqlparser-rs | Not in v1 |
| 22 | Concurrency | One operation per connection; raise `ConnectionBusyError` |
| 23 | TLS | rustls |
| 24 | Packaging | One wheel and one CLI |
| 25 | Doc shape | Spec plus milestones and tasks |

### 15.2 Review-driven decisions (revision 2)

| # | Decision | Why |
|---|---|---|
| R1 | `rowx-pg` is pure Rust; all PyObject work is in `rowx-py` | Keeps `cargo test` real and the boundary clean |
| R2 | Cancellation uses a Settling state, rowx's own CancelRequest read to EOF, and draining | Stale cancels killed 55% of follow-up queries |
| R3 | Base nullability checks `convalidated` and `relkind` | PG18 `NOT VALID` NOT NULL, foreign tables |
| R4 | The walker covers scan boundaries, grouping sets, RETURNING old/new, and every matching position | Five unsafe NOT NULL answers were measured |
| R5 | DSN pre-parse with custom rustls verifiers and the ring provider | tokio-postgres rejects `verify-*` and `sslrootcert` |
| R6 | Codec dispatch goes through tokio-postgres `Type`/`Kind` | Domains and arrays carry domain OIDs in params and elements |
| R7 | The encoder and checker share `accepted_params`; `text` accepts only `str`; no `str()` fallback; `str`→`date` is rejected | Otherwise parameter checking would be silently off for text |
| R8 | Retry-once is refined (routine match, 25P02 and 26000 handling) | Retries inside transactions fail |
| R9 | EXPLAIN runs over `simple_query`, only after Parse | The extended protocol demands values for `$n` |
| R10 | The safety wording admits plan-time function evaluation and adds `lock_timeout` | Accuracy |
| R11 | `Any`/`object` are nullability-neutral in both the checker and the runtime | The checker and runtime contradicted each other |
| R12 | `void` columns are treated as 0 columns, or match the tuple element `None` | `SELECT pg_sleep()` could not be satisfied |
| R13 | New rules: `param-count-mismatch`, `indirect-query`, `syntax-error`, `invalid-override` | Silent holes in "uncheckable queries are an error" |
| R14 | A normative source-map algorithm (`tokenize` + verification) | `ast` offsets are bytes and include escapes and seams |
| R15 | Compile eagerly with `TypeError`; fetch-time row-plan errors raise `InterfaceError` | Removes the class contradiction |
| R16 | One row-type grammar with a conformance test | Prevents the runtime and checker drifting apart |
| R17 | The adjacency rule inserts spaces around `$n` | `x{y}` would become the identifier `x$1` |
| R18 | Arity is checked against the statement before executing | Otherwise an empty result hides a shape mismatch |
| R19 | No `NoReturn` overloads | `query[Any]` would be typed `NoReturn` |
| R20 | macOS CI runs without the DB | Docker isn't available on macOS CI runners |

---

## 16. Change process

`docs/design.md` is the contract.

- A feature PR **must not** change it silently.
- For a spec gap that blocks a task, add an ADR-lite entry to `docs/decisions.md`, giving
  the date, task id, question, decision and why, and reference it in the PR. The design doc
  is then updated in a follow-up docs PR.
- Changes to public behaviour need a design-doc PR first.
