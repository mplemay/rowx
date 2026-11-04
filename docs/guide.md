# rowx user guide

rowx runs raw SQL written as t-strings and returns typed rows. The same row types drive `rowx check`, a static checker
that prepares every query against a live Postgres 18 database and reports mismatches.

The `python test` blocks on this page are executed against a seeded database (users `alice`, `bob` and `carol`, plus
`orders`) by the test suite, with `DSN` set to its connection string.

## Quickstart

```python test
import asyncio

from rowx.postgres import connect
from rowx import query

type UserRow = tuple[int, str, str | None]


async def main() -> None:
    async with await connect(DSN) as conn:
        uid = await query[int](t"SELECT id FROM users WHERE name = {'alice'}").one(conn)
        user = await query[UserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").one(conn)
        assert user == (uid, "alice", "alice@example.com")


asyncio.run(main())
```

| Method | Result | Behaviour |
|---|---|---|
| `one` | `T` | The first row. Raises `RowNotFound` if there are none. Extra rows are not an error. |
| `optional` | `T \| None` | The first row, or `None` if there are none. Extra rows are not an error. |
| `all` | `list[T]` | Every row. |
| `fetch` | `RowStream[T]` | Streams the rows. See [Streaming](#streaming). |
| `execute` | `QueryResult` | `rows_affected` is the count of rows affected. Allowed on `query[T]`; the rows are discarded. |

`one` and `optional` discard the rest of the result, but DML side effects (`INSERT ... RETURNING`) always run to
completion. Add `LIMIT 1` to large selects: the server still sends the remaining rows, and the connection reads and
discards them before the next operation runs.

`optional` on a nullable scalar is ambiguous. For `query[int | None]`, `None` means either "no row" or a NULL value.
Use a tuple row type (`query[tuple[int | None]]`) when the difference matters. The checker does not report this.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query


async def main() -> None:
    async with await connect(DSN) as conn:
        no_row = await query[int | None](t"SELECT 1 WHERE false").optional(conn)
        null_value = await query[int | None](t"SELECT NULL::int").optional(conn)
        assert no_row is None
        assert null_value is None

        assert await query[tuple[int | None]](t"SELECT 1 WHERE false").optional(conn) is None
        assert await query[tuple[int | None]](t"SELECT NULL::int").optional(conn) == (None,)


asyncio.run(main())
```

## Streaming

`fetch` returns a single-use `RowStream[T]` for results without a known upper bound. It is an async iterator, and an
async context manager for callers that don't want release to depend on garbage collection. The rows cross into Python
`batch_size` at a time (default 1024).

Streaming bounds memory, not time. Each row is a separate `async for` step, so iterating 100,000 rows takes about 1.4
times as long as `all()` (about 72 ms against 52 ms on a release build, for `int4`, `text` and `timestamptz` columns).
Use `all()` when the result is known to be small, and `fetch` when it is large or unbounded.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query


async def main() -> None:
    async with await connect(DSN) as conn:
        users = query[tuple[int, str]](t"SELECT id, name FROM users ORDER BY id")

        names = [name async for _, name in users.fetch(conn)]
        assert names == ["alice", "bob", "carol"]

        async with users.fetch(conn, batch_size=2) as rows:
            assert rows.result is None
            seen = [row async for row in rows]
            assert len(seen) == 3
            # set once the stream is exhausted
            assert rows.result is not None
            assert rows.result.rows_affected == 3


asyncio.run(main())
```

- **No IO until iteration.** `fetch` itself does nothing. The first `async for` step claims the connection, so
  `ConnectionBusyError` and parameter errors are raised there.
- **`.result`** is `None` until the stream is exhausted, then a `QueryResult`. It stays `None` after an early exit,
  an error or a cancellation.
- **The connection is Busy while the stream is open.** From the first row until the stream ends, any other operation on
  that connection raises `ConnectionBusyError`, so a loop body can't query the connection it is streaming from. (A
  result that arrives entirely with the first batch ends the stream at once, so the connection is already idle by the
  first row.)
- **`break` drains.** Stopping early does not cancel the statement: the server finishes it, and the connection
  discards the remaining rows, so DML side effects (`INSERT ... RETURNING`) complete. Prefer `async with`: a stream that
  is still referenced after a `break` stays open until it is collected. Bound large reads with `LIMIT`, or cancel the
  task, when stopping early has to be fast.
- **Cancellation.** Cancelling the task while it waits for a row sends a cancel to the server, as for the other
  methods. A cancelled stream is over: the next step raises `StopAsyncIteration`.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query
from rowx.errors import ConnectionBusyError


async def main() -> None:
    async with await connect(DSN) as conn:
        numbers = query[int](t"SELECT generate_series(1, 100000)")

        async with numbers.fetch(conn, batch_size=1) as rows:
            async for _ in rows:
                try:
                    await query[int](t"SELECT 1").one(conn)
                except ConnectionBusyError:
                    pass
                else:
                    raise AssertionError
                break

        # the stream was closed by `async with`, so the connection is usable again
        assert await query[int](t"SELECT 1").one(conn) == 1


