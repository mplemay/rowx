import asyncio
import datetime as dt
from typing import TYPE_CHECKING, Any, Final

import pytest

from rowx import query
from rowx.errors import DecodeError, EncodeError
from rowx.postgres import Connection, Interval, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

INT4_MAX: Final[int] = 2**31 - 1
INT4_MIN: Final[int] = -(2**31)
INT8_MAX: Final[int] = 2**63 - 1
INT8_MIN: Final[int] = -(2**63)
MICROS_PER_HOUR: Final[int] = 3_600_000_000
# the server stores infinity as the extreme of every field, and `Interval` keeps those fields as they are
POSITIVE_INFINITY: Final[Interval] = Interval(months=INT4_MAX, days=INT4_MAX, microseconds=INT8_MAX)
NEGATIVE_INFINITY: Final[Interval] = Interval(months=INT4_MIN, days=INT4_MIN, microseconds=INT8_MIN)
# the driver's per-column flag byte: bit 1 reads the column as an `Interval`
SPAN: Final[int] = 0b10


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def read(dsn: str, sql: str, params: list[object] | None = None, flags: int | None = None) -> Any:  # ruff: ignore[any-type]
    mask = None if flags is None else [flags]

    async def run(conn: Connection) -> Any:  # ruff: ignore[any-type]
        return (await conn._impl._fetch(sql, params or [], mask, scalar=True, limit_one=True, arity=1))[0]

    return run_with_conn(dsn, run)


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("0", Interval()),
        ("1 mon", Interval(months=1)),
        ("1 year", Interval(months=12)),
        ("-1 mon", Interval(months=-1)),
        (
            "1 year 2 mons 3 days 04:05:06.000007",
            Interval(months=14, days=3, microseconds=4 * MICROS_PER_HOUR + 5 * 60_000_000 + 6_000_007),
        ),
        ("-1 year -2 days -00:00:00.5", Interval(months=-12, days=-2, microseconds=-500_000)),
        ("1000000 hours", Interval(microseconds=1_000_000 * MICROS_PER_HOUR)),
        ("178000000 years", Interval(months=178_000_000 * 12)),
        ("infinity", POSITIVE_INFINITY),
        ("-infinity", NEGATIVE_INFINITY),
    ],
)
def test_interval_leaf_decodes_every_field(schema: str, literal: str, expected: Interval) -> None:
    assert read(schema, f"SELECT '{literal}'::interval", flags=SPAN) == expected


def test_months_and_days_are_not_folded_into_each_other(schema: str) -> None:
    got = read(schema, "SELECT '1 mon 30 days'::interval", flags=SPAN)
    assert (got.months, got.days, got.microseconds) == (1, 30, 0)
    assert got != read(schema, "SELECT '30 days'::interval", flags=SPAN)


@pytest.mark.parametrize("literal", ["1 mon", "1 year", "-178000000 years", "1 mon 1 day", "infinity", "-infinity"])
def test_timedelta_leaf_still_raises_on_months_and_infinity(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="text"):
        read(schema, f"SELECT '{literal}'::interval")


def test_timedelta_leaf_is_the_default_for_finite_day_time_intervals(schema: str) -> None:
    assert read(schema, "SELECT '1 day 02:00:00'::interval") == dt.timedelta(days=1, hours=2)


@pytest.mark.parametrize(
    "value",
    [
        Interval(),
        Interval(months=1),
        Interval(months=-14, days=3, microseconds=-7),
        Interval(months=INT4_MAX - 1, days=INT4_MIN + 1, microseconds=INT8_MAX - 1),
        POSITIVE_INFINITY,
        NEGATIVE_INFINITY,
    ],
)
def test_interval_round_trips(schema: str, value: Interval) -> None:
    assert read(schema, "SELECT $1::interval", [value], flags=SPAN) == value


def test_infinite_interval_is_infinite_on_the_server(schema: str) -> None:
    assert read(schema, "SELECT $1::interval::text", [POSITIVE_INFINITY]) == "infinity"
    assert read(schema, "SELECT $1::interval::text", [NEGATIVE_INFINITY]) == "-infinity"
    assert read(schema, "SELECT isfinite($1::interval)", [Interval(months=1)]) is True
    assert read(schema, "SELECT isfinite($1::interval)", [POSITIVE_INFINITY]) is False


