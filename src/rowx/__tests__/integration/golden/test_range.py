import asyncio
import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest

from rowx import query
from rowx.errors import DatabaseError, DecodeError, EncodeError
from rowx.postgres import Connection, Range, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

UTC: Final = dt.UTC
RANGE_TYPES: Final[list[str]] = ["int4range", "int8range", "numrange", "daterange", "tsrange", "tstzrange"]


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
    ("literal", "expected"),
    [
        ("[1,5]", Range(1, 6)),
        ("(1,5)", Range(2, 5)),
        ("[1,5)", Range(1, 5)),
        ("(,)", Range(None, None)),
        ("[1,)", Range(1, None)),
        ("(,5)", Range(None, 5)),
        ("empty", Range(empty=True)),
        # an empty range is the same value however it was written
        ("[3,3)", Range(empty=True)),
    ],
)
def test_int4range_decodes_to_the_canonical_form(schema: str, literal: str, expected: Range[int]) -> None:
    assert read(schema, f"SELECT '{literal}'::int4range") == expected


def test_decoded_range_exposes_its_flags(schema: str) -> None:
    got = read(schema, "SELECT '[1,5]'::int4range")
    assert (got.lower, got.upper, got.lower_inc, got.upper_inc, got.empty) == (1, 6, True, False, False)
    assert got.bounds == "[)"
    assert (got.lower_inf, got.upper_inf) == (False, False)
    both = read(schema, "SELECT '(,)'::int4range")
    assert (both.lower_inf, both.upper_inf, both.bounds) == (True, True, "()")
    empty = read(schema, "SELECT 'empty'::int4range")
    assert (empty.empty, empty.lower_inf, empty.upper_inf, empty.bounds) == (True, False, False, "empty")


def test_continuous_ranges_keep_their_inclusivity(schema: str) -> None:
    got = read(schema, "SELECT '(1.5,2.5]'::numrange")
    assert got == Range(Decimal("1.5"), Decimal("2.5"), lower_inc=False, upper_inc=True)
    assert got.bounds == "(]"
    assert read(schema, "SELECT '[0,)'::numrange") == Range(Decimal(0), None)


def test_int8range_reaches_the_ends_of_int8(schema: str) -> None:
    low, high = -(2**63), 2**63 - 2
    assert read(schema, "SELECT $1::int8range", [Range(low, high)]) == Range(low, high)


def test_daterange_and_timestamp_ranges_decode_their_bounds(schema: str) -> None:
    assert read(schema, "SELECT '[2024-02-28,2024-02-29]'::daterange") == Range(
        dt.date(2024, 2, 28), dt.date(2024, 3, 1)
    )
    naive = read(schema, "SELECT '[2024-01-02 03:04:05,2024-01-02 03:04:06)'::tsrange")
    assert naive.lower.tzinfo is None
    assert naive.upper == dt.datetime(2024, 1, 2, 3, 4, 6)  # ruff: ignore[call-datetime-without-tzinfo]
    aware = read(schema, "SELECT '[2024-01-02 03:04:05+00,)'::tstzrange")
    assert aware == Range(dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC), None)
    assert aware.lower.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize("literal", ["[-infinity,2024-01-01)", "[2024-01-01,infinity)"])
def test_infinite_date_bound_is_a_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match=r"infinite|infinity"):
        read(schema, f"SELECT '{literal}'::daterange")


@pytest.mark.parametrize("sql_type", ["tsrange", "tstzrange"])
def test_infinite_timestamp_bound_is_a_decode_error(schema: str, sql_type: str) -> None:
    with pytest.raises(DecodeError, match=r"infinite|infinity"):
        read(schema, f"SELECT '[-infinity,2024-01-01 00:00:00+00)'::{sql_type}")


def test_numrange_nan_follows_the_servers_ordering(schema: str) -> None:
    # NaN sorts above every number, so it is a valid upper bound and an invalid lower bound
    assert read(schema, "SELECT '[1,NaN]'::numrange").upper.is_nan()
    got = read(schema, "SELECT $1::numrange", [Range(Decimal(1), Decimal("NaN"), upper_inc=True)])
    assert got.upper.is_nan()
    with pytest.raises(DatabaseError, match="lower bound must be less than or equal"):
        read(schema, "SELECT $1::numrange", [Range(Decimal("NaN"), Decimal(1))])


@pytest.mark.parametrize(
    ("sql_type", "value"),
    [
        ("int4range", Range(1, 5)),
        ("int4range", Range(1, 5, lower_inc=False, upper_inc=True)),
        ("int4range", Range(None, 5)),
        ("int4range", Range(empty=True)),
        ("int8range", Range(-(2**40), None)),
        ("numrange", Range(Decimal("1.50"), Decimal("1E+3"), upper_inc=True)),
        ("numrange", Range(1, 2)),
        ("numrange", Range(None, None)),
        ("numrange", Range(empty=True)),
        ("daterange", Range(dt.date(2024, 1, 1), dt.date(2024, 2, 1))),
        ("tsrange", Range(dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2, 3, 4, 5, 6))),  # ruff: ignore[call-datetime-without-tzinfo]
        ("tstzrange", Range(dt.datetime(2024, 1, 1, tzinfo=UTC), None)),
    ],
)
def test_range_round_trips(schema: str, sql_type: str, value: Range[Any]) -> None:
    got = read(schema, f"SELECT $1::{sql_type}", [value])
    assert got == read(schema, f"SELECT $1::{sql_type}", [got])
    assert got.empty == value.empty
    if not value.empty:
        assert got.lower_inf == (value.lower is None)
        assert got.upper_inf == (value.upper is None)


