import asyncio
import datetime as dt
import sys
from decimal import Decimal, DecimalTuple
from math import isnan
from typing import Any, Final, override

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
            rows = await conn._impl._fetch(sql, params, None, scalar=True, limit_one=True, arity=1, shape=None)
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


ADVERSARIAL: Final[list[str]] = [
    "",
    "plain",
    'has "quote"',
    "back\\slash",
    "a,b",
    "{braces}",
    "{}",
    "NULL",
    "null",
    " padded ",
    "empty",
    "ā",
    "表ア",
    "😀́",
    "line\nbreak\ttab",
    '\\"\\\\',
]


def shaped(values: list[Any]) -> list[list[Any]]:
    # a NULL-holding vector, a two-dimensional array and a three-dimensional one, all from the same values
    pool = [values[i % len(values)] for i in range(8)]
    return [
        [*values, None],
        [pool[:4], [pool[4], None, pool[5], pool[6]]],
        [[pool[:2], [pool[2], None]], [[pool[3], pool[4]], [pool[5], pool[6]]]],
    ]


@pytest.mark.parametrize("sql_type", ["text", "varchar", "name", "public.citext"])
def test_text_arrays_keep_adversarial_strings(schema: str, sql_type: str) -> None:
    values = [value for value in ADVERSARIAL if not (sql_type == "name" and "\n" in value)]
    for array in shaped(values):
        assert fetch_scalar(schema, f"SELECT $1::{sql_type}[]", [array]) == array


def test_bpchar_arrays_keep_adversarial_strings(schema: str) -> None:
    for array in shaped(ADVERSARIAL):
        assert fetch_scalar(schema, "SELECT $1::bpchar[]", [array]) == array


@pytest.mark.parametrize("sql_type", ["json", "jsonb"])
def test_json_arrays_round_trip_adversarial_documents(schema: str, sql_type: str) -> None:
    documents: list[Any] = [{"k": value, value: [value, None, 1.5]} for value in ADVERSARIAL]
    # a bare list would read as another array dimension, so lists only appear inside documents
    documents += ["just a string", 7, {}, True, {"l": []}]
    for array in shaped(documents):
        assert fetch_scalar(schema, f"SELECT $1::{sql_type}[]", [array]) == array


def test_json_parameters_are_sent_as_ascii(schema: str) -> None:
    # json.dumps runs with ensure_ascii=True, so a non-ASCII character is a \uXXXX escape and never raw UTF-8
    assert fetch_scalar(schema, "SELECT $1::json::text", ["ā表😀"]) == '"\\u0101\\u8868\\ud83d\\ude00"'
    assert fetch_scalar(schema, "SELECT $1::json::text", [{"ā": ["é"]}]) == '{"\\u0101": ["\\u00e9"]}'
    assert fetch_scalar(schema, "SELECT $1::json[]::text", [["ā", None]]) == '{"\\"\\\\u0101\\"",NULL}'
    assert fetch_scalar(schema, "SELECT $1::jsonb::text", ["ā"]) == '"ā"'


def test_enum_and_domain_arrays_keep_their_element_types(schema: str) -> None:
    moods = ["sad", "ok", "happy"]
    for array in shaped(moods):
        assert fetch_scalar(schema, "SELECT $1::mood[]", [array]) == array
    for array in shaped([1, 5, 2147483647]):
        assert fetch_scalar(schema, "SELECT $1::positive_int[]", [array]) == array
    assert fetch_scalar(schema, "SELECT $1::positive_ints", [[1, 2]]) == [1, 2]
    with pytest.raises(DatabaseError, match="positive_int"):
        fetch_scalar(schema, "SELECT $1::positive_int[]", [[1, -1]])
    with pytest.raises(DatabaseError):
        fetch_scalar(schema, "SELECT $1::mood[]", [["nope"]])


def test_uuid_str_elements_are_parsed_on_the_client(schema: str) -> None:
    from uuid import UUID  # ruff: ignore[import-outside-top-level]

    text = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    expected = UUID(text)
    for spelling in [text, text.upper(), text.replace("-", ""), f"{{{text}}}", "a0eebc99-9c0b4ef8-bb6d6bb9-bd380a11"]:
        assert fetch_scalar(schema, "SELECT $1::uuid[]", [[spelling, None, expected]]) == [expected, None, expected]
    for bad in ["", "not a uuid", text + "0", text[:-1], f"{text}}}", "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a1g", "'}"]:
        with pytest.raises(EncodeError, match="uuid"):
            fetch_scalar(schema, "SELECT $1::uuid[]", [[bad]])
    # a scalar uuid str is still handed to the server as text
    assert fetch_scalar(schema, "SELECT $1::uuid", [text]) == expected


