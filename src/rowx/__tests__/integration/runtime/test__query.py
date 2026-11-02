import asyncio
import datetime as dt
from dataclasses import dataclass
from string.templatelib import Template
from typing import TYPE_CHECKING, Final, NamedTuple, NotRequired, TypedDict

import pytest
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.dataclasses import dataclass as pydantic_dataclass

from rowx import RowStream, query, query_unchecked
from rowx.errors import RowConstructError, RowShapeError
from rowx.postgres import Cube, Interval, Money, Point, Pool, Range, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx import Executor

pytestmark = [pytest.mark.integration, pytest.mark.db]

SQL: Final[str] = "SELECT 1::int4 AS id, 'a'::text AS name"


@dataclass
class User:
    id: int
    name: str


class UserRow(NamedTuple):
    id: int
    name: str


class UserDict(TypedDict):
    id: int
    name: str


@dataclass
class Partial:
    id: int
    name: str | None = None


class PartialRow(NamedTuple):
    id: int
    name: str | None = None


class PartialDict(TypedDict):
    id: int
    name: NotRequired[str]


@dataclass
class Spans:
    span: Interval
    plain: str


@dataclass
class Flaky:
    id: int

    def __init__(self, *, id: int) -> None:  # ruff: ignore[builtin-argument-shadowing]
        if id == 3:
            msg = "three"
            raise ValueError(msg)
        self.id = id


@dataclass
class Strict:
    id: int

    def __init__(self, **_row: object) -> None:
        msg = "boom"
        raise TypeError(msg)


FORMS: Final[list[tuple[type, object]]] = [
    (User, User(id=1, name="a")),
    (UserRow, UserRow(1, "a")),
    (UserDict, {"id": 1, "name": "a"}),
]
EXECUTORS: Final[list[str]] = ["connection", "pool", "transaction"]


def run_on[T](dsn: str, kind: str, body: Callable[[Executor], Awaitable[T]]) -> T:
    async def run() -> T:
        if kind == "pool":
            async with Pool(dsn, max_size=1) as pool:
                return await body(pool)
        async with await connect(dsn) as conn:
            if kind == "transaction":
                async with conn.transaction() as tx:
                    return await body(tx)
            return await body(conn)

    return asyncio.run(run())


async def collect[T](stream: RowStream[T]) -> list[T]:
    return [row async for row in stream]


@pytest.mark.parametrize("kind", EXECUTORS)
@pytest.mark.parametrize(("row_type", "expected"), FORMS, ids=[f[0].__name__ for f in FORMS])
def test_each_form_builds_rows_through_one_optional_all_and_fetch(
    schema: str, kind: str, row_type: type, expected: object
) -> None:
    q = query[row_type](t"SELECT g::int4 AS id, 'a'::text AS name FROM generate_series(1, 2) g")  # ty: ignore[invalid-type-form]
    first = query[row_type](t"SELECT 1::int4 AS id, 'a'::text AS name")  # ty: ignore[invalid-type-form]
    none = query[row_type](t"SELECT 1::int4 AS id, 'a'::text AS name WHERE false")  # ty: ignore[invalid-type-form]

    async def body(conn: Executor) -> tuple[object, ...]:
        return (
            await first.one(conn),
            await first.optional(conn),
            await none.optional(conn),
            await q.all(conn),
            await collect(q.fetch(conn, batch_size=1)),
        )

    one, optional, missing, every, streamed = run_on(schema, kind, body)
    assert one == expected
    assert optional == expected
    assert missing is None
    assert isinstance(every, list)
    assert len(every) == 2
    assert every == streamed
    assert type(one) is type(expected)


def test_a_dataclass_row_is_an_instance_of_the_class(schema: str) -> None:
    row = run_on(schema, "connection", query[User](t"SELECT 3::int4 AS id, 'z'::text AS name").one)
    assert isinstance(row, User)
    assert (row.id, row.name) == (3, "z")


def test_by_name_forms_ignore_the_column_order(schema: str) -> None:
    sql = t"SELECT 'a'::text AS name, 1::int4 AS id"

    async def body(conn: Executor) -> tuple[object, object]:
        return await query[User](sql).one(conn), await query[UserDict](sql).one(conn)

    assert run_on(schema, "connection", body) == (User(id=1, name="a"), {"name": "a", "id": 1})


@pytest.mark.parametrize("kind", EXECUTORS)
@pytest.mark.parametrize("override", ["!", "?"])
def test_override_suffixes_map_to_the_plain_field_name(schema: str, kind: str, override: str) -> None:
    from string.templatelib import Template  # ruff: ignore[import-outside-top-level]

    sql = Template(f'SELECT 1::int4 AS "id{override}", \'a\'::text AS "name{override}"')

    async def body(conn: Executor) -> tuple[object, object, object]:
        return (
            await query[User](sql).one(conn),
            await query[UserDict](sql).one(conn),
            await query[UserRow](sql).one(conn),
        )

    assert run_on(schema, kind, body) == (User(id=1, name="a"), {"id": 1, "name": "a"}, UserRow(1, "a"))