def test_server_reads_the_fields_we_send(schema: str) -> None:
    value = Interval(months=14, days=3, microseconds=4 * MICROS_PER_HOUR)
    assert read(schema, "SELECT $1::interval::text", [value]) == "1 year 2 mons 3 days 04:00:00"
    assert (
        read(schema, "SELECT (date '2024-01-31' + $1::interval)::text", [Interval(months=1)]) == "2024-02-29 00:00:00"
    )


def test_timedelta_is_still_accepted_for_an_interval_parameter(schema: str) -> None:
    delta = dt.timedelta(days=-1, seconds=5, microseconds=7)
    assert read(schema, "SELECT $1::interval", [delta]) == delta
    assert read(schema, "SELECT $1::interval", [delta], flags=SPAN) == Interval(days=-1, microseconds=5_000_007)


@pytest.mark.parametrize(
    "value",
    [
        Interval(months=INT4_MAX + 1),
        Interval(months=INT4_MIN - 1),
        Interval(days=INT4_MAX + 1),
        Interval(days=INT4_MIN - 1),
    ],
)
def test_months_and_days_must_fit_an_int4(schema: str, value: Interval) -> None:
    with pytest.raises(EncodeError, match="out of range"):
        read(schema, "SELECT $1::interval", [value])


@pytest.mark.parametrize("value", [Interval(microseconds=INT8_MAX + 1), Interval(months=INT8_MIN - 1)])
def test_a_field_beyond_int8_is_an_encode_error(schema: str, value: Interval) -> None:
    with pytest.raises(EncodeError, match="int8"):
        read(schema, "SELECT $1::interval", [value])


def test_interval_is_not_accepted_for_other_parameters(schema: str) -> None:
    with pytest.raises(EncodeError, match="not accepted for int4"):
        read(schema, "SELECT $1::int4", [Interval()])
    with pytest.raises(EncodeError, match="Interval"):
        read(schema, "SELECT $1::json", [Interval()])


def test_interval_arrays_follow_the_leaf(schema: str) -> None:
    sql = 'SELECT \'{"1 mon",NULL,"2 days"}\'::interval[]'
    assert read(schema, sql, flags=SPAN) == [Interval(months=1), None, Interval(days=2)]
    with pytest.raises(DecodeError):
        read(schema, sql)
    plain = "SELECT '{\"1 day\",NULL}'::interval[]"
    assert read(schema, plain) == [dt.timedelta(days=1), None]


def test_interval_arrays_round_trip_with_nesting_and_nulls(schema: str) -> None:
    value = [[Interval(months=1), None], [Interval(days=-1), POSITIVE_INFINITY]]
    assert read(schema, "SELECT $1::interval[]", [value], flags=SPAN) == value


def test_only_annotated_columns_use_the_interval_leaf(schema: str) -> None:
    async def body(conn: Connection) -> list[object]:
        sql = "SELECT '1 mon'::interval, '1 day'::interval, '2 mons'::interval"
        return await conn._impl._fetch(sql, [], [SPAN, 0, SPAN | 1], scalar=False, limit_one=True, arity=3)

    assert run_with_conn(schema, body) == [(Interval(months=1), dt.timedelta(days=1), Interval(months=2))]


def test_interval_is_read_through_a_query(schema: str) -> None:
    async def body(conn: Connection) -> tuple[object, ...]:
        one = await query[Interval](t"SELECT {Interval(months=3)}::interval").one(conn)
        none = await query[Interval | None](t"SELECT NULL::interval").one(conn)
        mixed = await query[tuple[Interval, dt.timedelta | None]](
            t"SELECT '1 year'::interval, '2 days'::interval",
        ).one(conn)
        many = await query[list[Interval | None]](t"SELECT ARRAY['1 mon', NULL]::interval[]").one(conn)
        return one, none, mixed, many

    assert run_with_conn(schema, body) == (
        Interval(months=3),
        None,
        (Interval(months=12), dt.timedelta(days=2)),
        [Interval(months=1), None],
    )


def test_interval_leaf_streams(schema: str) -> None:
    async def body(conn: Connection) -> list[Interval]:
        stream = query[Interval](t"SELECT (g || ' mons')::interval FROM generate_series(1, 5) g").fetch(
            conn, batch_size=2
        )
        return [row async for row in stream]

    assert run_with_conn(schema, body) == [Interval(months=n) for n in range(1, 6)]
