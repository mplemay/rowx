import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

import rowx
from rowx import query
from rowx.errors import EncodeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[rowx.Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await rowx.connect(dsn) as conn:
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
    got = run_with_conn(schema, query[Decimal](t"SELECT {value}::numeric").fetch_one)
    if value.is_nan():
        assert got.is_nan()
    else:
        assert got == value


def test_numeric_keeps_trailing_zeros(schema: str) -> None:
    value = Decimal("1.5000")
    got = run_with_conn(schema, query[Decimal](t"SELECT {value}::numeric").fetch_one)
    assert str(got) == "1.5000"


def test_numeric_decodes_large_scale_literal(schema: str) -> None:
    q = query[Decimal](t"SELECT 1e-30::numeric")
    assert run_with_conn(schema, q.fetch_one) == Decimal("1E-30")


def test_int_to_numeric(schema: str) -> None:
    for x in (7, -(2**70)):
        got = run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").fetch_one)
        assert got == Decimal(x)


def test_snan_is_encode_error(schema: str) -> None:
    x = Decimal("sNaN")
    with pytest.raises(EncodeError, match=r"\$1.*sNaN"):
        run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").fetch_one)


def test_float_to_numeric_is_encode_error(schema: str) -> None:
    x = 1.5
    with pytest.raises(EncodeError, match=r"\$1.*float.*numeric"):
        run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").fetch_one)


def test_float8_round_trip(schema: str) -> None:
    x = 1.25e100
    assert run_with_conn(schema, query[float](t"SELECT {x}::float8").fetch_one) == x


def test_float8_special_values(schema: str) -> None:
    inf, nan = float("inf"), float("nan")
    q = query[tuple[float, float]](t"SELECT {inf}::float8, {nan}::float8")
    got_inf, got_nan = run_with_conn(schema, q.fetch_one)
    assert got_inf == inf
    assert got_nan != got_nan  # noqa: PLR0124


def test_float4_round_trip(schema: str) -> None:
    x = 0.5
    assert run_with_conn(schema, query[float](t"SELECT {x}::float4").fetch_one) == x


def test_float4_overflow_is_encode_error(schema: str) -> None:
    x = 1e300
    with pytest.raises(EncodeError, match=r"\$1.*float4"):
        run_with_conn(schema, query[float](t"SELECT {x}::float4").fetch_one)


def test_int_to_float8(schema: str) -> None:
    x = 3
    got = run_with_conn(schema, query[float](t"SELECT {x}::float8").fetch_one)
    assert got == 3.0
    assert isinstance(got, float)


def test_oid_round_trip(schema: str) -> None:
    x = 4_000_000_000
    assert run_with_conn(schema, query[int](t"SELECT {x}::oid").fetch_one) == x


def test_oid_out_of_range_is_encode_error(schema: str) -> None:
    x = -1
    with pytest.raises(EncodeError, match=r"\$1.*oid"):
        run_with_conn(schema, query[int](t"SELECT {x}::oid").fetch_one)