def test_a_field_with_a_default_may_have_no_column(schema: str) -> None:
    sql = t"SELECT 1::int4 AS id"

    async def body(conn: Executor) -> tuple[object, object, object]:
        return (
            await query[Partial](sql).one(conn),
            await query[PartialRow](sql).one(conn),
            await query[PartialDict](sql).one(conn),
        )

    assert run_on(schema, "connection", body) == (Partial(id=1), PartialRow(1), {"id": 1})


def test_an_interval_field_reads_a_lossless_interval(schema: str) -> None:
    q = query[Spans](t"SELECT interval '1 month 2 days' AS span, 'x'::text AS plain")
    row = run_on(schema, "connection", q.one)
    assert isinstance(row.span, Interval)


# an empty result proves that the mapping fails before execution, not on the first row
@pytest.mark.parametrize(
    ("row_type", "sql", "message"),
    [
        (User, "SELECT 1::int4 AS id WHERE false", 'field "name" of `User` has no column'),
        (
            User,
            "SELECT 1::int4 AS id, 'a'::text AS name, 2::int4 AS age WHERE false",
            'column 3 "age" matches no field',
        ),
        (UserDict, "SELECT 1::int4 AS id WHERE false", 'field "name" of `UserDict` has no column'),
        (UserDict, "SELECT 1::int4 AS id, 'a'::text AS name, 2 AS age WHERE false", 'column 3 "age" matches no field'),
        (
            UserRow,
            "SELECT 'a'::text AS name, 1::int4 AS id WHERE false",
            'column 1 "name" faces field "id" of `UserRow`',
        ),
    ],
)
def test_a_shape_mismatch_is_a_row_shape_error_before_execution(
    schema: str, row_type: type, sql: str, message: str
) -> None:
    from string.templatelib import Template  # ruff: ignore[import-outside-top-level]

    q = query[row_type](Template(sql))  # ty: ignore[invalid-type-form]
    with pytest.raises(RowShapeError, match=message):
        run_on(schema, "connection", q.all)


def test_a_hand_written_init_that_raises_is_a_row_construct_error(schema: str) -> None:
    with pytest.raises(RowConstructError) as caught:
        run_on(schema, "connection", query[Strict](t"SELECT 1::int4 AS id").one)
    assert caught.value.type_name == "Strict"
    assert caught.value.row_index == 0
    assert isinstance(caught.value.__cause__, TypeError)


def test_query_unchecked_builds_named_rows(schema: str) -> None:
    table = "users"
    q = query_unchecked[UserRow](t"SELECT id, name FROM {table:ident} ORDER BY id LIMIT 1")
    assert run_on(schema, "connection", q.one) == UserRow(1, "alice")


def test_query_unchecked_builds_a_dataclass_and_a_typeddict(schema: str) -> None:
    table = "users"

    async def body(conn: Executor) -> tuple[object, object]:
        return (
            await query_unchecked[User](t"SELECT id, name FROM {table:ident} ORDER BY id LIMIT 1").one(conn),
            await query_unchecked[UserDict](t"SELECT id, name FROM {table:ident} ORDER BY id LIMIT 1").one(conn),
        )

    assert run_on(schema, "connection", body) == (User(id=1, name="alice"), {"id": 1, "name": "alice"})


@pytest.mark.parametrize("row_type", [User, UserRow, UserDict])
def test_a_shape_error_surfaces_from_the_first_anext_of_a_fetch_and_from_one(schema: str, row_type: type) -> None:
    from string.templatelib import Template  # ruff: ignore[import-outside-top-level]

    q = query[row_type](Template("SELECT 1::int4 AS id, 2 AS age WHERE false"))  # ty: ignore[invalid-type-form]

    async def via_fetch(conn: Executor) -> None:
        stream = q.fetch(conn)
        await anext(stream)

    with pytest.raises(RowShapeError):
        run_on(schema, "connection", via_fetch)
    with pytest.raises(RowShapeError):
        run_on(schema, "connection", q.one)


