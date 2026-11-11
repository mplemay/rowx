# API

```python
import asyncio
from dataclasses import dataclass

from rowx import query
from rowx_postgres import Pool, connect

type UserRow = tuple[int, str, str | None]


@dataclass(frozen=True, slots=True, kw_only=True)
class User:
    id: int
    name: str
    email: str | None = None


async def main() -> None:
    async with await connect("postgresql://app@localhost/app") as conn:
        uid = 42
        user = await query[UserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").one(conn)
        maybe = await query[UserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").optional(conn)
        named = await query[User](t"SELECT name, id FROM users WHERE id = {uid}").one(conn)
        async for name in query[str](t"SELECT name FROM users ORDER BY name").fetch(conn):
            ...
        result = await query(t"UPDATE users SET name = {'x'} WHERE id = {uid}").execute(conn)
        print(result.rows_affected)

    async with Pool("postgresql://app@localhost/app", max_size=20) as pool:
        await query[UserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").one(pool)
        async with pool.transaction() as tx:
            await query(t"UPDATE users SET name = {'x'} WHERE id = {uid}").execute(tx)


asyncio.run(main())
```

The block is an illustration. It is not a runnable snippet.

## Public surface

Three import roots. `from rowx import query` and `from rowx_core import query` are the same object. `import rowx`
loads no database backend.

`rowx` and `rowx_core` both export `query`, `query_unchecked`, `QueryResult`, `RowStream`, the executor protocols
`Executor`, `Connection`, `Pool` and `Transaction`, and the errors below. The protocols describe what a query can
run on. They are not connections. Open a PostgreSQL connection with `rowx_postgres`:

