import asyncio
import datetime as dt
import gc
import inspect
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from string.templatelib import Interpolation, Template
from typing import TYPE_CHECKING, Final, NamedTuple, NotRequired, TypedDict

import pytest
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from rowx_core.errors import DecodeError, InterfaceError, RowConstructError, RowNotFound, RowShapeError
from rowx_postgres import Connection, Cube, Interval, Money, Point, Pool, Range, connect

from rowx import ConnectionBusyError, EncodeError, QueryResult, RowStream, query, query_unchecked

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from rowx_postgres._core import Connection as ConnectionImpl

    from rowx import Executor

pytestmark = [pytest.mark.db]

SQL: Final[str] = "SELECT 1::int4 AS id, 'a'::text AS name"


@dataclass
class User:
    id: int
    name: str


EqualStr = type(
    "EqualStr",
    (str,),
    {
        "__slots__": (),
        "__eq__": lambda *_, **__: True,
        "__hash__": lambda *_, **__: 0,
        "__bool__": lambda *_, **__: False,
    },
)


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


async def collect_rows[T](stream: RowStream[T]) -> list[T]:
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
            await collect_rows(q.fetch(conn, batch_size=1)),
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
            await collect_rows(q.fetch(conn, batch_size=1)),
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


class InitFalseModel(BaseModel):
    id: int
    count: int = Field(init=False)


class ByAliasOnlyModel(BaseModel, populate_by_name=True, validate_by_name=False):
    id: int = Field(alias="user_id")
    name: str = "default"


def test_a_model_field_with_init_false_is_fetched(schema: str) -> None:
    row = run_on(schema, "connection", query[InitFalseModel](t"SELECT 1::int4 AS id, 5::int4 AS count").one)
    assert (row.id, row.count) == (1, 5)


def test_a_name_column_is_rejected_when_validate_by_name_is_false(schema: str) -> None:
    with pytest.raises(RowShapeError):
        run_on(schema, "connection", query[ByAliasOnlyModel](t"SELECT 1::int4 AS id").one)


def value_models(*, strict: bool) -> tuple[dict[str, object], type[BaseModel], type]:
    config = ConfigDict(strict=strict)
    seen: dict[str, object] = {}

    # a before validator records each input, so the test can tell the validator's result from a copy
    class Values(BaseModel):
        model_config = config
        ints: Range[int]
        stamps: Range[dt.datetime]
        money: Money
        span: Interval
        point: Point
        cube: Cube

        @field_validator("*", mode="before")
        @classmethod
        def record(cls, value: object, info: ValidationInfo) -> object:
            seen[str(info.field_name)] = value
            return value

    @pydantic_dataclass(config=config)
    class ValuesDataclass:
        ints: Range[int]
        stamps: Range[dt.datetime]
        money: Money
        span: Interval
        point: Point
        cube: Cube

        @field_validator("*", mode="before")
        @classmethod
        def record(cls, value: object, info: ValidationInfo) -> object:
            seen[str(info.field_name)] = value
            return value

    return seen, Values, ValuesDataclass


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
    seen, *row_types = value_models(strict=strict)
    keys = ("ints", "stamps", "money", "span", "point", "cube")
    for row_type in row_types:
        seen.clear()
        row = run_on(schema, "connection", query[row_type](Template(VALUES_SQL)).one)  # ty: ignore[invalid-type-form]
        fields = tuple(getattr(row, k) for k in keys)
        assert fields == plain
        assert [type(v) for v in fields] == [Range, Range, Money, Interval, Point, Cube]
        assert all(getattr(row, k) is seen[k] for k in keys)


@dataclass
class Page[T]:
    id: int
    item: T


class ModelPage[T](BaseModel):
    id: int
    item: T


@dataclass
class Checked[T]:
    id: int
    item: T

    def __post_init__(self) -> None:
        if self.id < 0:
            msg = "negative"
            raise ValueError(msg)


def test_a_generic_dataclass_row_is_the_origin_class_without_an_orig_class(schema: str) -> None:
    q = query[Page[int]](t"SELECT g::int4 AS id, 2::int4 AS item FROM generate_series(1, 3) g")
    rows = run_on(schema, "connection", q.all)
    assert [type(row) for row in rows] == [Page, Page, Page]
    # calling `Page[int](...)` would set `__orig_class__` on every row
    assert not any(hasattr(row, "__orig_class__") for row in rows)
    assert rows[0] == Page(1, 2)


