import asyncio
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Final

import pytest

import rowx
from rowx import query
from rowx.errors import EncodeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

EAST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))
WEST: Final[timezone] = timezone(timedelta(hours=-8))


def run_with_conn[T](dsn: str, body: Callable[[rowx.Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await rowx.connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def fetch[T](dsn: str, q: query[T]) -> T:
    return run_with_conn(dsn, q.one)


@pytest.mark.parametrize(
    "value",
    [date(1, 1, 1), date(1999, 12, 31), date(2000, 1, 1), date(2024, 2, 29), date(9999, 12, 31)],
)
def test_date_round_trip(schema: str, value: date) -> None:
    assert fetch(schema, query[date](t"SELECT {value}::date")) == value


@pytest.mark.parametrize("value", [time(0, 0), time(13, 45, 7, 123456), time(23, 59, 59, 999999)])
def test_time_round_trip(schema: str, value: time) -> None:
    assert fetch(schema, query[time](t"SELECT {value}::time")) == value


@pytest.mark.parametrize(
    "value",
    [
        time(13, 45, 7, 123456, tzinfo=UTC),
        time(1, 2, 3, tzinfo=EAST),
        time(23, 0, tzinfo=WEST),
    ],
)
def test_timetz_round_trip(schema: str, value: time) -> None:
    got = fetch(schema, query[time](t"SELECT {value}::timetz"))
    assert got == value
    assert got.utcoffset() == value.utcoffset()


@pytest.mark.parametrize(
    "value",
    [
        datetime(1, 1, 1),  # ruff: ignore[call-datetime-without-tzinfo]
        datetime(1999, 12, 31, 23, 59, 59, 999999),  # ruff: ignore[call-datetime-without-tzinfo]
        datetime(2000, 1, 1),  # ruff: ignore[call-datetime-without-tzinfo]
        datetime(2024, 2, 29, 12, 30, 15, 250000),  # ruff: ignore[call-datetime-without-tzinfo]
        datetime(9999, 12, 31, 23, 59, 59, 999999),  # ruff: ignore[call-datetime-without-tzinfo]
    ],
)
def test_timestamp_round_trip(schema: str, value: datetime) -> None:
    assert fetch(schema, query[datetime](t"SELECT {value}::timestamp")) == value


@pytest.mark.parametrize(
    "value",
    [
        datetime(2000, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 29, 12, 30, 15, 250000, tzinfo=UTC),
        datetime(2024, 6, 1, 9, 0, tzinfo=EAST),
        datetime(2024, 6, 1, 9, 0, tzinfo=WEST),
    ],
)
def test_timestamptz_round_trip(schema: str, value: datetime) -> None:
    got = fetch(schema, query[datetime](t"SELECT {value}::timestamptz"))
    assert got == value
    assert got.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "value",
    [
        timedelta(0),
        timedelta(days=3, seconds=4, microseconds=5),
        timedelta(days=-3, seconds=4, microseconds=5),
        timedelta(microseconds=-1),
        timedelta(days=100000),
    ],
)
def test_interval_round_trip(schema: str, value: timedelta) -> None:
    assert fetch(schema, query[timedelta](t"SELECT {value}::interval")) == value


def test_naive_datetime_to_timestamptz_is_encode_error(schema: str) -> None:
    value = datetime(2024, 1, 1)  # ruff: ignore[call-datetime-without-tzinfo]
    with pytest.raises(EncodeError, match=r"\$1.*naive"):
        fetch(schema, query[datetime](t"SELECT {value}::timestamptz"))


def test_aware_datetime_to_timestamp_is_encode_error(schema: str) -> None:
    value = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(EncodeError, match=r"\$1.*aware"):
        fetch(schema, query[datetime](t"SELECT {value}::timestamp"))


def test_naive_time_to_timetz_is_encode_error(schema: str) -> None:
    value = time(1, 2, 3)
    with pytest.raises(EncodeError, match=r"\$1.*naive"):
        fetch(schema, query[time](t"SELECT {value}::timetz"))


def test_aware_time_to_time_is_encode_error(schema: str) -> None:
    value = time(1, 2, 3, tzinfo=UTC)
    with pytest.raises(EncodeError, match=r"\$1.*aware"):
        fetch(schema, query[time](t"SELECT {value}::time"))


def test_datetime_to_date_is_encode_error(schema: str) -> None:
    value = datetime(2024, 1, 1)  # ruff: ignore[call-datetime-without-tzinfo]
    with pytest.raises(EncodeError, match=r"\$1.*datetime"):
        fetch(schema, query[date](t"SELECT {value}::date"))


def test_str_to_date_is_encode_error(schema: str) -> None:
    value = "2024-01-01"
    with pytest.raises(EncodeError, match=r"\$1.*str"):
        fetch(schema, query[date](t"SELECT {value}::date"))