asyncio.run(main())
```

`rowx.postgres.connect(dsn)` returns a `Connection`. Use it as an async context manager, or call `close()`. Pass a
`ConnectOptions` instead of a DSN to set `connect_timeout`, `statement_cache_size`, `server_settings` and more
(`ConnectOptions.from_dsn(dsn, connect_timeout=5)`).

## Row types

`T` in `query[T]` is one of:

- `None` (the default): no rows. Use `.execute()`.
- a scalar, such as `str`: exactly one column.
- `tuple[A, B, ...]`: one element per column, in order.
- a class: a dataclass, `NamedTuple`, `TypedDict`, or a pydantic model or dataclass, one field per column. See
  [Named row types](#named-row-types).

Elements are `int`, `float`, `str`, `bool`, `bytes`, `Decimal`, `date`, `time`, `datetime`, `timedelta`, `UUID`, or
`list[...]` of those for arrays. Add `| None` for nullable columns. `Any` and `object` match any type and either
nullability. `type` aliases and `Annotated[...]` are followed.

```python test
import asyncio
from decimal import Decimal

from rowx.postgres import connect
from rowx import query

type OrderRow = tuple[int, Decimal, str | None]


async def main() -> None:
    async with await connect(DSN) as conn:
        rows = await query[OrderRow](t"SELECT id, amount, note FROM orders ORDER BY id").all(conn)
        assert rows
        total = await query[Decimal](t'SELECT sum(amount) AS "total!" FROM orders').one(conn)
        assert total == sum(amount for _, amount, _ in rows)


asyncio.run(main())
```

At runtime the number of columns must equal the arity of `T` (otherwise `RowShapeError`), and a NULL in a non-nullable
position raises `RowShapeError` naming the column. A named row type is matched by name instead of arity alone.
`rowx check` finds both statically. See [types.md](types.md) for what each Postgres type decodes to.

## Named row types

`T` can also be a class. Each result column is mapped to a field, and each row is built through the class's
constructor. `rowx check` maps the columns to the fields the same way, and checks each field as it checks a tuple
element.

| Form | Columns are matched | A row is built by | A field is optional when |
|---|---|---|---|
| `@dataclass` | by name | calling the class with keyword arguments | it has a `default` or `default_factory` |
| `typing.NamedTuple` | by position, and the names must match | calling the class with positional arguments | it has a default |
| `TypedDict` | by key | making a plain `dict` | it is `NotRequired` (or the class is `total=False`) |
| pydantic `BaseModel` | by name (or alias) | validating a dict, as `Model.model_validate` does | it has a default |
| pydantic dataclass | by name (or alias) | calling the class with keyword arguments, which validates them | it has a default |

Field types are the same elements as in a tuple (`int`, `str`, `Decimal`, `datetime`, `list[...]`, ... with `| None`
for nullable columns), after `Annotated`, `ReadOnly`, `Required` and `NotRequired` are unwrapped.

```python test
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple, TypedDict

from rowx import query
from rowx.postgres import connect


@dataclass(slots=True, frozen=True)
class User:
    id: int
    name: str
    email: str | None = None


class OrderRow(NamedTuple):
    id: int
    amount: Decimal
    note: str | None = None


class Account(TypedDict):
    id: int
    name: str


async def main() -> None:
    async with await connect(DSN) as conn:
        # by name: the order of the columns doesn't matter
        alice = await query[User](t"SELECT name, id, email FROM users WHERE name = {'alice'}").one(conn)
        assert alice.name == "alice"
        assert alice.email == "alice@example.com"

        # a field with a default needs no column
        bob = await query[User](t"SELECT id, name FROM users WHERE name = {'bob'}").one(conn)
        assert bob.email is None

        # by position, and the names must match the fields: an alias fixes a mismatch
        orders = await query[OrderRow](t"SELECT id, amount, note FROM orders ORDER BY id").all(conn)
        assert all(isinstance(order.amount, Decimal) for order in orders)

        # a TypedDict row is a dict
        accounts = await query[Account](t"SELECT id, name FROM users ORDER BY name").all(conn)
        assert [account["name"] for account in accounts] == ["alice", "bob", "carol"]