@pytest.mark.parametrize("kind", EXECUTORS)
def test_a_row_construct_error_on_a_later_row_keeps_the_rows_before_it(schema: str, kind: str) -> None:
    q = query[Flaky](t"SELECT g::int4 AS id FROM generate_series(1, 4) g ORDER BY g")
    seen: list[int] = []

    async def body(conn: Executor) -> None:
        stream = q.fetch(conn, batch_size=1)
        while True:
            seen.append((await anext(stream)).id)

    with pytest.raises(RowConstructError) as caught:
        run_on(schema, kind, body)
    assert seen == [1, 2]
    assert caught.value.type_name == "Flaky"
    assert caught.value.row_index == 2
    assert isinstance(caught.value.__cause__, ValueError)


class UserModel(BaseModel):
    id: int
    name: str


@pydantic_dataclass
class UserPydantic:
    id: int
    name: str


class AliasedModel(BaseModel):
    id: int = Field(alias="user_id")
    name: str


class OverlapModel(BaseModel, populate_by_name=True):
    id: int = Field(alias="user_id")
    user_id: int


class DuplicateModel(BaseModel, populate_by_name=True):
    id: int = Field(alias="user_id")


class InitModel(BaseModel):
    id: int
    doubled: int = 0

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        self.doubled = self.id * 2


class PositiveModel(BaseModel):
    id: int = Field(gt=0)


class AfterModel(BaseModel):
    id: int

    @field_validator("id")
    @classmethod
    def not_three(cls, value: int) -> int:
        if value == 3:
            msg = "three"
            raise ValueError(msg)
        return value


SEEN: Final[list[dict[str, object]]] = []


class UnknownModel(BaseModel):
    id: int = Field(validation_alias=AliasChoices("id", "other"))

    @model_validator(mode="before")
    @classmethod
    def keep(cls, data: dict[str, object]) -> dict[str, object]:
        SEEN.append(dict(data))
        return {"id": data["id"]}


class ExtraModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int


@pydantic_dataclass(config=ConfigDict(strict=True))
class StrictRow:
    id: int
    name: str | None
    aliased: int = Field(default=0, alias="a")


PYDANTIC_FORMS: Final[list[tuple[type, object]]] = [
    (UserModel, UserModel(id=1, name="a")),
    (UserPydantic, UserPydantic(id=1, name="a")),
]


@pytest.mark.parametrize("kind", EXECUTORS)
@pytest.mark.parametrize(("row_type", "expected"), PYDANTIC_FORMS, ids=[f[0].__name__ for f in PYDANTIC_FORMS])
def test_pydantic_rows_build_through_one_optional_all_and_fetch(
    schema: str, kind: str, row_type: type, expected: object
) -> None:
    q = query[row_type](t"SELECT g::int4 AS id, 'a'::text AS name FROM generate_series(1, 2) g")  # ty: ignore[invalid-type-form]
    first = query[row_type](t"SELECT 1::int4 AS id, 'a'::text AS name")  # ty: ignore[invalid-type-form]
    none = query[row_type](t"SELECT 1::int4 AS id, 'a'::text AS name WHERE false")  # ty: ignore[invalid-type-form]

    async def body(conn: Executor) -> tuple[object, ...]:
        return (
            await first.one(conn),
            await first.optional(conn),
            await none.optional(conn),
            await q.all(conn),
            await collect(q.fetch(conn, batch_size=1)),
        )

    one, optional, missing, every, streamed = run_on(schema, kind, body)
    assert one == expected
    assert optional == expected
    assert missing is None
    assert isinstance(every, list)
    assert len(every) == 2
    assert every == streamed
    assert type(one) is row_type


@pytest.mark.parametrize("row_type", [UserModel, UserPydantic])
def test_a_reordered_select_still_fills_a_pydantic_row_by_name(schema: str, row_type: type) -> None:
    row = run_on(schema, "connection", query[row_type](t"SELECT 'z'::text AS name, 7::int4 AS id").one)  # ty: ignore[invalid-type-form]
    assert (row.id, row.name) == (7, "z")


def test_an_aliased_field_is_filled_from_its_alias_column(schema: str) -> None:
    row = run_on(schema, "connection", query[AliasedModel](t"SELECT 'z'::text AS name, 7::int4 AS user_id").one)
    assert (row.id, row.name) == (7, "z")


def test_a_pydantic_dataclass_alias_is_honoured_under_strict(schema: str) -> None:
    q = query[StrictRow](t"SELECT 1::int4 AS id, NULL::text AS name, 5::int4 AS a")
    row = run_on(schema, "connection", q.one)
    assert row == StrictRow(id=1, name=None, a=5)


def test_a_custom_init_on_a_model_runs(schema: str) -> None:
    row = run_on(schema, "connection", query[InitModel](t"SELECT 4::int4 AS id").one)
    assert (row.id, row.doubled) == (4, 8)