def test_a_generic_pydantic_row_is_built_by_the_subscripted_model(schema: str) -> None:
    row = run_on(schema, "connection", query[ModelPage[int]](t"SELECT 1::int4 AS id, 2::int4 AS item").one)
    assert type(row) is ModelPage[int]
    assert isinstance(row, ModelPage)
    assert (row.id, row.item) == (1, 2)


def test_the_substituted_field_type_decides_nullability_for_a_generic_row(schema: str) -> None:
    row = run_on(schema, "connection", query[Page[int | None]](t"SELECT 1::int4 AS id, NULL::int4 AS item").one)
    assert row == Page(1, None)
    with pytest.raises(RowShapeError, match=r'NULL in column 2 "item" for non-nullable field "item" of `Page\[int\]`'):
        run_on(schema, "connection", query[Page[int]](t"SELECT 1::int4 AS id, NULL::int4 AS item").one)


@pytest.mark.parametrize("row_type", [Page[int], ModelPage[int]], ids=["dataclass", "pydantic"])
def test_a_shape_error_names_the_row_type_with_its_arguments(schema: str, row_type: type) -> None:
    q = query[row_type](t"SELECT 1::int4 AS id")  # ty: ignore[invalid-type-form]
    with pytest.raises(RowShapeError, match=r'field "item" of `(Page|ModelPage)\[int\]` has no column'):
        run_on(schema, "connection", q.all)


def test_a_construct_error_names_the_dataclass_with_its_arguments(schema: str) -> None:
    with pytest.raises(RowConstructError) as caught:
        run_on(schema, "connection", query[Checked[int]](t"SELECT -1::int4 AS id, 2::int4 AS item").one)
    assert caught.value.type_name == "Checked[int]"
    assert "Checked[int]" in str(caught.value)
    assert isinstance(caught.value.__cause__, ValueError)


def test_a_construct_error_names_the_pydantic_model_with_its_arguments(schema: str) -> None:
    with pytest.raises(RowConstructError) as caught:
        run_on(schema, "connection", query[ModelPage[int]](t"SELECT 1::int4 AS id, 'x'::text AS item").one)
    assert caught.value.type_name == "ModelPage[int]"
    assert "ModelPage[int]" in str(caught.value)
    assert isinstance(caught.value.__cause__, ValidationError)


def run_failures(dsn: str, templates: list[Template], *, unchecked: bool = True) -> None:
    async def body(conn: Executor) -> None:
        for template in templates:
            build = query_unchecked if unchecked else query
            with pytest.raises(TypeError):
                await build[int](template).one(conn)
            assert await query[int](t"SELECT 1::int4").one(conn) == 1

    run_on(dsn, "connection", body)


def test_compile_time_failures_leave_the_connection_usable(schema: str) -> None:
    nested = Template("SELECT $1", *[Interpolation(i, "i") for i in range(65536)])
    many = Template(*[Interpolation(i, "i") for i in range(65536)])
    run_failures(
        schema,
        [
            t"SELECT 1 FROM {'a\x00b':ident}",
            t"SELECT 1 FROM {'\x85':ident}",
            t"SELECT 1 FROM {'a\ud800':ident}",
            t"SELECT '{'n':ident}'",
            t"SELECT /* {'n':ident} */ 1",
            Template("SELECT '\x00'"),
            many,
            nested,
            Template(Interpolation("ident", "x", None, EqualStr("ident"))),
        ],
    )


def test_a_runtime_ident_spec_takes_the_ident_path(schema: str) -> None:
    spec = "ident"
    table = "users"

    async def body(conn: Executor) -> int:
        return await query_unchecked[int](t"SELECT count(*)::int4 FROM {table:{spec}}").one(conn)

    assert run_on(schema, "connection", body) >= 1
    run_failures(schema, [t"SELECT '{table:{spec}}'"])


def test_a_query_template_with_nul_or_too_many_parameters_leaves_the_connection_usable(schema: str) -> None:
    run_failures(
        schema, [Template("SELECT '\x00'"), Template(*[Interpolation(i, "i") for i in range(65536)])], unchecked=False
    )