asyncio.run(main())
```

### Matching columns to fields

A column is matched under the name the server reports, after a trailing `!` or `?` override is stripped (see
[Nullability overrides](#nullability-overrides)), so `AS "id!"` maps to the field `id`. Matching is case-sensitive.
Alias a column with `AS` to give it a field's name. Every violation is collected and reported, not just the first. At
runtime that is a `RowShapeError`, before the statement executes (even for an empty result), and the checker reports
one diagnostic for each:

| Rule | Fires when |
|---|---|
| `unexpected-column` | A column matches no field (by name). |
| `missing-column` | A field without a default has no column (by name). |
| `duplicate-column` | Two columns have the same name (`?column?` included), or two columns map to one pydantic field. |
| `ambiguous-column` | One column matches two pydantic fields. |
| `column-name-mismatch` | A NamedTuple's column and field at the same position have different names. |
| `arity-mismatch` | The column count is outside the NamedTuple's range from required fields to all fields. |
| `type-mismatch`, `nullability-mismatch` | As for a tuple element, with the field named in the message. |

Defaults relax the rules: a field with a default may have no column, and a NamedTuple may omit trailing fields that have
defaults. A column with no field is always an error, even for a pydantic model with `extra="allow"` or a TypedDict with
`extra_items`. A NamedTuple has no `missing-column` or `unexpected-column`, because it is matched by position, and
`SELECT name, id` against `(id, name)` fails on both columns.

Nullability follows the field type, as it does for a tuple. The `!` and `?` overrides work as they do there, and a NULL
in a column whose field isn't nullable raises `RowShapeError` naming the column and the field.

### Which members are fields

A field is an annotated member that the constructor accepts. `ClassVar`, `KW_ONLY`, `dataclasses.field(init=False)`,
pydantic private attributes and `computed_field`s, and `Field(init=False)` on a pydantic dataclass are not fields.
Dataclasses may use `slots`, `frozen`, `kw_only`, `field(kw_only=...)` and `KW_ONLY`. A dataclass's or model's fields
include those it inherits, and a field redeclared in a child replaces the base's. A NamedTuple's fields are those of
the class that directly subclasses `NamedTuple`, so annotations a subclass adds are ignored. NamedTuple and TypedDict
also work in functional form (`NamedTuple("Row", [...])`).

Annotations are resolved at runtime with `typing.get_type_hints`, so a field's type has to be imported at runtime. A
name that is imported only under `TYPE_CHECKING` raises `InterfaceError` at runtime, and the checker reports it as
`invalid-row-type` with the help "import `X` at runtime". Ruff's `TC003` suggests such a move, so import the field's
type outside the `TYPE_CHECKING` block. The same holds for a `type` alias used as `T` that names such an import.

### Pydantic

A `BaseModel` row is validated as `Model.model_validate` would, so validators, constraints, aliases and a custom
`__init__` all apply. A pydantic dataclass is called as `Cls(**row)`, which runs the same validation. rowx never
imports pydantic itself, and it needs pydantic 2.11 or later. The dict is keyed by column name, and a failure is a
`RowConstructError` ([below](#construction-errors)).

A column is matched to a field's `validation_alias`, else its `alias`, else its name. `validate_by_name` or
`populate_by_name` adds the field name, and `validate_by_alias=False` keeps only the name. Class keywords are config:
`class M(BaseModel, populate_by_name=True)` works as it does in pydantic. Config merges the bases left to right (the
last base wins), then the class's own `model_config` or `Config`, then the class keywords. `Field(...)` metadata inside
a PEP 695 `type` alias is ignored, as pydantic ignores it, while a plain alias or a `TypeAlias` alias works.

```python test
import asyncio
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from rowx import query
from rowx.postgres import connect


def lower(value: str) -> str:
    return value.lower()


