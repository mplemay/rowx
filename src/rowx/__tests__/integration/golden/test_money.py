import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest

from rowx import query
from rowx.errors import DatabaseError, EncodeError
from rowx.postgres import Connection, Money, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

INT8_MAX: Final[int] = 2**63 - 1
INT8_MIN: Final[int] = -(2**63)
INVALID_PARAMETER_VALUE: Final[str] = "22023"


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def read(dsn: str, sql: str, params: list[object] | None = None) -> Any:  # ruff: ignore[any-type]
    async def run(conn: Connection) -> Any:  # ruff: ignore[any-type]
        return (await conn._impl._fetch(sql, params or [], None, scalar=True, limit_one=True, arity=1))[0]

    return run_with_conn(dsn, run)


@pytest.mark.parametrize(
    ("literal", "minor_units"),
    [
        ("0", 0),
        ("0.01", 1),
        ("1.00", 100),
        ("-1.00", -100),
        ("12345.67", 1_234_567),
        ("92233720368547758.07", INT8_MAX),
        ("-92233720368547758.08", INT8_MIN),
    ],
)
def test_money_decodes_to_minor_units(schema: str, literal: str, minor_units: int) -> None:
    assert read(schema, f"SELECT '{literal}'::money") == Money(minor_units)


def test_money_to_decimal_takes_the_callers_scale() -> None:
    assert Money(1_234_567).to_decimal(2) == Decimal("12345.67")
    assert Money(1_234_567).to_decimal(0) == Decimal(1_234_567)
    assert Money(-5).to_decimal(3) == Decimal("-0.005")


@pytest.mark.parametrize("minor_units", [0, 1, -1, 100, INT8_MAX, INT8_MIN])
def test_money_round_trips(schema: str, minor_units: int) -> None:
    assert read(schema, "SELECT $1::money", [Money(minor_units)]) == Money(minor_units)


def test_money_array_decodes_and_encodes_with_null_elements(schema: str) -> None:
    assert read(schema, "SELECT '{1.00,NULL,-2.50}'::money[]") == [Money(100), None, Money(-250)]
    value = [[Money(1), None], [Money(INT8_MAX), Money(INT8_MIN)]]
    assert read(schema, "SELECT $1::money[]", [value]) == value
    assert read(schema, "SELECT '{}'::money[]") == []


def test_money_is_read_through_a_query(schema: str) -> None:
    async def body(conn: Connection) -> tuple[Money, Money | None, list[Money | None]]:
        one = await query[Money](t"SELECT {Money(250)}::money").one(conn)
        none = await query[Money | None](t"SELECT NULL::money").one(conn)
        many = await query[list[Money | None]](t"SELECT ARRAY[{Money(1)}, NULL]::money[]").one(conn)
        return one, none, many

    assert run_with_conn(schema, body) == (Money(250), None, [Money(1), None])


# the scale comes from lc_monetary, a session setting that can change at any time, so the value keeps no scale and
# the same text means different minor units after a SET
@pytest.mark.parametrize(
    ("locale", "scale"),
    [("C", 2), ("en_US.utf8", 2), ("ja_JP.UTF-8", 0), ("ja_JP.utf8", 0)],
)
def test_minor_units_follow_the_session_locale_without_a_stored_scale(schema: str, locale: str, scale: int) -> None:
    async def body(conn: Connection) -> tuple[object, object, object]:
        before = (await conn._impl._fetch("SELECT '1000'::money", [], None, scalar=True, limit_one=True, arity=1))[0]
        await conn._impl._execute(f"SET lc_monetary = '{locale}'", [])
        after = (await conn._impl._fetch("SELECT '1000'::money", [], None, scalar=True, limit_one=True, arity=1))[0]
        # the same connection and statement cache, a fresh read after the setting changed
        again = (await conn._impl._fetch("SELECT '1000'::money", [], None, scalar=True, limit_one=True, arity=1))[0]
        return before, after, again

    try:
        before, after, again = run_with_conn(schema, body)
    except DatabaseError as exc:
        # the stock postgres image only ships C and en_US
        if exc.sqlstate == INVALID_PARAMETER_VALUE:
            pytest.skip(f"the server has no {locale} locale")
        raise
    assert before == Money(100_000)
    assert after == again == Money(1000 * 10**scale)
    assert isinstance(after, Money)
    assert after.to_decimal(scale) == Decimal(1000)


def test_money_param_takes_only_money(schema: str) -> None:
    for value in (5, Decimal("5.00"), "5.00", 1.5):
        with pytest.raises(EncodeError, match="money"):
            read(schema, "SELECT $1::money", [value])


@pytest.mark.parametrize("minor_units", [INT8_MAX + 1, INT8_MIN - 1])
def test_money_outside_int8_is_an_encode_error(schema: str, minor_units: int) -> None:
    with pytest.raises(EncodeError, match="int8"):
        read(schema, "SELECT $1::money", [Money(minor_units)])


def test_money_is_not_json_serialisable(schema: str) -> None:
    with pytest.raises(EncodeError, match="Money"):
        read(schema, "SELECT $1::json", [Money(1)])


def test_money_is_not_accepted_for_a_text_parameter(schema: str) -> None:
    with pytest.raises(EncodeError, match="not accepted for text"):
        read(schema, "SELECT $1::text", [Money(1)])