def test_query_unchecked_round_trips_a_quoted_identifier(schema: str) -> None:
    table = 'odd "table" é'
    column = "col; DROP"

    async def body(conn: Executor) -> tuple[int, ...]:
        await query_unchecked(t"CREATE TABLE {table:ident} ({column:ident} int4)").execute(conn)
        await query_unchecked(t"INSERT INTO {table:ident} ({column:ident}) VALUES ({7})").execute(conn)
        return await query_unchecked[tuple[int]](t"SELECT {column:ident} FROM {table:ident}").one(conn)

    assert run_on(schema, "connection", body) == (7,)


def test_an_empty_oidvector_or_int2vector_decodes_as_an_empty_list(schema: str) -> None:
    async def body(conn: Executor) -> tuple[list[int], list[int], list[int], list[int]]:
        return await query_unchecked[tuple[list[int], list[int], list[int], list[int]]](
            t"SELECT ''::oidvector, ''::int2vector, "
            t"(SELECT proargtypes FROM pg_catalog.pg_proc WHERE proname = 'now' LIMIT 1), '1 2'::oidvector"
        ).one(conn)

    assert run_on(schema, "connection", body) == ([], [], [], [1, 2])


def run_with_conn_context[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def test_scalar_one(schema: str) -> None:
    assert run_with_conn_context(schema, query[int](t"SELECT 1").one) == 1


def test_tuple_one(schema: str) -> None:
    assert run_with_conn_context(schema, query[tuple[int, str]](t"SELECT 1, 'a'").one) == (1, "a")


def test_all(schema: str) -> None:
    q = query[int](t"SELECT x FROM generate_series(1, 3) x")
    assert run_with_conn_context(schema, q.all) == [1, 2, 3]


def test_no_rows_raises_row_not_found(schema: str) -> None:
    with pytest.raises(RowNotFound):
        run_with_conn_context(schema, query[int](t"SELECT 1 WHERE false").one)


def test_str_argument_is_type_error() -> None:
    with pytest.raises(TypeError):
        query("SELECT 1")  # ty: ignore[invalid-argument-type]


def test_fetch_without_row_type_is_interface_error(schema: str) -> None:
    with pytest.raises(InterfaceError, match="without a row type"):
        run_with_conn_context(schema, query(t"SELECT 1").one)


def test_execute_returns_row_count(schema: str) -> None:
    assert run_with_conn_context(schema, query(t"SELECT 1").execute) == QueryResult(rows_affected=1)


def test_execute_update_rows_affected(schema: str) -> None:
    q = query(t"UPDATE users SET name = name WHERE id > {0}")
    assert run_with_conn_context(schema, q.execute).rows_affected == 3


def test_execute_ddl_rows_affected_is_zero(schema: str) -> None:
    assert run_with_conn_context(schema, query(t"CREATE TEMP TABLE rowx_ddl (id int)").execute).rows_affected == 0


def test_optional_no_rows_is_none(schema: str) -> None:
    assert run_with_conn_context(schema, query[int](t"SELECT 1 WHERE false").optional) is None


def test_optional_many_rows_returns_first(schema: str) -> None:
    q = query[int](t"SELECT x FROM generate_series(1, 3) x ORDER BY x")
    assert run_with_conn_context(schema, q.optional) == 1


def test_optional_without_row_type_is_interface_error(schema: str) -> None:
    with pytest.raises(InterfaceError, match="without a row type"):
        run_with_conn_context(schema, query(t"SELECT 1").optional)


def test_orig_class_is_present() -> None:
    q = query[int](t"SELECT 1")
    assert q.__orig_class__.__args__ == (int,)  # ty: ignore[unresolved-attribute]


def test_methods_are_coroutine_functions() -> None:
    assert inspect.iscoroutinefunction(query.one)
    assert inspect.iscoroutinefunction(query.optional)
    assert inspect.iscoroutinefunction(query.all)
    assert inspect.iscoroutinefunction(query.execute)


async def fetch(  # ruff: ignore[too-many-arguments]
    conn: ConnectionImpl,
    sql: str,
    *,
    params: list[object] | None = None,
    mask: list[bool] | None = None,
    scalar: bool = False,
    limit_one: bool = False,
    arity: int | None = None,
) -> list[object]:
    return await conn._fetch(sql, params or [], mask, scalar, limit_one, arity, None)


async def execute(conn: ConnectionImpl, sql: str) -> int:
    return await conn._execute(sql, [])


def run_with_impl[T](dsn: str, body: Callable[[ConnectionImpl], Awaitable[T]]) -> T:
    async def run() -> T:
        conn = await connect(dsn)
        try:
            return await body(conn._impl)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_fetch_rows(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, "SELECT 1, 'a'", mask=[False, False], arity=2)

    assert run_with_impl(schema, body) == [(1, "a")]


def test_fetch_scalar(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, "SELECT x FROM generate_series(1, 3) x", mask=[False], scalar=True, arity=1)

    assert run_with_impl(schema, body) == [1, 2, 3]


def test_fetch_decodes_supported_types(schema: str) -> None:
    sql = "SELECT true, 2::int2, 4::int4, 8::int8, 'v'::varchar, 't'::text, NULL::int4"

    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, sql, mask=[False, False, False, False, False, False, True], arity=7)

    [row] = run_with_impl(schema, body)
    assert row == (True, 2, 4, 8, "v", "t", None)
    assert isinstance(row, tuple)
    assert type(row[0]) is bool


