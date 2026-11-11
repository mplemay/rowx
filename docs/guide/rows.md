# Rows

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

from rowx_postgres import connect
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
`rowx check` finds both statically. See [types.md](../types.md) for what each Postgres type decodes to.

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
from rowx_postgres import connect


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
[Nullability overrides](parameters.md#nullability-overrides)), so `AS "id!"` maps to the field `id`. Matching is
case-sensitive. Alias a column with `AS` to give it a field's name. Every violation is collected and reported, not just
the first. At runtime that is a `RowShapeError`, before the statement executes (even for an empty result), and the
checker reports one diagnostic for each:

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
from rowx_postgres import connect


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
from rowx_postgres import connect


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
from rowx_postgres import connect


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
  models. They are deferred, not rejected for good: see the [roadmap](../contributing/architecture.md#deferred).
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
