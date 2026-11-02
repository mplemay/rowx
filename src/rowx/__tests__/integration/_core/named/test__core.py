import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

import pytest

from rowx._core.postgres import Connection as ConnectionImpl, RowShape
from rowx.errors import DecodeError, RowConstructError, RowShapeError
from rowx.postgres import Connection, Interval, Pool, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

NULLABLE: Final = 1
SPAN: Final = 2

type Field = tuple[str, tuple[str, ...] | None, bool, int]


def spec(
    key: str, *, names: tuple[str, ...] | None = None, unknown: bool = False, required: bool = True, flags: int = 0
) -> Field:
    return key, None if unknown else (names or (key,)), required, flags


def shape(  # ruff: ignore[too-many-arguments]
    ctor: object,
    form: str,
    fields: list[Field],
    *,
    matching: str = "by_name",
    validate: bool = True,
    name: str = "Row",
) -> RowShape:
    return RowShape(ctor, form, matching, name, fields, validate=validate)  # ty: ignore[invalid-argument-type]


def kwargs_of(**kw: object) -> dict[str, object]:
    return kw


def args_of(*args: object) -> tuple[object, ...]:
    return args


@dataclass(slots=True)
class Validator:
    seen: list[dict[str, object]] = field(default_factory=list)

    def validate_python(self, row: dict[str, object]) -> dict[str, object]:
        self.seen.append(row)
        return {"validated": row}


class Plain:
    def __init__(self, **kw: object) -> None:
        self.kw = kw


class Stop(BaseException):
    pass


def raising_on(bad: set[int], exc: type[BaseException] = ValueError) -> Callable[..., object]:
    def ctor(**kw: int) -> dict[str, int]:
        if kw["n"] in bad:
            msg = "boom"
            raise exc(msg)
        return kw

    return ctor


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


async def fetch(conn: Connection, sql: str, row_shape: RowShape, *, limit_one: bool = False) -> list[object]:
    return await conn._impl._fetch(sql, [], None, False, limit_one, None, row_shape)  # ruff: ignore[boolean-positional-value-in-call]


def stream(conn: ConnectionImpl, sql: str, row_shape: RowShape, batch_size: int = 1024) -> object:
    return conn._stream(sql, [], None, False, None, batch_size, row_shape)  # ruff: ignore[boolean-positional-value-in-call]


async def drain(handle: object) -> tuple[list[object], BaseException | None]:
    rows: list[object] = []
    try:
        while batch := await handle._next():  # ty: ignore[unresolved-attribute]
            rows.extend(batch)
    except BaseException as err:  # ruff: ignore[blind-except]
        return rows, err
    return rows, None


def test_keyword_form_matches_by_name_when_the_column_order_differs(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("a"), spec("b")])

    async def body(conn: Connection) -> list[object]:
        return await fetch(conn, "SELECT 2::int4 AS b, 'x'::text AS a", row_shape)

    assert run_with_conn(schema, body) == [{"a": "x", "b": 2}]


def test_keyword_form_leaves_out_an_absent_optional_field(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("a"), spec("b", required=False)])

    async def body(conn: Connection) -> list[object]:
        return await fetch(conn, "SELECT 'x'::text AS a", row_shape)

    assert run_with_conn(schema, body) == [{"a": "x"}]


def test_positional_form_orders_the_arguments_by_field(schema: str) -> None:
    row_shape = shape(
        args_of,
        "positional",
        [spec("id"), spec("name"), spec("note", required=False)],
        matching="positional",
    )

    async def body(conn: Connection) -> tuple[list[object], list[object]]:
        two = await fetch(conn, "SELECT 1::int4 AS id, 'n'::text AS name", row_shape)
        three = await fetch(conn, "SELECT 1::int4 AS id, 'n'::text AS name, 'z'::text AS note", row_shape)
        return two, three

    assert run_with_conn(schema, body) == ([(1, "n")], [(1, "n", "z")])


def test_dict_form_is_the_dict_itself_without_absent_keys(schema: str) -> None:
    row_shape = shape(None, "dict", [spec("a"), spec("b", required=False)])

    async def body(conn: Connection) -> tuple[list[object], list[object]]:
        both = await fetch(conn, "SELECT 2::int4 AS b, 1::int4 AS a", row_shape)
        one = await fetch(conn, "SELECT 1::int4 AS a", row_shape)
        return both, one

    assert run_with_conn(schema, body) == ([{"a": 1, "b": 2}], [{"a": 1}])


def test_validator_form_keys_by_column_name_with_pass_through_and_last_duplicate(schema: str) -> None:
    validator = Validator()
    # a field with unknown names turns the by-name rules off, so every column reaches the dict
    row_shape = shape(
        validator, "validator", [spec("id", names=("user_id",)), spec("any", unknown=True, required=False)]
    )

    async def body(conn: Connection) -> list[object]:
        return await fetch(
            conn, "SELECT 5::int4 AS user_id, 1::int4 AS extra, 2::int4 AS extra, 'k'::text AS \"u!\"", row_shape
        )

    assert run_with_conn(schema, body) == [{"validated": {"user_id": 5, "extra": 2, "u": "k"}}]
    assert validator.seen == [{"user_id": 5, "extra": 2, "u": "k"}]