def test_fetch_void_is_none(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, "SELECT pg_sleep(0)", scalar=True, arity=1)

    assert run_with_impl(schema, body) == [None]


def test_limit_one_leaves_connection_usable(schema: str) -> None:
    async def body(c: ConnectionImpl) -> tuple[list[object], list[object]]:
        first = await fetch(
            c,
            "SELECT * FROM generate_series(1, 100000)",
            mask=[False],
            scalar=True,
            limit_one=True,
            arity=1,
        )
        second = await fetch(c, "SELECT 2", mask=[False], scalar=True, arity=1)
        return first, second

    assert run_with_impl(schema, body) == ([1], [2])


def test_arity_mismatch_without_rows(schema: str) -> None:
    async def body(c: ConnectionImpl) -> None:
        await fetch(c, "SELECT 1 WHERE false", arity=2)

    with pytest.raises(RowShapeError):
        run_with_impl(schema, body)


def test_null_in_non_nullable_position(schema: str) -> None:
    async def body(c: ConnectionImpl) -> None:
        await fetch(c, "SELECT NULL::int4", mask=[False], arity=1)

    with pytest.raises(RowShapeError, match="index 0"):
        run_with_impl(schema, body)


def test_unsupported_type_is_decode_error(schema: str) -> None:
    async def body(c: ConnectionImpl) -> tuple[bool, list[object]]:
        with pytest.raises(DecodeError, match="xml"):
            await fetch(c, "SELECT '<a/>'::xml", scalar=True, arity=1)
        # a decode failure still releases the connection
        return True, await fetch(c, "SELECT 1", scalar=True, arity=1)

    assert run_with_impl(schema, body) == (True, [1])


def test_execute_row_counts(schema: str) -> None:
    async def body(c: ConnectionImpl) -> tuple[int, int]:
        created = await execute(c, "CREATE TEMP TABLE x(i int)")
        inserted = await execute(c, "INSERT INTO x SELECT generate_series(1, 3)")
        return created, inserted

    assert run_with_impl(schema, body) == (0, 3)