def test_timetz_array_elements_carry_the_zone(schema: str) -> None:
    zone = dt.timezone(dt.timedelta(hours=5, minutes=30))
    values = [
        dt.time(12, 0, tzinfo=zone),
        None,
        dt.time(23, 59, 59, 999999, tzinfo=dt.timezone(-dt.timedelta(hours=7))),
    ]
    assert fetch_scalar(schema, "SELECT $1::timetz[]", [values]) == values
    assert fetch_scalar(schema, "SELECT $1::timetz[]::text", [values[:1]]) == "{12:00:00+05:30}"
    with pytest.raises(EncodeError, match="naive"):
        fetch_scalar(schema, "SELECT $1::timetz[]", [[dt.time(1)]])
    over = dt.time(0, tzinfo=dt.timezone(dt.timedelta(hours=16)))
    with pytest.raises(EncodeError, match="out of range"):
        fetch_scalar(schema, "SELECT $1::timetz[]", [[over]])


class ShiftedDecimal(Decimal):
    @override
    def as_tuple(self) -> DecimalTuple:
        return Decimal(999).as_tuple()

    @override
    def __str__(self) -> str:
        return "999"


def test_numeric_arrays_round_trip_decimal_edge_cases(schema: str) -> None:
    values: list[Any] = [
        Decimal(0),
        Decimal("-0.00"),
        Decimal("1.500"),
        Decimal("-12345.6700"),
        Decimal("1E+1000"),
        Decimal("1E+100000"),
        Decimal("123E-30"),
        Decimal("9" * 200),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        0,
        -(2**63),
        2**63,
        -(2**63) - 1,
        2**200,
        None,
    ]
    got = fetch_scalar(schema, "SELECT $1::numeric[]", [values])
    assert isinstance(got, list)
    for sent, back in zip(values, got, strict=True):
        assert back == sent
    # the display scale survives as well as the value
    assert (
        fetch_scalar(schema, "SELECT $1::numeric[]::text", [[Decimal("1.500"), Decimal("-0.00"), 7]])
        == "{1.500,0.00,7}"
    )
    nan = fetch_scalar(schema, "SELECT $1::numeric[]", [[Decimal("NaN"), Decimal("-NaN123")]])
    assert isinstance(nan, list)
    assert all(isinstance(item, Decimal) and item.is_nan() for item in nan)
    for array in shaped([Decimal("1.5"), 2, Decimal(-3)]):
        assert fetch_scalar(schema, "SELECT $1::numeric[]", [array]) == array


def test_numeric_array_decimals_that_cannot_be_encoded_are_encode_errors(schema: str) -> None:
    with pytest.raises(EncodeError, match="sNaN"):
        fetch_scalar(schema, "SELECT $1::numeric[]", [[Decimal(1), Decimal("sNaN")]])
    with pytest.raises(EncodeError, match="element 1"):
        fetch_scalar(schema, "SELECT $1::numeric[]", [[Decimal(1), Decimal("1E+131072")]])
    with pytest.raises(EncodeError, match="scale"):
        fetch_scalar(schema, "SELECT $1::numeric[]", [[Decimal("1E-16384")]])
    with pytest.raises(EncodeError, match="range"):
        fetch_scalar(schema, "SELECT $1::numeric[]", [[Decimal("1E+999999999999999999")]])
    with pytest.raises(EncodeError, match="not accepted for numeric"):
        fetch_scalar(schema, "SELECT $1::numeric[]", [["1.5"]])


def test_a_decimal_subclass_cannot_choose_what_is_encoded(schema: str) -> None:
    # as_tuple and __str__ are read through the unbound Decimal methods, so the subclass overrides are ignored
    value = ShiftedDecimal("12.5")
    assert fetch_scalar(schema, "SELECT $1::numeric[]", [[value]]) == [Decimal("12.5")]
    assert fetch_scalar(schema, "SELECT $1::numeric", [value]) == Decimal("12.5")


def test_an_int_past_the_str_digit_limit_does_not_crash_the_array_encoder(schema: str) -> None:
    # str(int) is capped, so the digits are read through Decimal; golden/test_numeric.py pins the same for scalars
    limit = sys.get_int_max_str_digits()
    assert limit > 0
    for value in (10**limit + 7, -(10**limit), 10 ** (limit - 1)):
        assert fetch_scalar(schema, "SELECT $1::numeric[]", [[value]]) == [Decimal(value)]
    with pytest.raises(EncodeError, match="int8"):
        fetch_scalar(schema, "SELECT $1::int8[]", [[10**limit]])
