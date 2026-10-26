import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

import pytest

from rowx import query
from rowx.errors import DecodeError
from rowx.postgres import Connection, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def fetch[T](dsn: str, q: query[T]) -> T:
    return run_with_conn(dsn, q.one)


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("0001-01-01", date(1, 1, 1)),
        ("9999-12-31", date(9999, 12, 31)),
        ("2000-01-01", date(2000, 1, 1)),
        ("2024-02-29", date(2024, 2, 29)),
        ("1999-12-31", date(1999, 12, 31)),
    ],
)
def test_date(schema: str, literal: str, expected: date) -> None:
    assert fetch(schema, query[date](t"SELECT {literal}::text::date")) == expected


@pytest.mark.parametrize("literal", ["infinity", "-infinity", "4713-01-01 BC", "10000-01-01"])
def test_date_out_of_range_is_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch(schema, query[date](t"SELECT {literal}::text::date"))


def test_time(schema: str) -> None:
    assert fetch(schema, query[time](t"SELECT '13:45:07.123456'::time")) == time(13, 45, 7, 123456)
    assert fetch(schema, query[time](t"SELECT '00:00'::time")) == time(0, 0)


def test_time_24_is_decode_error(schema: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch(schema, query[time](t"SELECT '24:00'::time"))


@pytest.mark.parametrize(
    ("literal", "hours"),
    [("12:00+02", 2), ("12:00-05", -5), ("12:00+00", 0), ("12:00+05:30", 5.5)],
)
def test_timetz_offset_sign(schema: str, literal: str, hours: float) -> None:
    value = fetch(schema, query[time](t"SELECT {literal}::text::timetz"))
    assert value.replace(tzinfo=None) == time(12, 0)
    assert value.utcoffset() == timedelta(hours=hours)


def test_timestamp_is_naive(schema: str) -> None:
    value = fetch(schema, query[datetime](t"SELECT '2024-03-05 06:07:08.000009'::timestamp"))
    assert value == datetime(2024, 3, 5, 6, 7, 8, 9)  # ruff: ignore[call-datetime-without-tzinfo]
    assert value.tzinfo is None


def test_timestamp_before_epoch(schema: str) -> None:
    value = fetch(schema, query[datetime](t"SELECT '1999-12-31 23:59:59.999999'::timestamp"))
    assert value == datetime(1999, 12, 31, 23, 59, 59, 999999)  # ruff: ignore[call-datetime-without-tzinfo]


def test_timestamp_bounds(schema: str) -> None:
    assert fetch(schema, query[datetime](t"SELECT '0001-01-01'::timestamp")) == datetime(1, 1, 1)  # ruff: ignore[call-datetime-without-tzinfo]
    last = fetch(schema, query[datetime](t"SELECT '9999-12-31 23:59:59.999999'::timestamp"))
    assert last == datetime(9999, 12, 31, 23, 59, 59, 999999)  # ruff: ignore[call-datetime-without-tzinfo]


def test_timestamptz_is_utc_aware(schema: str) -> None:
    value = fetch(schema, query[datetime](t"SELECT '2024-03-05 06:07:08+02'::timestamptz"))
    assert value == datetime(2024, 3, 5, 4, 7, 8, tzinfo=UTC)
    assert value.utcoffset() == timedelta(0)


@pytest.mark.parametrize("literal", ["infinity", "-infinity", "10000-01-01 00:00:00"])
def test_timestamp_out_of_range_is_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch(schema, query[datetime](t"SELECT {literal}::text::timestamp"))


@pytest.mark.parametrize("literal", ["infinity", "-infinity", "10000-01-01 00:00:00+00"])
def test_timestamptz_out_of_range_is_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch(schema, query[datetime](t"SELECT {literal}::text::timestamptz"))


def test_interval(schema: str) -> None:
    assert fetch(schema, query[timedelta](t"SELECT interval '1 day 2 hours'")) == timedelta(days=1, hours=2)
    assert fetch(schema, query[timedelta](t"SELECT interval '-3 days 4 microseconds'")) == timedelta(
        days=-3,
        microseconds=4,
    )
    assert fetch(schema, query[timedelta](t"SELECT interval '0'")) == timedelta(0)


@pytest.mark.parametrize("literal", ["1 month", "1 year", "infinity", "-infinity"])
def test_interval_months_or_infinity_is_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch(schema, query[timedelta](t"SELECT {literal}::text::interval"))