def test_params_are_bound(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return await fetch(c, "SELECT $1::int4", params=[1], scalar=True, arity=1)

    assert run_with_impl(schema, body) == [1]


def test_sequential_awaits_never_see_busy(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        return [await fetch(c, "SELECT 1", scalar=True, arity=1) for _ in range(50)]

    assert len(run_with_impl(schema, body)) == 50


def test_closed_connection_is_interface_error(schema: str) -> None:
    async def run() -> None:
        conn = await connect(schema)
        await conn.close()
        await fetch(conn._impl, "SELECT 1", scalar=True, arity=1)

    with pytest.raises(InterfaceError):
        asyncio.run(run())


BIG: Final[int] = 1_000_000


def numbers(count: int) -> query[int]:
    return query[int](t"SELECT g FROM generate_series(1, {count}) g")


async def collect_into[T](rows: RowStream[T], into: list[T]) -> None:
    async for row in rows:
        into.append(row)  # ruff: ignore[manual-list-comprehension]


async def alive(conn: Connection) -> int:
    return await query[int](t"SELECT 1").one(conn)


def run_with_open_conn(schema: str, body: Callable[[Connection], Coroutine[object, object, object]]) -> object:
    async def run() -> object:
        conn = await connect(schema)
        try:
            return await body(conn)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_full_stream_yields_every_row_in_order(schema: str) -> None:
    async def body(conn: Connection) -> list[int]:
        return [row async for row in numbers(5000).fetch(conn, batch_size=64)]

    assert run_with_open_conn(schema, body) == list(range(1, 5001))


def test_result_is_none_midstream_and_rows_affected_when_exhausted(schema: str) -> None:
    async def body(conn: Connection) -> tuple[QueryResult | None, QueryResult | None]:
        stream = numbers(10).fetch(conn, batch_size=2)
        assert stream.result is None
        await anext(stream)
        midway = stream.result
        async for _ in stream:
            pass
        return midway, stream.result

    midway, final = run_with_open_conn(schema, body)  # ty: ignore[not-iterable]
    assert midway is None
    assert final == QueryResult(rows_affected=10)


def test_break_then_next_query_succeeds_and_result_stays_none(schema: str) -> None:
    async def body(conn: Connection) -> tuple[int, QueryResult | None]:
        # a stream that stays referenced after the break would keep the connection busy
        async for _ in numbers(BIG).fetch(conn):
            break
        ok = await alive(conn)
        async with numbers(BIG).fetch(conn) as rows:
            async for _ in rows:
                break
        return ok, rows.result

    assert run_with_open_conn(schema, body) == (1, None)


def test_aclose_releases_connection(schema: str) -> None:
    async def body(conn: Connection) -> int:
        stream = numbers(BIG).fetch(conn)
        await anext(stream)
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return await alive(conn)

    assert run_with_open_conn(schema, body) == 1


def test_async_with_releases_connection(schema: str) -> None:
    async def body(conn: Connection) -> tuple[int, QueryResult | None]:
        async with numbers(BIG).fetch(conn) as rows:
            await anext(rows)
        return await alive(conn), rows.result

    assert run_with_open_conn(schema, body) == (1, None)


def test_gc_of_unexhausted_stream_releases_connection(schema: str) -> None:
    async def body(conn: Connection) -> int:
        stream = numbers(BIG).fetch(conn)
        await anext(stream)
        del stream
        gc.collect()
        return await alive(conn)

    assert run_with_open_conn(schema, body) == 1


def test_second_query_while_streaming_raises_busy(schema: str) -> None:
    async def body(conn: Connection) -> int:
        async with numbers(BIG).fetch(conn) as rows:
            await anext(rows)
            with pytest.raises(ConnectionBusyError):
                await alive(conn)
            with pytest.raises(ConnectionBusyError):
                await anext(numbers(3).fetch(conn))
        return await alive(conn)

    assert run_with_open_conn(schema, body) == 1


def test_close_while_streaming_then_anext_raises_interface_error(schema: str) -> None:
    async def body(conn: Connection) -> None:
        rows = numbers(BIG).fetch(conn, batch_size=1)
        await anext(rows)
        await conn.close()
        with pytest.raises(InterfaceError):
            await anext(rows)

    run_with_open_conn(schema, body)


def test_close_while_rows_are_buffered_then_anext_raises_interface_error(schema: str) -> None:
    async def body(conn: Connection) -> None:
        rows = numbers(10).fetch(conn)
        await anext(rows)
        await conn.close()
        with pytest.raises(InterfaceError):
            await anext(rows)

    run_with_open_conn(schema, body)


def test_null_in_non_nullable_column_yields_prior_rows_then_row_shape_error_and_idle(schema: str) -> None:
    async def body(conn: Connection) -> tuple[list[int], int]:
        q = query[int](t"SELECT CASE WHEN g = 3 THEN NULL ELSE g END FROM generate_series(1, 5) g")
        seen: list[int] = []
        with pytest.raises(RowShapeError):
            await collect_into(q.fetch(conn), seen)
        return seen, await alive(conn)

    assert run_with_open_conn(schema, body) == ([1, 2], 1)


def test_decode_error_yields_prior_rows_then_decode_error_and_idle(schema: str) -> None:
    async def body(conn: Connection) -> tuple[list[date], int]:
        q = query[date](
            t"SELECT CASE WHEN g = 3 THEN 'infinity'::date ELSE '2020-01-01'::date END FROM generate_series(1, 5) g",
        )
        seen: list[date] = []
        with pytest.raises(DecodeError):
            await collect_into(q.fetch(conn), seen)
        return seen, await alive(conn)

    seen, ok = run_with_open_conn(schema, body)  # ty: ignore[not-iterable]
    assert seen == [date(2020, 1, 1)] * 2
    assert ok == 1


def test_batch_size_one_yields_one_row_per_trip(schema: str) -> None:
    async def body(conn: Connection) -> list[int]:
        impl = conn._impl._stream("SELECT g FROM generate_series(1, 20) g", [], [False], True, 1, 1, None)  # ruff: ignore[boolean-positional-value-in-call]
        sizes: list[int] = []
        while batch := await impl._next():
            sizes.append(len(batch))
        assert impl.rows_affected == 20
        return sizes

    assert run_with_open_conn(schema, body) == [1] * 20


def test_batch_size_zero_raises_value_error(schema: str) -> None:
    async def body(conn: Connection) -> None:  # ruff: ignore[unused-async]
        with pytest.raises(ValueError, match="batch_size"):
            numbers(3).fetch(conn, batch_size=0)

    run_with_open_conn(schema, body)


def test_fetch_on_unit_query_raises_interface_error_from_call(schema: str) -> None:
    async def body(conn: Connection) -> None:  # ruff: ignore[unused-async]
        with pytest.raises(InterfaceError):
            query(t"SELECT 1").fetch(conn)

    run_with_open_conn(schema, body)


def test_arity_and_encode_errors_raise_from_first_anext(schema: str) -> None:
    async def body(conn: Connection) -> int:
        wrong = query[tuple[int, int]](t"SELECT 1").fetch(conn)
        with pytest.raises(RowShapeError):
            await anext(wrong)
        bad = "not a number"
        encoding = query[int](t"SELECT {bad}::int4").fetch(conn)
        with pytest.raises(EncodeError):
            await anext(encoding)
        return await alive(conn)

    assert run_with_open_conn(schema, body) == 1


def test_anext_is_a_coroutine_function_and_works_in_create_task(schema: str) -> None:
    assert inspect.iscoroutinefunction(RowStream.__anext__)

    async def body(conn: Connection) -> int:
        stream = numbers(3).fetch(conn)
        return await asyncio.create_task(anext(stream))

    assert run_with_open_conn(schema, body) == 1


type ExampleUserRow = tuple[int, str, str | None]


def test_design_example(schema: str) -> None:
    async def main() -> None:
        async with await connect(schema) as conn:
            uid = await query[int](t"SELECT id FROM users WHERE name = {'alice'}").one(conn)
            user = await query[ExampleUserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").one(conn)
            assert user == (uid, "alice", "alice@example.com")

            names = await query[str](t"SELECT name FROM users ORDER BY name").all(conn)
            assert names == ["alice", "bob", "carol"]

            ids = [uid, uid + 1, uid + 2]
            n = await query(t"UPDATE users SET name = {'x'} WHERE id = ANY({ids})").execute(conn)
            assert n == QueryResult(rows_affected=3)

            total = await query[Decimal](t'SELECT sum(amount) AS "total!" FROM orders').one(conn)
            assert total == Decimal("110.49")

    asyncio.run(main())


def test_execute_on_typed_query_discards_rows(schema: str) -> None:
    async def body() -> QueryResult:
        async with await connect(schema) as conn:
            return await query[tuple[int, str]](t"SELECT id, name FROM users").execute(conn)

    assert asyncio.run(body()) == QueryResult(rows_affected=3)


def test_query_unchecked_nested_template_and_ident(schema: str) -> None:
    async def body() -> list[str]:
        table, column = "users", "name"
        cond = t"WHERE {'id':ident} > {0}"
        async with await connect(schema) as conn:
            q = query_unchecked[str](t"SELECT {column:ident} FROM {table:ident} {cond} ORDER BY 1")
            return await q.all(conn)

    assert asyncio.run(body()) == ["alice", "bob", "carol"]


def test_query_unchecked_qualified_ident(schema: str) -> None:
    async def body() -> int:
        async with await connect(schema) as conn:
            return await query_unchecked[int](t"SELECT count(*) FROM {('pg_catalog', 'pg_class'):ident}").one(
                conn,
            )

    assert asyncio.run(body()) > 0