def test_an_inclusive_discrete_range_is_canonicalised_by_the_server(schema: str) -> None:
    assert read(schema, "SELECT $1::int4range", [Range(1, 5, upper_inc=True)]) == Range(1, 6)
    assert read(schema, "SELECT $1::int4range", [Range(1, 5, lower_inc=False)]) == Range(2, 5)


def test_integer_bounds_are_accepted_for_numrange(schema: str) -> None:
    assert read(schema, "SELECT $1::numrange", [Range(1, 2)]) == Range(Decimal(1), Decimal(2))


@pytest.mark.parametrize(
    ("sql_type", "value"),
    [
        ("int4range", Range("a", "b")),
        ("int4range", Range(1.5, 2.5)),
        ("daterange", Range(1, 2)),
        ("tsrange", Range(dt.datetime(2024, 1, 1, tzinfo=UTC), None)),
        ("tstzrange", Range(dt.datetime(2024, 1, 1), None)),  # ruff: ignore[call-datetime-without-tzinfo]
        ("int4range", Range(2**40, None)),
    ],
)
def test_wrong_bound_type_is_an_encode_error(schema: str, sql_type: str, value: Range[Any]) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*(lower|upper) bound"):
        read(schema, f"SELECT $1::{sql_type}", [value])


@pytest.mark.parametrize("value", [(1, 5), [1, 5], "[1,5)", 5, Range])
def test_a_non_range_is_an_encode_error(schema: str, value: object) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*int4range"):
        read(schema, "SELECT $1::int4range", [value])


def test_a_range_is_not_accepted_for_another_type(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*Range.*int4"):
        read(schema, "SELECT $1::int4", [Range(1, 2)])


@pytest.mark.parametrize("sql_type", RANGE_TYPES)
def test_range_arrays_round_trip_with_nulls_empty_and_two_dimensions(schema: str, sql_type: str) -> None:
    sample = {
        "int4range": Range(1, 5),
        "int8range": Range(None, 5),
        "numrange": Range(Decimal("1.5"), Decimal("2.5"), upper_inc=True),
        "daterange": Range(dt.date(2024, 1, 1), None),
        "tsrange": Range(dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2)),  # ruff: ignore[call-datetime-without-tzinfo]
        "tstzrange": Range(dt.datetime(2024, 1, 1, tzinfo=UTC), None),
    }[sql_type]
    empty: Range[Any] = Range(empty=True)
    flat = [sample, None, empty]
    assert read(schema, f"SELECT $1::{sql_type}[]", [flat]) == flat
    matrix = [[sample, None], [empty, sample]]
    assert read(schema, f"SELECT $1::{sql_type}[]", [matrix]) == matrix
    assert read(schema, f"SELECT $1::{sql_type}[]", [[]]) == []


def test_range_array_decodes_from_the_server(schema: str) -> None:
    got = read(schema, "SELECT ARRAY['[1,5]'::int4range, NULL, 'empty']")
    assert got == [Range(1, 6), None, Range(empty=True)]


def test_array_element_error_names_the_element(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*element 1"):
        read(schema, "SELECT $1::int4range[]", [[Range(1, 2), Range("x", None)]])


def test_a_domain_over_a_range_is_its_base_range(schema: str) -> None:
    assert read(schema, "SELECT '[1,5]'::rowx_short_range") == Range(1, 6)
    assert read(schema, "SELECT $1::rowx_short_range", [Range(1, 3)]) == Range(1, 3)


def test_user_defined_range_over_a_supported_subtype_works(schema: str) -> None:
    # a user-defined range has no canonical function, so the server keeps `[1,5]` as written
    assert read(schema, "SELECT '[1,5]'::rowx_intrange") == Range(1, 5, upper_inc=True)
    assert read(schema, "SELECT $1::rowx_intrange", [Range(3, 9)]) == Range(3, 9)
    assert read(schema, "SELECT $1::rowx_intrange[]", [[Range(3, 9), None]]) == [Range(3, 9), None]
    assert read(schema, "SELECT c_custom_range FROM all_types") is None


def test_user_defined_range_over_an_unsupported_subtype_is_a_decode_error(schema: str) -> None:
    with pytest.raises(DecodeError, match="rowx_floatrange"):
        read(schema, "SELECT '[1.5,2.5]'::rowx_floatrange")


def test_query_annotated_with_ranges_decodes(schema: str) -> None:
    q = query[tuple[Range[int], Range[Decimal] | None, list[Range[dt.date]]]](
        t"SELECT '[1,3)'::int4range, NULL::numrange, ARRAY['[2024-01-01,2024-01-02)'::daterange]"
    )
    assert run_with_conn(schema, q.one) == (
        Range(1, 3),
        None,
        [Range(dt.date(2024, 1, 1), dt.date(2024, 1, 2))],
    )
