import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from rowx import query
from rowx.errors import EncodeError
from rowx.postgres import Connection, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "value",
    [
        Decimal(0),
        Decimal("1.50"),
        Decimal("-0.00"),
        Decimal("123456789.987654321"),
        Decimal("1E-30"),
        Decimal("1E+100"),
        Decimal(-42),
        Decimal("0.0001234"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_numeric_round_trip(schema: str, value: Decimal) -> None:
    got = run_with_conn(schema, query[Decimal](t"SELECT {value}::numeric").one)
    if value.is_nan():
        assert got.is_nan()
    else:
        assert got == value


def test_numeric_keeps_trailing_zeros(schema: str) -> None:
    value = Decimal("1.5000")
    got = run_with_conn(schema, query[Decimal](t"SELECT {value}::numeric").one)
    assert str(got) == "1.5000"


def test_numeric_decodes_large_scale_literal(schema: str) -> None:
    q = query[Decimal](t"SELECT 1e-30::numeric")
    assert run_with_conn(schema, q.one) == Decimal("1E-30")


def test_int_to_numeric(schema: str) -> None:
    for x in (7, -(2**70)):
        got = run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one)
        assert got == Decimal(x)


def test_int_beyond_the_str_digit_limit_to_numeric(schema: str) -> None:
    x = 10**5000 + 7
    got = run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one)
    assert got == Decimal(x)


def test_int_beyond_the_str_digit_limit_to_int8_is_encode_error(schema: str) -> None:
    x = 10**5000
    with pytest.raises(EncodeError, match=r"\$1.*int8"):
        run_with_conn(schema, query[int](t"SELECT {x}::int8").one)


def test_numeric_decodes_more_than_32767_digit_groups(schema: str) -> None:
    q = query[Decimal](t"SELECT repeat('1', 131072)::numeric")
    assert run_with_conn(schema, q.one) == Decimal("1" * 131072)


@pytest.mark.parametrize("text", ["NaN123", "-NaN7"])
def test_nan_with_payload_is_sent_as_nan(schema: str, text: str) -> None:
    x = Decimal(text)
    assert run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one).is_nan()


@pytest.mark.parametrize("text", ["sNaN", "sNaN123"])
def test_snan_is_encode_error(schema: str, text: str) -> None:
    x = Decimal(text)
    with pytest.raises(EncodeError, match=r"\$1.*sNaN"):
        run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one)


def test_float_to_numeric_is_encode_error(schema: str) -> None:
    x = 1.5
    with pytest.raises(EncodeError, match=r"\$1.*float.*numeric"):
        run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one)


def test_float8_round_trip(schema: str) -> None:
    x = 1.25e100
    assert run_with_conn(schema, query[float](t"SELECT {x}::float8").one) == x


def test_float8_special_values(schema: str) -> None:
    inf, nan = float("inf"), float("nan")
    q = query[tuple[float, float]](t"SELECT {inf}::float8, {nan}::float8")
    got_inf, got_nan = run_with_conn(schema, q.one)
    assert got_inf == inf
    assert got_nan != got_nan  # ruff: ignore[comparison-with-itself]


def test_float4_round_trip(schema: str) -> None:
    x = 0.5
    assert run_with_conn(schema, query[float](t"SELECT {x}::float4").one) == x


def test_float4_overflow_is_encode_error(schema: str) -> None:
    x = 1e300
    with pytest.raises(EncodeError, match=r"\$1.*float4"):
        run_with_conn(schema, query[float](t"SELECT {x}::float4").one)


def test_int_to_float8(schema: str) -> None:
    x = 3
    got = run_with_conn(schema, query[float](t"SELECT {x}::float8").one)
    assert got == 3.0  # ruff: ignore[float-equality-comparison]
    assert isinstance(got, float)


def test_oid_round_trip(schema: str) -> None:
    x = 4_000_000_000
    assert run_with_conn(schema, query[int](t"SELECT {x}::oid").one) == x


def test_oid_out_of_range_is_encode_error(schema: str) -> None:
    x = -1
    with pytest.raises(EncodeError, match=r"\$1.*oid"):
        run_with_conn(schema, query[int](t"SELECT {x}::oid").one)