def test_a_failing_constraint_is_a_row_construct_error_caused_by_a_validation_error(schema: str) -> None:
    with pytest.raises(RowConstructError) as caught:
        run_on(schema, "connection", query[PositiveModel](t"SELECT 0::int4 AS id").one)
    assert caught.value.type_name == "PositiveModel"
    assert caught.value.row_index == 0
    assert isinstance(caught.value.__cause__, ValidationError)


def test_a_failing_after_validator_is_a_row_construct_error_on_the_failing_row(schema: str) -> None:
    q = query[AfterModel](t"SELECT g::int4 AS id FROM generate_series(1, 4) g ORDER BY g")
    with pytest.raises(RowConstructError) as caught:
        run_on(schema, "connection", q.all)
    assert caught.value.row_index == 2
    assert isinstance(caught.value.__cause__, ValidationError)


def test_a_failing_pydantic_dataclass_is_a_row_construct_error_caused_by_a_validation_error(schema: str) -> None:
    with pytest.raises(RowConstructError) as caught:
        run_on(schema, "connection", query[StrictRow](t"SELECT 1::int4 AS id, 'x'::text AS name, 'bad'::text AS a").one)
    assert isinstance(caught.value.__cause__, ValidationError)


def test_a_model_with_unknown_names_receives_every_column(schema: str) -> None:
    row = run_on(
        schema, "connection", query[UnknownModel](t"SELECT 1::int4 AS id, 'x'::text AS extra, 2::int4 AS n").one
    )
    assert row.id == 1
    assert SEEN[-1] == {"id": 1, "extra": "x", "n": 2}


def test_a_null_in_a_known_non_nullable_field_is_a_row_shape_error_before_pydantic_runs(schema: str) -> None:
    with pytest.raises(RowShapeError, match='NULL in column 1 "id" for non-nullable field "id"'):
        run_on(schema, "connection", query[UserModel](t"SELECT NULL::int4 AS id, 'a'::text AS name").one)


def test_a_null_in_a_known_non_nullable_field_never_reaches_a_validator(schema: str) -> None:
    # a ValidationError would be a RowConstructError, so a RowShapeError means pydantic never ran
    with pytest.raises(RowShapeError):
        run_on(schema, "connection", query[AfterModel](t"SELECT NULL::int4 AS id").one)


def test_extra_allow_does_not_accept_an_extra_column(schema: str) -> None:
    with pytest.raises(RowShapeError, match='column 2 "extra" matches no field'):
        run_on(schema, "connection", query[ExtraModel](t"SELECT 1::int4 AS id, 2::int4 AS extra").one)


def test_populate_by_name_overlap_is_an_ambiguous_column(schema: str) -> None:
    with pytest.raises(RowShapeError, match='matches fields "id" and "user_id"'):
        run_on(schema, "connection", query[OverlapModel](t"SELECT 1::int4 AS id, 2::int4 AS user_id").one)


def test_an_alias_and_the_name_of_one_field_are_a_duplicate_column(schema: str) -> None:
    with pytest.raises(RowShapeError, match="both map to field"):
        run_on(schema, "connection", query[DuplicateModel](t"SELECT 1::int4 AS id, 2::int4 AS user_id").one)


def value_models(*, strict: bool) -> tuple[type[BaseModel], type]:
    config = ConfigDict(strict=strict)

    class Values(BaseModel):
        model_config = config
        ints: Range[int]
        stamps: Range[dt.datetime]
        money: Money
        span: Interval
        point: Point
        cube: Cube

    @pydantic_dataclass(config=config)
    class ValuesDataclass:
        ints: Range[int]
        stamps: Range[dt.datetime]
        money: Money
        span: Interval
        point: Point
        cube: Cube

    return Values, ValuesDataclass


VALUES_SQL: Final[str] = (
    "SELECT int4range(1, 5) AS ints, tsrange('2020-01-01', '2020-01-02') AS stamps, '1.50'::money AS money, "
    "'1 day 2 hours'::interval AS span, point(1, 2) AS point, '(1,2),(3,4)'::public.cube AS cube"
)


@pytest.mark.parametrize("strict", [False, True])
def test_value_classes_pass_through_a_model_and_a_pydantic_dataclass_unchanged(
    schema: str,
    strict: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
) -> None:
    plain = run_on(
        schema,
        "connection",
        query_unchecked[tuple[Range[int], Range[dt.datetime], Money, Interval, Point, Cube]](Template(VALUES_SQL)).one,
    )
    for row_type in value_models(strict=strict):
        row = run_on(schema, "connection", query[row_type](Template(VALUES_SQL)).one)  # ty: ignore[invalid-type-form]
        fields = (row.ints, row.stamps, row.money, row.span, row.point, row.cube)
        assert fields == plain
        assert [type(v) for v in fields] == [Range, Range, Money, Interval, Point, Cube]