def test_validator_form_can_call_the_class_with_the_dict_as_keywords(schema: str) -> None:
    row_shape = shape(Plain, "validator", [spec("a"), spec("b")], validate=False)

    async def body(conn: Connection) -> list[object]:
        return await fetch(conn, "SELECT 2::int4 AS b, 1::int4 AS a", row_shape)

    (row,) = run_with_conn(schema, body)
    assert isinstance(row, Plain)
    assert row.kw == {"a": 1, "b": 2}


def test_interval_flag_reads_a_lossless_interval_for_the_mapped_field(schema: str) -> None:
    sql = "SELECT interval '1 month 2 days' AS i, interval '3 hours' AS t"
    plain = shape(kwargs_of, "keyword", [spec("t"), spec("i", flags=SPAN)])

    async def body(conn: Connection) -> list[object]:
        return await fetch(conn, sql, plain)

    (found,) = run_with_conn(schema, body)
    row = cast("dict[str, object]", found)
    assert isinstance(row["i"], Interval)
    # the other interval field has no flag, so it decodes as a timedelta
    assert not isinstance(row["t"], Interval)

    async def without(conn: Connection) -> list[object]:
        return await fetch(conn, sql, shape(kwargs_of, "keyword", [spec("t"), spec("i")]))

    with pytest.raises(DecodeError):
        run_with_conn(schema, without)


def test_null_in_a_non_nullable_field_names_the_field_and_column(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("b"), spec("a", flags=NULLABLE)])

    async def body(conn: Connection) -> list[object]:
        return await fetch(conn, "SELECT 1::int4 AS a, NULL::int4 AS b", row_shape)

    with pytest.raises(RowShapeError) as caught:
        run_with_conn(schema, body)
    assert str(caught.value) == 'NULL in column 2 "b" for non-nullable field "b" of `Row` (row 0)'


def test_null_in_a_stream_names_its_row_after_the_rows_before_it(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("n")], name="User")
    sql = "SELECT CASE WHEN g = 4 THEN NULL ELSE g END::int4 AS n FROM generate_series(1, 5) g"

    async def body(conn: Connection) -> tuple[list[object], BaseException | None]:
        return await drain(stream(conn._impl, sql, row_shape, 2))

    rows, err = run_with_conn(schema, body)
    assert rows == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert isinstance(err, RowShapeError)
    assert str(err) == 'NULL in column 1 "n" for non-nullable field "n" of `User` (row 3)'


def test_mapping_error_on_an_empty_result_is_exact(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("a"), spec("b")], name="User")

    async def body(conn: Connection) -> list[object]:
        return await fetch(conn, "SELECT 1::int4 AS a, 2::int4 AS c WHERE false", row_shape)

    with pytest.raises(RowShapeError) as caught:
        run_with_conn(schema, body)
    assert str(caught.value) == 'User: column 2 "c" matches no field of `User`; field "b" of `User` has no column'


@pytest.mark.parametrize(
    ("sql", "fields", "kwargs", "message"),
    [
        (
            "SELECT 1 AS id, 2 AS id",
            [spec("id")],
            {},
            'M: columns 1 and 2 are both named "id"',
        ),
        (
            "SELECT 1, 2",
            [spec("id")],
            {},
            'M: columns 1 and 2 are both unnamed (`?column?`); field "id" of `M` has no column',
        ),
        (
            "SELECT 1 AS id, 2 AS user_id",
            [spec("id", names=("id", "user_id"))],
            {},
            'M: columns 1 "id" and 2 "user_id" both map to field "id" of `M`',
        ),
        (
            "SELECT 1 AS y",
            [spec("x", names=("y",)), spec("y")],
            {},
            'M: column 1 "y" matches fields "x" and "y" of `M`',
        ),
        (
            "SELECT 1 AS login, 2 AS id",
            [spec("id"), spec("email")],
            {"matching": "positional"},
            'M: column 1 "login" faces field "id" of `M` (position 1); column 2 "id" faces field "email" of `M` (position 2)',
        ),
        (
            "SELECT 1 AS a, 2 AS b, 3 AS c, 4 AS d",
            [spec("a"), spec("b", required=False)],
            {"matching": "positional"},
            "M: the query returns 4 columns but `M` takes between 1 and 2",
        ),
    ],
)
def test_mapping_messages(schema: str, sql: str, fields: list[Field], kwargs: dict[str, object], message: str) -> None:
    matching = str(kwargs.get("matching", "by_name"))
    row_shape = shape(
        kwargs_of, "keyword" if matching == "by_name" else "positional", fields, matching=matching, name="M"
    )

    async def body(conn: Connection) -> list[object]:
        return await fetch(conn, sql, row_shape)

    with pytest.raises(RowShapeError) as caught:
        run_with_conn(schema, body)
    assert str(caught.value) == message