class Customer(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    id: int
    name: Annotated[str, AfterValidator(lower)]
    contact: str | None = Field(default=None, alias="email")


class Spend(BaseModel):
    user_id: int
    total: Annotated[Decimal, Field(ge=0)]


async def main() -> None:
    async with await connect(DSN) as conn:
        # `email` is the alias of `contact`, so the column is named by it
        carol = await query[Customer](t"SELECT id, upper(name) AS name, email FROM users WHERE name = {'carol'}").one(conn)
        assert carol.name == "carol"
        assert carol.contact is None

        spend = await query[Spend](
            t'SELECT user_id, sum(amount) AS "total!" FROM orders GROUP BY user_id ORDER BY user_id',
        ).all(conn)
        assert all(row.total >= 0 for row in spend)


asyncio.run(main())
```

**The checker is exact.** Pydantic's lax mode would coerce a `numeric` column into an `int` field, a `text` column into
a `UUID`, and so on, but `rowx check` compares the column's Python type with the field's type and reports the difference
as `type-mismatch`. rowx has no table of pydantic's coercions, and a coercion that works on today's data is a bug on
tomorrow's. Cast in SQL (`amount::int`), or declare the type the column really has and convert with a validator.

Some pydantic types are not Postgres types and are rejected as `invalid-row-type`: `EmailStr`, `AnyUrl` and `SecretStr`.
Declare the column's own type and validate it, for example `Annotated[str, AfterValidator(check_email)]`. A pydantic
constraint alias such as `PositiveInt` or `constr(...)` works at runtime, but the checker can't look through it and
rejects it, so write `Annotated[int, Field(gt=0)]`. Likewise `Json[...]` is checker-rejected.

**Unknown names.** An alias that is an `AliasChoices`, an `AliasPath` or not a literal makes that field's names unknown,
and a `model_validator(mode="before")` or `mode="wrap"`, an `alias_generator`, or a `model_config` the checker can't
read makes every field's names unknown. The checker can't map such columns, so it skips the four by-name rules
(`unexpected-column`, `missing-column`, `duplicate-column` and `ambiguous-column`) for that model and still checks types
and nullability for the columns it can map. When it reports any other diagnostic on that call, it adds
`note: names not checked: <reason>`. A model that is otherwise clean prints nothing. At runtime every column goes into
the dict, and the NULL check applies only to columns that match a known name.

### Construction errors

An exception from the constructor or validator, such as a pydantic `ValidationError`, a failing `__post_init__` or a
`TypeError` from a hand-written `__init__`, is raised as `RowConstructError`, a subclass of `DecodeError`. It carries
`type_name` (the class's `__name__`) and `row_index`, the row's 0-based position in the result (counted across stream
batches, and always 0 for `one` and `optional`). The original exception is its `__cause__`. In `fetch`, the rows before
the failing one are yielded first. Only `Exception` is wrapped: cancellation, `KeyboardInterrupt` and `SystemExit` pass
through.

```python test
import asyncio
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ValidationError

from rowx import RowConstructError, query
from rowx.postgres import connect


def not_bob(name: str) -> str:
    if name == "bob":
        msg = "no bobs"
        raise ValueError(msg)
    return name


class Member(BaseModel):
    id: int
    name: Annotated[str, AfterValidator(not_bob)]


async def main() -> None:
    async with await connect(DSN) as conn:
        try:
            await query[Member](t"SELECT id, name FROM users ORDER BY name").all(conn)
        except RowConstructError as error:
            assert (error.type_name, error.row_index) == ("Member", 1)
            assert isinstance(error.__cause__, ValidationError)
        else:
            raise AssertionError


asyncio.run(main())
```

### Generics

Generic classes (`class Box[T]`, `Generic[T]`) and generic aliases (`type Rows[T] = tuple[int, T]`) work once they are
subscripted: `query[Box[str]](...)`. Every type parameter must be bound by an argument or a PEP 696 default. For a
generic class, Python itself raises `TypeError` on a wrong argument count when you subscript it; for a generic alias
both the runtime (`InterfaceError`) and the checker (`invalid-row-type`) reject it. An unbound parameter, a
`TypeVarTuple` or a `ParamSpec` is also `invalid-row-type`, and a chain of more than 16 alias or class substitutions is
too deep. A `TypeVar`'s bound isn't enforced, but the column type is still checked against the substituted field type.
The row is built by the class itself (`type_name` reads `Box[str]` in messages).

```python test
import asyncio
from dataclasses import dataclass

from rowx import query
from rowx.postgres import connect


@dataclass
class Box[T]:
    id: int
    value: T


type Labelled[T] = Box[T]


async def main() -> None:
    async with await connect(DSN) as conn:
        box = await query[Box[str]](t"SELECT id, name AS value FROM users WHERE name = {'alice'}").one(conn)
        assert box.value == "alice"
        label = await query[Labelled[str | None]](t"SELECT id, email AS value FROM users WHERE name = {'bob'}").one(conn)
        assert label.value is None


asyncio.run(main())
```

### What isn't supported

- A row class from attrs or msgspec, a `Protocol`, an untyped `collections.namedtuple`, a `RootModel` and `pydantic.v1`
  models. They are deferred, not rejected for good: see the [roadmap](design/roadmap.md).
- A stdlib dataclass with `init=False`, an `InitVar` field (a pydantic dataclass accepts `init=False`), and a class
  with no fields.
- A nullable row (`User | None`): call `.optional()` instead. A row class nested in a tuple, a list or a union, or used
  as a field type, so there are no JSON-shaped or composite columns yet.
- The checker can't see through a class decorator other than the dataclass ones, a `metaclass=` or an unknown class
  keyword, a hand-written `__init__` on a stdlib dataclass, or a pydantic constraint alias. It rejects them as
  `invalid-row-type`, while the runtime accepts them and reports a failure as `RowConstructError`.
- A row class has to be defined in your project, because the checker never imports code. A row class (or base class)
  from an installed package is not resolved.
- Type overrides (`AS "col: Type"`) are not supported. Nullability overrides (`!` and `?`) are.

## Writing queries

A `query[T]` takes one inline t-string. Conversions (`{x!r}`, `{x=}`), format specs and nested templates are rejected,
so every interpolation is a bound parameter. Use `query_unchecked` for dynamic SQL; it is skipped by the checker.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query, query_unchecked


async def main() -> None:
    async with await connect(DSN) as conn:
        # IN-lists: bind a list as an array with = ANY(...)
        wanted = ["alice", "bob"]
        found = await query[str](t"SELECT name FROM users WHERE name = ANY({wanted}) ORDER BY name").all(conn)
        assert found == wanted

        # literal braces are doubled
        arr = await query[list[int]](t"SELECT '{{1,2,3}}'::int[]").one(conn)
        assert arr == [1, 2, 3]

        # bind the whole pattern, never an interpolation inside a literal
        pattern = "al%"
        like = await query[str](t"SELECT name FROM users WHERE name LIKE {pattern}").all(conn)
        assert like == ["alice"]

        # cast a parameter whose type the server cannot infer
        missing = None
        none_ok = await query[bool](t"SELECT {missing}::int IS NULL").one(conn)
        assert none_ok

        # dynamic identifiers and nested templates are for query_unchecked only
        table, column = "users", "name"
        cond = t"WHERE id > {0}"
        names = await query_unchecked[str](t"SELECT {column:ident} FROM {table:ident} {cond} ORDER BY 1").all(conn)
        assert names == ["alice", "bob", "carol"]


asyncio.run(main())
```

Rules of thumb:

- **IN-lists.** Write `= ANY({seq})` with `seq` a list.
- **Literal braces.** JSON and array literals in SQL are written `{{` and `}}`.
- **No interpolation inside a string literal or comment.** `LIKE '%{q}%'` puts the text `$1` inside the literal. Bind
  the whole pattern instead: `LIKE {pat}`. The checker reports this as `param-count-mismatch`, and at runtime the
  parameter count disagrees and raises `InterfaceError`.
- **Cast parameters whose type the server can't infer.** For example `{x}::int IS NULL`. Without a cast, Postgres fails
  with 42P18 or 42P08.
- **Don't write `$n` by hand.** It causes the same count mismatch.
- **Reusing an expression** (`{a}` twice) creates two parameters.

In `query_unchecked`, a nested `Template` is flattened and its values stay bound, and `{name:ident}` emits a quoted
identifier (a `str`, or a `tuple[str, ...]` joined with dots). There is no raw-SQL escape.

## Nullability overrides

The checker infers column nullability from the catalog and the query plan. When it can't prove a column is NOT NULL, it
treats it as nullable, and your annotation must say `| None`. Narrow it with a quoted alias:

- `AS "col!"` forces NOT NULL.
- `AS "col?"` forces nullable.

The runtime ignores the suffix for tuples and scalars (rows are positional), so a NULL in a `!` column still raises
`RowShapeError`. For a named row type the suffix is stripped from the name the column is matched under. Postgres
truncates identifiers at 63 bytes, which would drop the suffix; the checker reports that as `invalid-override`.

```python test
import asyncio

from rowx.postgres import connect
from rowx import query


async def main() -> None:
    async with await connect(DSN) as conn:
        n = await query[int](t'SELECT count(*) AS "n!" FROM users').one(conn)
        assert n == 3
        email = await query[str | None](t'SELECT email AS "email?" FROM users WHERE name = {"carol"}').one(conn)
        assert email is None or isinstance(email, str)


asyncio.run(main())
```

## Pools

`rowx.postgres` holds the Postgres backend (`connect`, `ConnectOptions`, `Connection`, `Pool`, `Transaction`). The
shared pieces (`query`, `query_unchecked`, `QueryResult`, `RowStream`, `Executor` and the errors) stay in `rowx`, and
`import rowx` loads no backend.

A `Pool` is an `Executor`, so every query method takes it in place of a connection. The constructor does no IO, so a
pool is safe to create at module level. `async with pool` warms it to `min_size` and closes it on exit; the first
acquire also opens it.

```python test
import asyncio

from rowx.postgres import Pool
from rowx import query


async def main() -> None:
    async with Pool(DSN, min_size=1, max_size=4) as pool:
        # acquire, run, release: one call
        assert await query[int](t"SELECT 1").one(pool) == 1

        # many tasks share the pool; waiters are served in order
        results = await asyncio.gather(*(query[int](t"SELECT {i}::int").one(pool) for i in range(20)))
        assert results == list(range(20))

        # hold one connection for several statements
        async with pool.acquire() as conn:
            await query(t"CREATE TEMP TABLE scratch (n int)").execute(conn)
            await query(t"INSERT INTO scratch VALUES (1), (2)").execute(conn)
            assert await query[int](t'SELECT count(*) AS "n!" FROM scratch').one(conn) == 2

        assert pool.size <= pool.max_size


asyncio.run(main())
```

### Sizing and lifetimes

| Option | Default | Meaning |
|---|---|---|
| `min_size` | 0 | Connections kept open (opened by `open()` and maintained in the background). |
| `max_size` | 10 | The most connections open at once. Waiters queue past it. |
| `acquire_timeout` | 30.0 | Seconds an acquire waits before `PoolTimeoutError`. A failed connect is retried until then. |
| `idle_timeout` | 600.0 | Idle connections above `min_size` are closed after this long. |
| `max_lifetime` | 1800.0 | A connection older than this is closed on release instead of reused. |
| `max_queries` | `None` | Retire a connection after this many queries. |
| `ping_after` | 30.0 | A connection idle for longer is pinged at checkout. There is no round trip otherwise. |

Idle connections are reused most-recently-used first, so hot connections stay hot and cold ones age out. A query that
fails with an IO error is not retried, because it may not be idempotent. `pool.expire_connections()` retires every
connection as it is released. `pool.size`, `idle`, `waiting`, `min_size`, `max_size` and `closed` are available for
metrics.

```python test
import asyncio

from rowx.postgres import Pool
from rowx import query
from rowx.errors import PoolTimeoutError


async def main() -> None:
    async with Pool(DSN, max_size=1, acquire_timeout=0.2) as pool, pool.acquire():
        try:
            await query[int](t"SELECT 1").one(pool)
        except PoolTimeoutError:
            pass
        else:
            raise AssertionError


asyncio.run(main())
```

### Hooks and the reset

Hooks are async callables that take a `Connection`. They run on the caller's event loop, and a hook that raises
discards the connection.

- `init` runs once per connection, at its first checkout.
- `setup` runs before each checkout.
- `reset` runs at release, after the reset query.

On release the pool runs `reset_query` (by default `RESET_QUERY`: `SELECT pg_advisory_unlock_all(); CLOSE ALL;
UNLISTEN *; RESET ALL;`), preceded by a `ROLLBACK` when the connection may be inside a transaction. It does not
`DISCARD ALL`, so the statement cache survives. Pass `reset_query=None` to turn it off.

`RESET ALL` returns session settings to their startup values. That is why `server_settings` survive a release (they
are part of the startup options) while a `SET` run in `init` does not: put settings you want to keep in
`ConnectOptions(server_settings=...)`, or re-apply them in `setup`.

```python test
import asyncio

from rowx.postgres import ConnectOptions, Connection, Pool
from rowx import query


async def main() -> None:
    async def init(conn: Connection) -> None:
        await query(t"SET work_mem = '8MB'").execute(conn)

    opts = ConnectOptions.from_dsn(DSN, server_settings={"statement_timeout": "5s"})
    async with Pool(opts, max_size=1, init=init) as pool:
        async with pool.acquire() as conn:
            assert await query[str](t"SHOW work_mem").one(conn) == "8MB"
            assert await query[str](t"SHOW statement_timeout").one(conn) == "5s"
        async with pool.acquire() as conn:
            # RESET ALL put work_mem back; the startup setting survived
            assert await query[str](t"SHOW work_mem").one(conn) != "8MB"
            assert await query[str](t"SHOW statement_timeout").one(conn) == "5s"


asyncio.run(main())
```

A `fetch` on a pool acquires at its first row and holds that connection until the stream ends or is closed. Prefer
`async with`, and see the caveats below.

### Deployment caveats

- **Fork.** A pool records its process id, and using it in a forked child raises `InterfaceError`. Create the pool
  after the fork (for gunicorn, do not create it at import time under `--preload`; create it in a worker start hook or
  the app's startup).
- **What `RESET ALL` does not reset:** `SET ROLE`, `SET SESSION AUTHORIZATION` and temporary tables. Undo them
  yourself, in a `reset` hook or a custom `reset_query`.
- **Session encoding drift.** rowx sends UTF-8 and standard-conforming strings, and watches the server's parameter
  reports. If application SQL runs `SET client_encoding` to anything but `UTF8` or `SET standard_conforming_strings =
  off` (also `SET LOCAL`, or a role or database default of `standard_conforming_strings = off`), the connection is
  marked broken for good. The next call raises `InterfaceError` naming the setting, and a pool discards the connection
  when it is released, so a legitimate `SET standard_conforming_strings = off` costs a connection. Setting
  `client_encoding = 'UTF8'` again changes nothing. This is a backstop with a small window: a statement already queued
  behind the `SET` (for example from another task sharing a connection) may still run first.
- **Early exit from a pooled stream drains.** Breaking out of a `fetch` makes the connection discard the remaining rows
  before the reset runs. The connection stays counted against `max_size` while it drains. Bound large reads with
  `LIMIT`.
- **Hooks move release onto the caller's path.** With no hooks, release happens in the background and `__aexit__`
  returns at once. With any hook set, `__aexit__` awaits the `reset` hook and the reset query first.
- **Leases.** A stream or transaction on a pooled connection is tied to that checkout. Using it after the
  `async with pool.acquire()` block raises `InterfaceError`. `conn.detach()` takes a connection out of the pool for
  good.

### Coming from asyncpg

| asyncpg | rowx |
|---|---|
| `await asyncpg.create_pool(dsn, min_size=1, max_size=10)` | `Pool(dsn, min_size=1, max_size=10)`, then `async with pool` or `await pool.open()` |
| `await pool.fetchrow(sql, *args)` | `await query[Row](t"...").one(pool)` (or `.optional(pool)`) |
| `async with pool.acquire() as conn:` | `async with pool.acquire() as conn:` |
| `async with conn.transaction():` | `async with conn.transaction() as tx:` or `async with pool.transaction() as tx:` |
| `await pool.close()` | `await pool.close()` (`terminate()` is synchronous) |

## Transactions

`conn.transaction()` and `pool.transaction()` (which holds a connection for the block) return a `Transaction`. It is an
`Executor`: queries run on it are part of the transaction. A clean exit commits, and an exception or a cancellation
rolls back. `commit()` and `rollback()` end it early, and using the transaction afterwards raises `InterfaceError`.
`tx.transaction()` opens a savepoint, which takes no isolation arguments.

```python test
import asyncio

from rowx.postgres import Pool
from rowx import query


async def main() -> None:
    async with Pool(DSN, max_size=2) as pool:
        count = query[int](t'SELECT count(*) AS "n!" FROM orders')
        before = await count.one(pool)

        # an exception rolls back
        try:
            async with pool.transaction(isolation="serializable") as tx:
                await query(t"DELETE FROM orders").execute(tx)
                raise RuntimeError
        except RuntimeError:
            pass
        assert await count.one(pool) == before

        # a savepoint rolls back on its own
        async with pool.transaction() as tx:
            await query(t"DELETE FROM orders WHERE id = (SELECT min(id) FROM orders)").execute(tx)
            try:
                async with tx.transaction():
                    await query(t"DELETE FROM orders").execute(tx)
                    raise RuntimeError
            except RuntimeError:
                pass
            assert await count.one(tx) == before - 1
            await tx.rollback()
        assert await count.one(pool) == before


asyncio.run(main())
```

- **Failed transactions.** An error inside the block marks the transaction failed. Leaving the block cleanly while it
  is failed raises `InterfaceError` instead of letting `COMMIT` roll back silently. Rolling back to a savepoint clears
  the mark.
- **The ending statement can't be cancelled.** `COMMIT` and `ROLLBACK` run detached, so a second cancel can't leave the
  transaction open. If you are cancelled while a `COMMIT` is in flight, the outcome is unknown: it raises and the
  connection is discarded.
- **A failed `COMMIT`** (for example `40001`) raises `DatabaseError`; the connection is left idle and outside a
  transaction.
- **Raw `BEGIN` and `COMMIT` alongside the helper are unsupported.** Use one or the other on a connection.

## Migrations

Migrations are plain `.sql` files in the format [sqlx](https://github.com/launchbadge/sqlx) uses. `Migrator` applies
them in version order, records each one in a history table, and holds an advisory lock while it works. `rowx migrate`
and `rowx database` do the same from the command line, and `rowx check` and the `rowx.testing` pytest plugin build on
it. The blocks below are executed by the test suite, each in a temporary directory.

### Files

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

from rowx.postgres import Migrator, Pool

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

### Running

At startup, in the application, or as a deploy step. The lock makes both safe when several instances start together.

```python
from rowx.postgres import Migrator, Pool

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

### Safety

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
from rowx.errors import DirtyMigrationError, MigrationChecksumError, MigrationFailedError, MigrationOrderError
from rowx.postgres import Migrator, connect

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

### Setup

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

### Adopting from sqlx

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
from rowx.postgres import Migrator, connect

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

### Checking

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

### Testing

`rowx.testing` is a pytest plugin that gives each test a freshly migrated database. It is opt-in: install the extra and
name the plugin in a `conftest.py`.

```sh
pip install "rowx[pytest]"
```

```python
# conftest.py
pytest_plugins = ["rowx.testing"]
```

(`-p rowx.testing` on the command line works too. `import rowx` never imports pytest, and nothing loads without this.)

| Fixture | Value |
|---|---|
| `rowx_database_url` | The URL of a database cloned for this test. |
| `rowx_pool` | An open `Pool` on that database. |
| `rowx_connection` | A `Connection` to it. |

The fixtures are synchronous, so an async test uses the plugin's database through `asyncio.run`, or runs under
pytest-asyncio or anyio. The plugin supports **asyncio only**: rowx awaitables are asyncio futures, so anyio works on
its asyncio backend, and Trio does not.

```python
# test_signup.py
from rowx import query
from rowx.postgres import Pool


async def test_signup(rowx_pool: Pool) -> None:  # with pytest-asyncio or anyio's asyncio backend
    assert await query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool) == 0
```

Configuration:

- `rowx_database_url_env` (a `pytest.ini` or `[tool.pytest.ini_options]` option) names the environment variable that
  holds the server URL, so a password isn't written into the ini file. It defaults to `database_url_env` in
  `[tool.rowx]` (`DATABASE_URL`). The role needs `CREATEDB`. With no URL the tests skip, or fail when
  `rowx_require_database = true`.
- The migrations are `dir` (default `migrations/`) from `[tool.rowx.migrate]` in the `pyproject.toml` at pytest's
  rootdir or above it. `rowx_migrations` (relative to the rootdir) overrides it. Keep a `[tool.pytest.ini_options]`
  table next to the `pyproject.toml` so pytest picks that directory as its rootdir. A missing migrations directory
  fails the session. With neither `[tool.rowx.migrate]` nor `rowx_migrations`, tests get an empty database.
- `--rowx-keep-templates` keeps this project's stale templates instead of dropping them.

```toml
[tool.pytest.ini_options]
rowx_database_url_env = "TEST_DATABASE_URL"
rowx_migrations = "db/migrations"
rowx_require_database = true
```

How the template works:

- The plugin migrates **once per session**, into a template database named `rowx_tpl_<p>_<h>`. `<p>` is the first 8 hex
  characters of the SHA-256 of the migrations path, so it identifies the project. `<h>` hashes the rowx version, the
  path, the history table, `create_schemas` and the `(version, checksum)` list of the forward migrations, so editing a
  migration builds a new template, and an unchanged directory reuses the existing one across sessions and across
  processes (such as `pytest -n 2` workers) that share the server.
- A build takes an advisory lock for `<h>` before it looks for the template, so concurrent sessions build it once. It
  migrates a `_tmp_` database and renames it to the final name, so a database under that name is always complete.
  Building a new template drops this project's stale `rowx_tpl_<p>_*` databases that no session is using, and never
  another project's.
- Each test runs `CREATE DATABASE rowx_test_<p>_<hex> TEMPLATE rowx_tpl_<p>_<h>` and drops the clone with `WITH (FORCE)`
  afterwards, so no test sees another's rows. Clones and `_tmp_` builds carry a `COMMENT` with the process, host and
  time, and the next session sweeps this project's that are over an hour old and have no connections, such as the
  leftovers of a killed run.
- Roles are cluster-wide, so a test that creates one isn't isolated by the clone.

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
    # the plugin wants a server URL; the DSN of this page's tests pins a search_path, so its admin URL stands in
    server_url = os.environ["ROWX_TEST_DSN"]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=root,
        env={**os.environ, "DATABASE_URL": server_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

A complete project, with migrations, an application module, drift-checked queries and a `conftest.py`, is in
[`examples/migrations`](../examples/migrations).

## The checker

```text
rowx check [PATHS...] [--database-url URL] [--config FILE] [--output-format text|json] [--statistics] [-j N] [--scratch] [--no-drift-check]
```

The checker never imports or executes your code. It parses your files, finds every `query[T](t"...")` call, resolves `T`
statically, and asks Postgres to prepare and describe the SQL. Then it compares the columns and parameters with your
types. See [the checker rules](design/checker.md#rules-all-errors) for the rules.

| Exit code | Meaning |
|---|---|
| 0 | No diagnostics, including when no queries were found. |
| 1 | One or more diagnostics. |
| 2 | Usage or config error, an unreachable database, a describe failure that is not an SQL error, or an internal error. |

Configuration lives in `pyproject.toml`:

```toml
[tool.rowx]
include = ["src"]                 # default ["."]
exclude = ["**/migrations/**"]
database_url_env = "DATABASE_URL"
statement_timeout = "10s"         # integer + unit: ms|s|min
```

`[tool.rowx.migrate]` is described under [Migrations](#migrations).

The database URL comes from `--database-url`, else from the environment variable named by `database_url_env`.

### Safety

- The checker performs Parse/Describe, catalog lookups and `EXPLAIN` only, never `ANALYZE`. It does not execute your
  statements.
- Postgres may still **evaluate user-defined IMMUTABLE and STABLE functions while planning**, and it takes locks on
  referenced relations.
- Run it as a dedicated non-superuser role against a CI or disposable database.

With `--scratch` the statement is amended:

- The checker still never imports or executes your Python, and never executes your checked queries.
- It does execute the **migration files**, and only in the database it created for the run, as the connecting role.
  Migration files are trusted code. The role needs `CREATEDB`.

`rowx check --scratch` needs `[tool.rowx.migrate]`. It treats the database URL as a server URL, creates
`rowx_check_<hex>` (`TEMPLATE template0`), runs the migrations in it, checks against it, and drops it `WITH (FORCE)` on
every exit path. The drift gate doesn't apply. A missing `CREATEDB` privilege and a failing migration both exit 2.

The scratch database is tagged with `COMMENT ON DATABASE 'rowx:<pid>@<host>:<unix-ts>'`. At startup the checker drops
any `rowx_check_*` database whose tag is over an hour old and which has no sessions, ignoring errors, so a SIGKILLed or
cancelled CI run doesn't leak its database. Administrative connections use the URL's own database, or `postgres`
(`template1` when the target is `postgres`), and fall back from `postgres` to `template1` where `postgres` is missing or
refused.

The checker's session is read-only by default (`default_transaction_read_only = on`) and sets `statement_timeout`,
`lock_timeout` and `idle_in_transaction_session_timeout`.

## CI recipe

Run the checker against a throwaway `postgres:18` service that has your schema applied.

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
      # migrate: replace with your migration tool, or use `rowx migrate run` (see Migrations)
      - run: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql
      - run: uv run rowx check src
```

Pin the action versions to commit hashes in your own workflow. Migrate **before** checking: the checker validates
against whatever schema the database has. With SQL file migrations, [`rowx check --scratch`](#checking) builds the
database itself.

## Server-side poolers and the statement cache

Each connection keeps an LRU cache of prepared statements, sized by `statement_cache_size` (default 256). `0` disables
the cache, so every call uses an unnamed prepare.

Behind an external pooler in **transaction mode** (PgBouncer, for example), a prepared statement may land on a different
server connection than the one that prepared it. Either connect with `statement_cache_size=0`, or use PgBouncer 1.21 or
newer with `max_prepared_statements` set.

```python
from rowx.postgres import ConnectOptions, Pool

pool = Pool(ConnectOptions.from_dsn("postgresql://app@pooler:6432/app", statement_cache_size=0), max_size=5)
```

Also behind a pooler:

- **Transaction mode** hands out a different server connection per transaction, so session state (`SET`, advisory
  locks, `LISTEN`) isn't reliable. The reset query is then pointless work, so consider `reset_query=None`, and keep
  `max_size` below the pooler's server pool size.
- **Session mode** keeps a server connection per client connection, so the default reset works as it does against
  Postgres directly.

TLS defaults to `prefer`, as in libpq: it uses TLS when the server offers it, without verifying the certificate, and
falls back to plaintext when the server doesn't support TLS. Use `sslmode=verify-full` in production.

## Known limits

- **Nullability inference** is conservative, never optimistic. Views, set operations, recursive CTEs and computed
  columns are nullable; use `"col!"` to narrow. See [known-limits.md](known-limits.md) for each case.
- **Unsupported types** (multiranges, composites, `xml`, `hstore`, `regclass`, ...) are
  `unsupported-type` in the checker and raise `DecodeError` at runtime. Cast to `text`.
- **Decode errors.** `date`, `timestamp` and `timestamptz` that are infinite or outside years 1 to 9999, and `interval`
  values with a months component, raise `DecodeError`. Cast to `text`.
- **Array elements.** Element types and nullability inside `list[...]` are not checked at runtime; a NULL element
  decodes to `None`.
- **One operation per connection.** There is no pipelining yet. An open `fetch` stream holds the connection until it
  ends (on a pool, until it releases the lease).
- **No multi-statement execution** and **no command tag** in `QueryResult`. See [known-limits.md](known-limits.md).
- **Cancellation.** A cancelled statement inside a transaction aborts it (25P02), and a future cancelled during prepare
  can leak a named server statement until the connection closes.
- **Schema changes.** If the schema changes inside a transaction, a retried statement can fail with 25P02. `ROLLBACK`
  and retry.
- **Named row types** support dataclasses, NamedTuple, TypedDict and pydantic. attrs, msgspec, nested row classes and
  type overrides (`"col: Type"`) are not supported. See [What isn't supported](#what-isnt-supported).
- **`float` parameters.** An `int` above 2^53 bound to a `float4`/`float8` parameter loses precision.