| Module | Exports |
|---|---|
| `rowx_core`, re-exported by `rowx` | `query`, `query_unchecked`, `QueryResult`, `RowStream`, `Executor`, `Connection`, `Pool`, `Transaction`, and the errors |
| `rowx_postgres` | `connect`, `ConnectOptions`, `Connection`, `Pool`, `Transaction`, `RESET_QUERY`, `Range`, `Interval`, `Money`, `Cube`, `Point`, `Line`, `LineSegment`, `Box`, `Path`, `Polygon`, `Circle`, `Migrator`, `Migration`, `MigrationStatus`, `MigrationResult` |
| `rowx.errors` | the same error classes, defined in `rowx_core.errors` |
| `rowx.testing` | opt-in pytest plugin ([Test plugin](../contributing/testing.md#test-plugin)) |

`rowx_core` also exports the template and row-plan types (`Compiled`, `Compiler`, `TemplatePlan`, `Parameter`,
`Identifier`, `capture_template`, `FieldPlan`, `ShapeSpec`, `ValueClass`) for a backend implementer. Application
code uses `query` and `rowx_postgres`.

`query` and `query_unchecked` are siblings. Neither subclasses the other, and user code must not subclass either.
The checker reports `indirect-query` if it does. `query` and `query_unchecked` set no `__slots__`, so each instance
has a `__dict__`, which keeps `__orig_class__` and therefore `T`.

Methods:

| Method | Result | Behaviour |
|---|---|---|
| `one` | `T` | The first row, or `RowNotFound` when there are none. Extra rows are not an error. After the first row the rest are drained and discarded. DML side effects still run. Use `LIMIT 1` for a large result. |
| `optional` | `T \| None` | The first row, or `None` when there are none. Extra rows drain as for `one`. |
| `all` | `list[T]` | Every row. Use `fetch` when the size is unbounded. |
| `fetch` | `RowStream[T]` | Streams rows. Does no IO itself. |
| `execute` | `QueryResult` | `rows_affected` from `CommandComplete`, or 0. Allowed on `query[T]`: the row plan is not built and the rows are discarded. |

Every method except `execute` needs a row type. On `query[None]` they raise
`InterfaceError("query without a row type; use .execute() or add query[T]")`. `fetch` raises that from the call, not
from the first iteration. `QueryResult` has no command tag. The tag is
[#300](https://github.com/mplemay/rowx/issues/300).

For `query[int | None]`, `optional` returning `None` means either no row or a NULL value. Use
`query[tuple[int | None]]` when the difference matters. The checker does not report it.

The shape check (arity, and for a named row the column-to-field mapping) happens before execution, against the
prepared statement. A mismatch is reported even when the result is empty.

Each call compiles SQL with **that** executor's compiler. Capturing the t-string (rejects, identifiers, nested
templates) happens in the constructor and does not depend on a database. Emitting `$n`, adjacency spaces and the
parameter cap happens when the method runs. The same compiler emits the SQL and the source-map padding the checker
uses. A query is not permanently bound to the first executor.

`one`, `optional`, `all` and `execute` are `async def`. `fetch` is a plain `def`. `RowStream.__anext__`, `aclose`,
`__aenter__` and `__aexit__` are `async def`.

### `rowx_postgres`

```python
from rowx_postgres import RESET_QUERY, ConnectOptions, Connection, Pool, Transaction, connect
```

`RESET_QUERY` is
`RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;`.

`ConnectOptions` is a frozen, slotted, keyword-only dataclass. `password` is omitted from `repr`. `extra` values are
shown as `'<redacted>'`. `from_dsn` uses the Rust DSN parser. `connect` and `Pool` accept `str | ConnectOptions`.
`channel_binding` is `Literal["disable", "prefer", "require"] | None`. An `extra` key of that name, an invalid value,
or `require` with `sslmode=disable` is an `InterfaceError`. `server_settings` travel in the startup `options` as
`-c` pairs and survive `RESET ALL`. Every other DSN key the parser keeps goes in `extra` and is passed through. A
DSN with `options=-c search_path=…` **MUST** round-trip. Keys and values are validated
([Option validation](driver.md#option-validation)). Redaction is in [Security](security.md#connect-options).

`Connection` is an executor. `close` on a pooled connection releases it. `ping` is an empty simple query. `reset`
runs `RESET_QUERY`. `transaction` starts a transaction. `detach` removes a pooled connection from the pool. One
operation at a time; a second raises `ConnectionBusyError`.

`Pool` does no IO in the constructor. `async with` calls `open` and `close`. Semantics are in
[Pool](driver.md#pool). Waiting past `acquire_timeout` raises `PoolTimeoutError`. A closed pool raises
`InterfaceError`. Pool operations never raise `ConnectionBusyError`.

`Transaction` is an executor. `transaction()` on it is a savepoint and takes no isolation arguments. Semantics are
in [Transactions](driver.md#transactions).

`Migrator` is constructed synchronously and validates the files immediately, with no database IO. Its methods take a
`Connection` or a `Pool`. Semantics are in [Migrations](migrations.md).

## Streaming

`fetch(executor)` returns a single-use `RowStream`. It is an async iterator and an async context manager.

- **Claiming.** `fetch` does no IO and does not claim the connection. The first `__anext__` claims it. On a
  connection or transaction that claim is synchronous, so `ConnectionBusyError`, a shape `RowShapeError` and
  `EncodeError` are raised from the first `__anext__`. On a pool the claim is an acquire.
- **Busy while open.** From the first `__anext__` until the stream ends, the connection is busy. A loop body cannot
  run another query on that connection.
- **`batch_size`** defaults to 1024. Below 1 is `ValueError`. It caps how many rows cross into Python per trip. It
  does not change the server's row limit. The first row of a slow query is not held for a full batch.
- **`result`** is `None` until the stream is exhausted, then `QueryResult`. It stays `None` after an early exit, an
  error or a cancel.
- **Early exit drains.** `aclose()`, leaving `async with`, or collecting an unexhausted stream drops the row stream.
  No `CancelRequest` is sent, so DML side effects run to completion. The connection is idle before `aclose()`
  returns. After `break`, a stream still referenced stays open until it is collected, which is why `async with` is
  the way to release it. Bound a large read with `LIMIT`, or cancel the task, when stopping early must be fast.
- **Cancellation** of the task awaiting `__anext__` cancels the statement
  ([Cancellation](driver.md#cancellation)). The stream is then over: the next `__anext__` raises `StopAsyncIteration`.
  A cancel that lands in the loop body is an ordinary early exit, so the stream drains.
- A `RowShapeError`, `DecodeError`, `RowConstructError` or `DatabaseError` partway through is raised from
  `__anext__`. Rows already yielded stay yielded. The connection returns to idle, or to broken on an IO error.
- Schema-change retry applies only before the first row is delivered.
- `close()` on the connection while a stream is open cancels and settles. The next `__anext__` raises
  `InterfaceError`.
- On a pool, the connection is held until the stream ends, then released. A stream created inside
  `pool.acquire()` and iterated after the block raises `InterfaceError`. Early exit drains before the release reset.

A stalled consumer keeps the server backend inside the statement, holding its snapshot and locks.

## Row-type grammar

The runtime row plan (`rowx_core`) and the checker (`src/rowx/check/resolve.py`) implement this grammar. Conformance
rows live in `packages/rowx-postgres/src/rowx_postgres/__tests__/conformance/row_types.py`.

```text
ROW   := NONE | tuple[ELEM, ...] | ELEM | NAMED
NONE  := None
NAMED := dataclass | NamedTuple | TypedDict | pydantic BaseModel | pydantic dataclass
ELEM  := LEAF
       | "LEAF | None"
       | Optional[LEAF]
       | Union[LEAF, None]
       | Any
       | object
       | None
LEAF  := int | float | str | bool | bytes
       | decimal.Decimal | datetime.date | datetime.time | datetime.datetime
       | datetime.timedelta | uuid.UUID
       | ipaddress.IPv4Interface | ipaddress.IPv6Interface
       | ipaddress.IPv4Network | ipaddress.IPv6Network
       | rowx_postgres.Interval | Money | Cube | Point | Line | LineSegment
       | Box | Path | Polygon | Circle
       | rowx_postgres.Range[RLEAF]
       | dict[str, str | None]
       | list[ELEM]
RLEAF := int | decimal.Decimal | datetime.date | datetime.datetime
```

`tuple` needs at least one element. A scalar plan is one column. `None` as `T` is the no-rows plan (`query` and
`query[None]` are the same). `None` as an element matches a `void` column only.

Wrappers unwrap first: `Annotated`, `ReadOnly`, `Required`, `NotRequired`, and a `type` alias, up to 16 hops. Leaf
detection runs before named-type detection, so the `rowx_postgres` value classes stay leaves even though they are
frozen dataclasses.

Rejected forms are `InterfaceError` at the first row-returning call and `invalid-row-type` in the checker: a string
or `ForwardRef`; an unbound `TypeVar`; a bare `tuple`, `tuple[()]` or `tuple[X, ...]`; a union with more than one
non-`None` member other than the two network pairs; `Literal`; any `dict` other than `dict[str, str | None]`; and
the named-row rejections below.

`"LEAF | None"` is a Python union, one alternative, as are `Optional[LEAF]` and `Union[LEAF, None]`. A bare `None`
element is the checker leaf `none`: it matches only `void`, and any other column is `type-mismatch`. `_elem` returns
true for `None`, so that element's `nullable_mask` is true. An element is also nullable when it is a union with `None`,
`Any`, or `object`. `T` is read lazily from `__orig_class__` at the first row-returning call. The plan is cached per
`T`.

A new leaf needs a grammar line, a row-plan rule, a checker rule and conformance rows. A new named form needs the
same four.

### Named row types

| Form | Matching | Construction | Required fields |
|---|---|---|---|
| `@dataclass` | by name | keyword arguments | no `default` or `default_factory` |
| `typing.NamedTuple` | by position; names must match | positional arguments | not in `_field_defaults` |
| `TypedDict` (`typing` or `typing_extensions`) | by key | the row is the dict | `__required_keys__` |
| pydantic `BaseModel` or pydantic dataclass | by name | the validator, or `cls(**row)` for a pydantic dataclass | `FieldInfo.is_required()` |

Dataclasses may use `slots`, `frozen`, `kw_only` and `KW_ONLY`. NamedTuple and TypedDict work as classes and as
calls. `typing_extensions.TypedDict` is detected through `sys.modules`. TypedDict keywords rowx knows are `total`,
`closed` and `extra_items`. `closed` and `extra_items` do not relax `unexpected-column`.

rowx never imports pydantic. A pydantic class is detected through `sys.modules`. A pydantic dataclass is recognised
before the stdlib dataclass rule. Pydantic 2.11 or later is required for the features the checker reads. It is a dev
dependency of the test suite, not a dependency of `rowx` or `rowx-postgres`.

Fields come from the bases in the library's own order. A redeclared field replaces the base field in place. A
project base that is not a row type contributes no fields. A NamedTuple's fields are only `_fields` of the class
that directly subclasses `NamedTuple`. The checker follows the C3 method resolution order over project classes. An
inconsistent MRO or a cycle is `unresolved-row-type`. A private name `__x` is keyed by its mangled name.

These are **not** fields: `ClassVar`, `KW_ONLY`, `field(init=False)`, pydantic private attributes, `computed_field`,
and `Field(init=False)` on a pydantic dataclass (`Field(init=False)` on a `BaseModel` is an ordinary field). A field
typed `None` matches a `void` column. A field is optional when it has a default, `NotRequired`, or `total=False`.

The column name used for matching is the server name after one trailing `!` or `?` is stripped. `?column?` is
exempt. `id!!` becomes `id!`. Matching is case-sensitive. Describe and the mapping both call `column_name`. The
checker passes raw names through `rowx_postgres._core.map_columns`.

**By name** (dataclass, TypedDict, pydantic). Column order does not matter.

- every column needs a field, or `unexpected-column`, including `extra="allow"` and `extra_items`;
- every field without a default needs a column, or `missing-column`;
- two columns with the same stripped name, or two accepted names of one pydantic field, are `duplicate-column`;
- one column that matches two or more fields is `ambiguous-column` (pydantic only).

**By position** (NamedTuple). The column count must lie between the required-field count and the total, or it is
`arity-mismatch` and no name rules run. A stripped name that differs from the field is `column-name-mismatch`.

Every violation is reported, not only the first. Column-indexed issues come first, then `missing-column` by field
order. The checker's text and the runtime `RowShapeError` text are the same for the mapping messages below, in the
`column N "name"` style (columns are 1-based). `RowShapeError` reads ``{type_name}: {msg}; {msg}``.

| Issue | Message |
|---|---|
| `unexpected-column` | ``column 3 "age" matches no field of `User` `` |
| `missing-column` | ``field "name" of `User` has no column`` |
| `duplicate-column` | ``columns 1 and 3 are both named "id"`` |
| `ambiguous-column` | ``column 2 "y" matches fields "x" and "y" of `M` `` |
| `column-name-mismatch` | ``column 2 "login" faces field "email" of `UserRow` (position 2)`` |

A `?column?` duplicate reads ``columns 1 and 3 are both unnamed (`?column?`)``. Two names of one pydantic field read
``columns 1 "id" and 2 "user_id" both map to field "id" of `M` ``. A NamedTuple arity mismatch reads
``the query returns 4 columns but `UserRow` takes between 2 and 3`` (`takes exactly N` when the bounds are equal).

`type-mismatch` and both `nullability-mismatch` strings are checker-only. A type mismatch reads
``column 2 "age" is `int` but field "age" of `User` is `str` ``. Nullability reads
``column 3 "email" may be NULL but field "email" of `User` is `str` `` or
``column 3 "email" is NOT NULL but field "email" of `User` is `str \| None` ``.
The NOT NULL form is checker-only. The runtime NULL check does not raise for a NOT NULL column annotated as a union
with `None`. A NULL in a non-nullable field uses the runtime text
``NULL in column N "..." for non-nullable field ...`` under [Runtime validation](#runtime-validation).

**Pydantic.** A `BaseModel` is built with `__pydantic_validator__.validate_python(row_dict)`. A pydantic dataclass is
called as `cls(**row_dict)`, because `validate_python(dict)` raises `dataclass_exact_type` under `strict=True`. The
dict is keyed by column name and includes pass-through columns. A failure is a `ValidationError` wrapped in
`RowConstructError`. An incomplete model is rebuilt while the plan is built; still incomplete is `InterfaceError`.
The plan caches the validator, so a later `model_rebuild(force=True)` is not seen.

A field's names are its `str` `validation_alias`, else its `str` `alias`, else its name. `validate_by_name` or
`populate_by_name` adds the name. `validate_by_alias=False` keeps only the name. Config precedence follows pydantic:
an explicit `validate_by_name` wins, else `populate_by_name`, else `validate_by_alias=False`. The legacy
`class Config: allow_population_by_field_name` is ignored. Literal class keywords on a model are config. A pydantic
dataclass's config does not merge through bases.

**Unknown names** are a deliberate exception to "uncheckable is an error". `AliasChoices`, `AliasPath` or a
non-literal alias makes that field's names unknown. A before or wrap `model_validator`, an `alias_generator`, or a
`model_config` the checker cannot read makes every field unknown, in the checker. If any field is unknown, the four
by-name rules are skipped for the whole model. The note `names not checked` is attached only to a diagnostic the
model emits. Type and nullability checks still run for columns that map to a field with known names. At runtime
every column goes into the dict, and the NULL check covers only known names. A pass-through column uses the default
decoder.

The checker stays exact. A `numeric` column into an `int` field is `type-mismatch`. There is no pydantic type table
([#297](https://github.com/mplemay/rowx/issues/297)). `EmailStr`, `AnyUrl` and `SecretStr` are `invalid-row-type` on
both sides. Annotate the column's Python type and validate with `Annotated[str, AfterValidator(...)]`.

**Annotations.** At runtime, `get_type_hints(cls, include_extras=True)`. An unresolved name is `InterfaceError`. In
the checker, annotations come from the class body. A string annotation in a class, and every annotation under
`from __future__ import annotations`, is parsed. A string as `T` itself is `invalid-row-type`. A name that exists
only under `TYPE_CHECKING` is `invalid-row-type` in a field and as a row-level alias, with help
`import X at runtime`.

**Generics.** Generic classes and generic aliases are supported once subscripted. Substitution walks
`__type_params__` and never `__value__[args]`. The argument count is checked. Unbound parameters, `TypeVarTuple`,
`ParamSpec`, a wrong count, or more than 16 hops are `invalid-row-type`. `TypeVar` bounds are not enforced by the
checker; the leaf check is. A pydantic dataclass `Box[int]` is built by calling the origin class, so pydantic
validates the unparametrized `T` ([Known limits](../known-limits.md)).

**Rejected on both sides:** `Protocol`, `collections.namedtuple`, attrs, msgspec, `RootModel`, pydantic v1, a stdlib
dataclass with `init=False`, an `InitVar` (including on a pydantic dataclass, whose decorator accepts `init=False`),
a named row with no fields, a nullable named row (`User | None`; use `.optional()`), a named type nested below the
row, and an unsupported generic parameter.

**Checker-only rejections** (`invalid-row-type` in the checker, accepted at runtime, failure surfaces as
`RowConstructError`): a class decorator other than the dataclass decorators and `typing.final`; `metaclass=` or
another unknown class keyword; a hand-written `__init__` on a stdlib dataclass; a constraint alias such as
`PositiveInt` or `constr()`; `Json[...]`; mixed dataclass and pydantic forms; an undecorated subclass of a pydantic
dataclass; an external stdlib base such as `abc.ABC` or `Exception`; a field declared in a conditional block; a
plain alias over a `TypeVarTuple`.

### Runtime validation

- **Arity and mapping** run before execution. A mismatch is `RowShapeError`, including on an empty result. A cached
  statement that fails the check is re-prepared once.
- **NULL check.** A NULL in a non-nullable position is `RowShapeError`. For a named row the message names the field:
  ``{type_name}: NULL in column 2 "email" for non-nullable field "email" of `User` (row {k})``, with `k` 0-based. In
  `all` and `one` the NULL checks run before any constructor. In a stream, earlier rows are yielded first.
- **Construction.** An exception from the constructor or validator is `RowConstructError`, a `DecodeError`, with
  `type_name`, `row_index` and the original as `__cause__`. It wraps `Exception` only. `CancelledError`,
  `KeyboardInterrupt` and `SystemExit` pass through.
- **Types are not checked at runtime**, except by pydantic. `query_unchecked[T]` gets the same runtime checks.
- Element types and nullability inside `list[...]` are not checked at runtime. A NULL array element decodes to
  `None`.

## t-string compilation

The constructor captures the template. That step rejects a non-`Template`, a NUL in static text, a conversion, and
an unsupported format spec, before any IO. PostgreSQL compilation, on the call, emits static text verbatim and the
n-th interpolation as `$n` counting from 1. The same expression twice is two parameters.

**Adjacency.** A single space is inserted before `$n` when the previous character is an identifier character
(`[A-Za-z0-9_$]` or non-ASCII), and after `$n` when the next character is one. Without it, `x{y}` would become the
identifier `x$1`.

**`query`** rejects a nested `Template`, any conversion (including `{x=}`), and any format spec.

**`query_unchecked`** flattens nested templates and renumbers parameters. `{x:ident}` emits a quoted identifier.
Any other conversion or format spec is `TypeError`. There is no raw-SQL spec.

Writing guidance:

- IN-lists are `= ANY({seq})`, with `seq` a list bound as an array.
- Literal braces are `{{` and `}}`.
- Do not interpolate inside a string literal or comment. `LIKE '%{q}%'` puts `$1` inside the literal. Bind the whole
  pattern. The checker reports `param-count-mismatch`. At runtime the counts disagree and raise `InterfaceError`.
- Cast a parameter the server cannot infer: `{x}::int`. Otherwise Postgres fails with 42P18 or 42P08.
- Do not write `$n` by hand.

### `:ident`

`{x:ident}` is `query_unchecked` only. `x` is a non-empty `str`, or a non-empty `tuple` of them joined with `.`.

- Any `str` instance is accepted, so a `StrEnum` allowlist works. The first step is `str.encode`, strict UTF-8. A
  lone surrogate is `TypeError`. Later checks and quoting run on those bytes. Tuples are iterated with
  `tuple.__iter__`.
- Rejected with `TypeError`: a non-`str` part, an empty name or part, a part longer than 63 UTF-8 bytes, and a C0 or
  C1 control character.
- The compiler tracks quotes, dollar quotes, `--` comments and nested block comments across flattened templates. An
  `:ident` anywhere but plain code is `TypeError("an ident interpolation must not be inside a string literal or
  comment")`.
- `conversion` and `format_spec` are examined only after `type(x) is str`. A `str` subclass is never compared.
- `{x:{spec}}` that evaluates to `"ident"` takes the same path. The checker reports a non-literal format spec in a
  `query_unchecked` interpolation as `dynamic-query` ([Call shape](checker.md#call-shape)).
- Quoting makes a name safe as an identifier, not harmless as a choice. Allowlist a name that comes from a user.

### Compile limits

`TypeError` before any IO. The connection stays usable.

- A NUL in the SQL text: `TypeError("SQL text must not contain a NUL character")`. Static text can hold one, so
  `query` raises it too. The capture step raises this.
- More than 65535 parameters after nested templates are flattened:
  `TypeError("a query takes at most 65535 parameters, got N")`. The PostgreSQL compiler raises this when the method
  runs. `N` is the count at the parameter that crosses the limit. A list bound with `= ANY({ids})` is one parameter.

## Nullability overrides

A result column whose name ends in `!` is forced NOT NULL, and one ending in `?` is forced nullable:
`count(*) AS "n!"`.

- The checker strips the suffix ([Overrides](nullability.md#overrides)).
- Tuple and scalar plans are positional, so the runtime ignores the name, but a NULL in a `!` column still raises
  `RowShapeError`.
- A named row strips one trailing `!` or `?` before matching. `"id!"` maps to the field `id`. The suffix does not
  weaken the NULL check.
- The described name is longer than 59 bytes and at most 63. The quoted source alias is longer than 63 bytes, ends
  in `!` or `?`, and starts with the described name. That is `invalid-override`, because Postgres truncated the
  suffix. An alias of exactly 63 bytes that still ends in the suffix is not reported. A described name of 59 bytes
  or shorter is not reported.
- Type overrides (`"col: T"`) are not supported ([#302](https://github.com/mplemay/rowx/issues/302)).

## Errors

Defined in `rowx_core.errors` and re-exported from `rowx` and `rowx.errors`.

```text
Error
├── InterfaceError
│   ├── ConnectionBusyError
│   └── PoolTimeoutError
├── DatabaseError           # .sqlstate .message .detail .hint .position
├── RowNotFound
├── RowShapeError
├── DecodeError
│   └── RowConstructError   # .type_name .row_index
├── EncodeError
└── MigrateError
    ├── MigrationSourceError       # .path .version
    │   └── IrreversibleMigrationError
    ├── MigrationChecksumError     # .version .local_checksum .applied_checksum
    ├── MigrationMissingError      # .versions
    ├── MigrationOrderError        # .version .latest
    ├── DirtyMigrationError        # .version
    └── MigrationFailedError       # .version .path .dirty .applied .error
```

Construction-time misuse of a template is `TypeError`. A bad row type at the call is `InterfaceError`.
`DatabaseError.sqlstate` is a 5-character `str`. `.position` is a 1-based character index, or `None`.
`MigrationFailedError.__cause__` is the `DatabaseError`. A failure in lock or history-table SQL is that
`DatabaseError` unwrapped. An invalid `table`, schema name or revert target is `ValueError`. A `Transaction`
argument to `Migrator` is `InterfaceError`. No error message or `repr` may include the DSN password. Sqlstate
subclasses are not provided.