def test_constructor_error_in_all_and_one_is_wrapped_with_its_cause(schema: str) -> None:
    row_shape = shape(raising_on({2}), "keyword", [spec("n")], name="Num")
    sql = "SELECT g::int4 AS n FROM generate_series(1, 4) g"

    async def body(conn: Connection) -> tuple[RowConstructError, list[object]]:
        with pytest.raises(RowConstructError) as caught:
            await fetch(conn, sql, row_shape)
        # `one` and `optional` stop at the first row, so a failure there is row 0
        first = shape(raising_on({1}), "keyword", [spec("n")], name="Num")
        with pytest.raises(RowConstructError) as one:
            await fetch(conn, sql, first, limit_one=True)
        assert one.value.row_index == 0
        return caught.value, await fetch(conn, "SELECT 1::int4 AS n", shape(kwargs_of, "keyword", [spec("n")]))

    err, after = run_with_conn(schema, body)
    assert isinstance(err, DecodeError)
    assert (err.type_name, err.row_index) == ("Num", 1)
    assert str(err) == "Num row 1: ValueError('boom')"
    assert isinstance(err.__cause__, ValueError)
    assert after == [{"n": 1}]


@pytest.mark.parametrize(("bad", "yielded"), [(2, 2), (3, 3), (0, 0), (1, 1)])
def test_constructor_error_in_a_stream_yields_earlier_rows_across_batches(schema: str, bad: int, yielded: int) -> None:
    row_shape = shape(raising_on({bad + 1}), "keyword", [spec("n")], name="Num")
    sql = "SELECT g::int4 AS n FROM generate_series(1, 5) g"

    async def body(conn: Connection) -> tuple[list[object], BaseException | None, list[object]]:
        rows, err = await drain(stream(conn._impl, sql, row_shape, 2))
        return rows, err, await fetch(conn, "SELECT 7::int4 AS n", shape(kwargs_of, "keyword", [spec("n")]))

    rows, err, after = run_with_conn(schema, body)
    assert rows == [{"n": n} for n in range(1, yielded + 1)]
    assert isinstance(err, RowConstructError)
    assert err.row_index == bad
    assert isinstance(err.__cause__, ValueError)
    assert after == [{"n": 7}]


def test_base_exception_from_the_constructor_passes_through(schema: str) -> None:
    row_shape = shape(raising_on({1}, Stop), "keyword", [spec("n")])

    async def body(conn: Connection) -> BaseException | None:
        with pytest.raises(Stop):
            await fetch(conn, "SELECT 1::int4 AS n", row_shape)
        _, err = await drain(stream(conn._impl, "SELECT 1::int4 AS n", row_shape))
        return err

    assert isinstance(run_with_conn(schema, body), Stop)


def test_pool_fetch_and_stream_build_named_rows(schema: str) -> None:
    good = shape(kwargs_of, "keyword", [spec("n")])
    bad = shape(raising_on({3}), "keyword", [spec("n")], name="Num")
    sql = "SELECT g::int4 AS n FROM generate_series(1, 4) g"

    async def run() -> tuple[list[object], list[object], BaseException | None, list[object]]:
        async with Pool(schema, max_size=1) as pool:
            rows = await pool._fetch(sql, [], None, False, False, None, good)  # ruff: ignore[boolean-positional-value-in-call]
            streamed, _ = await drain(pool._stream(sql, [], None, False, None, 2, good))  # ruff: ignore[boolean-positional-value-in-call]
            failed, err = await drain(pool._stream(sql, [], None, False, None, 2, bad))  # ruff: ignore[boolean-positional-value-in-call]
            with pytest.raises(RowConstructError):
                await pool._fetch(sql, [], None, False, False, None, bad)  # ruff: ignore[boolean-positional-value-in-call]
            return rows, streamed, err, failed

    rows, streamed, err, failed = asyncio.run(run())
    assert rows == streamed == [{"n": n} for n in range(1, 5)]
    assert failed == [{"n": 1}, {"n": 2}]
    assert isinstance(err, RowConstructError)
    assert err.row_index == 2


def test_the_mapping_is_rechecked_when_a_table_gains_a_column(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("a")])

    async def body(conn: Connection) -> None:
        await conn._impl._execute("CREATE TABLE named_t (a int4 NOT NULL)", [])
        await conn._impl._execute("INSERT INTO named_t VALUES (1)", [])
        assert await fetch(conn, "SELECT * FROM named_t", row_shape) == [{"a": 1}]
        await conn._impl._execute("ALTER TABLE named_t ADD COLUMN b int4", [])
        with pytest.raises(RowShapeError, match='column 2 "b" matches no field of `Row`'):
            await fetch(conn, "SELECT * FROM named_t", row_shape)

    run_with_conn(schema, body)
