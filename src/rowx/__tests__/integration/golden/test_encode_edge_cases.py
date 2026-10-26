import asyncio
import datetime as dt
from decimal import Decimal
from math import isnan
from typing import Any, Final

import pytest

from rowx.errors import DatabaseError, EncodeError
from rowx.postgres import connect

pytestmark = [pytest.mark.integration, pytest.mark.db]

UTC: Final = dt.UTC
MIB: Final[int] = 1 << 20


def fetch_scalar(dsn: str, sql: str, params: list[object]) -> object:
    async def run() -> object:
        conn = await connect(dsn)
        try:
            rows = await conn._impl._fetch(sql, params, None, scalar=True, limit_one=True, arity=1)
        finally:
            await conn.close()
        return rows[0]

    return asyncio.run(run())


INT_LIMITS: Final[dict[str, tuple[int, int]]] = {
    "int2": (-(2**15), 2**15 - 1),
    "int4": (-(2**31), 2**31 - 1),
    "int8": (-(2**63), 2**63 - 1),
    "oid": (0, 2**32 - 1),
}


@pytest.mark.parametrize("sql_type", sorted(INT_LIMITS))
def test_integer_limits_round_trip(schema: str, sql_type: str) -> None:
    low, high = INT_LIMITS[sql_type]
    for value in (low, high):
        assert fetch_scalar(schema, f"SELECT $1::{sql_type}", [value]) == value


@pytest.mark.parametrize("sql_type", sorted(INT_LIMITS))
def test_integer_beyond_limits_is_encode_error(schema: str, sql_type: str) -> None:
    low, high = INT_LIMITS[sql_type]
    for value in (low - 1, high + 1):
        with pytest.raises(EncodeError, match=f"out of range for {sql_type}"):
            fetch_scalar(schema, f"SELECT $1::{sql_type}", [value])


@pytest.mark.parametrize("sql_type", ["float4", "float8"])
def test_float_specials_round_trip(schema: str, sql_type: str) -> None:
    nan = fetch_scalar(schema, f"SELECT $1::{sql_type}", [float("nan")])
    assert isinstance(nan, float)
    assert isnan(nan)
    for value in (float("inf"), float("-inf")):
        assert fetch_scalar(schema, f"SELECT $1::{sql_type}", [value]) == value
    negative_zero = fetch_scalar(schema, f"SELECT $1::{sql_type}", [-0.0])
    assert negative_zero == 0
    assert isinstance(negative_zero, float)
    assert str(negative_zero) == "-0.0"


def test_float4_overflow_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match="out of range for float4"):
        fetch_scalar(schema, "SELECT $1::float4", [1e300])


def test_decimal_signalling_nan_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match="sNaN"):
        fetch_scalar(schema, "SELECT $1::numeric", [Decimal("sNaN")])


@pytest.mark.parametrize("value", [Decimal("1E+1000"), Decimal("1E-1000"), Decimal("-1.5E+1000"), Decimal("1E+100000")])
def test_decimal_large_exponent_round_trips(schema: str, value: Decimal) -> None:
    assert fetch_scalar(schema, "SELECT $1::numeric", [value]) == value


def test_decimal_beyond_numeric_range_is_database_error(schema: str) -> None:
    with pytest.raises(DatabaseError) as info:
        fetch_scalar(schema, "SELECT $1::numeric", [Decimal("1E+200000")])
    assert info.value.sqlstate == "22003"


NAIVE_DATETIME: Final = dt.datetime(2024, 1, 2, 3, 4, 5)  # ruff: ignore[call-datetime-without-tzinfo]
AWARE_DATETIME: Final = NAIVE_DATETIME.replace(tzinfo=dt.timezone(dt.timedelta(hours=-5)))
NAIVE_TIME: Final = dt.time(1, 2, 3)
AWARE_TIME: Final = NAIVE_TIME.replace(tzinfo=UTC)


def test_naive_and_aware_datetime(schema: str) -> None:
    assert fetch_scalar(schema, "SELECT $1::timestamp", [NAIVE_DATETIME]) == NAIVE_DATETIME
    # an aware value is stored as an instant and read back in UTC
    echoed = fetch_scalar(schema, "SELECT $1::timestamptz", [AWARE_DATETIME])
    assert echoed == AWARE_DATETIME
    assert isinstance(echoed, dt.datetime)
    assert echoed.utcoffset() == dt.timedelta(0)
    with pytest.raises(EncodeError, match="naive"):
        fetch_scalar(schema, "SELECT $1::timestamptz", [NAIVE_DATETIME])
    with pytest.raises(EncodeError, match="timezone-aware"):
        fetch_scalar(schema, "SELECT $1::timestamp", [AWARE_DATETIME])


def test_naive_and_aware_time(schema: str) -> None:
    assert fetch_scalar(schema, "SELECT $1::time", [NAIVE_TIME]) == NAIVE_TIME
    assert fetch_scalar(schema, "SELECT $1::timetz", [AWARE_TIME]) == AWARE_TIME
    with pytest.raises(EncodeError, match="naive"):
        fetch_scalar(schema, "SELECT $1::timetz", [NAIVE_TIME])
    with pytest.raises(EncodeError, match="timezone-aware"):
        fetch_scalar(schema, "SELECT $1::time", [AWARE_TIME])


@pytest.mark.parametrize("sql_type", ["text", "varchar", "jsonb"])
def test_str_with_nul_is_rejected(schema: str, sql_type: str) -> None:
    value = "a\x00b"
    if sql_type == "jsonb":
        # json.dumps escapes NUL, so the server is the one that rejects it
        with pytest.raises(DatabaseError) as info:
            fetch_scalar(schema, "SELECT $1::jsonb", [value])
        assert info.value.sqlstate == "22P05"
    else:
        with pytest.raises(EncodeError, match="NUL"):
            fetch_scalar(schema, f"SELECT $1::{sql_type}", [value])


def test_nul_inside_an_array_element_is_rejected(schema: str) -> None:
    with pytest.raises(EncodeError, match="NUL"):
        fetch_scalar(schema, "SELECT $1::text[]", [["ok", "a\x00"]])


def test_non_bmp_characters_round_trip(schema: str) -> None:
    value = "😀 𝄞 \U0010ffff"
    assert fetch_scalar(schema, "SELECT $1::text", [value]) == value
    assert fetch_scalar(schema, "SELECT octet_length($1::text)", [value]) == len(value.encode())


def test_bytes_over_one_mebibyte_round_trip(schema: str) -> None:
    value: Any = bytes(range(256)) * (4 * MIB // 256 + 1)
    assert len(value) > MIB
    assert fetch_scalar(schema, "SELECT $1::bytea", [value]) == value
