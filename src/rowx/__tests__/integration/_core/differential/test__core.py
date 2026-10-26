import asyncio
import datetime as dt
import ipaddress
import struct
from dataclasses import dataclass
from decimal import Decimal
from math import isnan
from pathlib import Path
from typing import Any, Final, Literal, LiteralString, cast

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.range import Range as PsycopgRange

from rowx._core.postgres import type_table
from rowx.errors import DecodeError
from rowx.postgres import Range, connect

pytestmark = [pytest.mark.integration, pytest.mark.db]

TYPES_DOC: Final[Path] = Path(__file__).resolve().parents[6] / "docs" / "types.md"
DOC_HEADING: Final[str] = "## Differences from psycopg"

# how a case may differ from psycopg: equal as instants, equal after rounding to float32, rowx raising DecodeError,
# rowx returning None for void, rowx returning an interface where psycopg returns the bare host address, or rowx's
# own `Range` class holding the bounds psycopg's `Range` holds
type Difference = Literal["instant", "float32", "decode-error", "void", "host-address", "range"]

_RANGE_CLASS: Final[str] = (
    "rowx returns `rowx.postgres.Range`, while psycopg returns its own `Range`; the bounds are equal"
)
_RANGE_BOUNDS: Final[str] = (
    "an infinite bound of this subtype raises `DecodeError`, while psycopg raises its own `DataError`"
)
RANGE_DIFFERENCES: Final[dict[str, str]] = {
    "int4range": f"{_RANGE_CLASS}.",
    "int8range": f"{_RANGE_CLASS}.",
    "numrange": f"{_RANGE_CLASS}.",
    "daterange": f"{_RANGE_CLASS}, but {_RANGE_BOUNDS}.",
    "tsrange": f"{_RANGE_CLASS}, but {_RANGE_BOUNDS}.",
    "tstzrange": f"{_RANGE_CLASS} as instants, but {_RANGE_BOUNDS}.",
}


# the same list the generator writes into docs/types.md; test_difference_list_matches_doc keeps them in step
DIFFERENCES: Final[dict[str, str]] = {
    "timestamptz": (
        "rowx always returns a UTC-aware value, while psycopg uses the session `TimeZone`; the instants are equal."
    ),
    "float4": (
        "rowx widens the exact 32-bit value to a `float`, while psycopg parses the shortest decimal text; the values are "
        "equal after rounding to 32 bits."
    ),
    "char": 'A non-ASCII `"char"` is cut to one byte by the server, which is not valid UTF-8, so rowx raises `DecodeError`.',
    "interval": (
        "An interval with a month or year part raises `DecodeError`, while psycopg approximates a month as 30 days."
    ),
    "date": "An infinite date raises `DecodeError`, while psycopg raises its own `DataError`.",
    "timestamp": "An infinite timestamp raises `DecodeError`, while psycopg raises its own `DataError`.",
    "void": "`void` decodes to `None`, while psycopg returns an empty string.",
    "inet": (
        "rowx always returns an interface, even for a full-width mask, while psycopg returns an address for `/32` and "
        "`/128`; the interface's `ip` is that address."
    ),
    **RANGE_DIFFERENCES,
}


@dataclass(frozen=True, slots=True)
class Case:
    key: str
    sql_type: str
    literal: str
    difference: Difference | None = None


def _cases(key: str, sql_type: str, *literals: str, difference: Difference | None = None) -> list[Case]:
    return [Case(key, sql_type, literal, difference) for literal in literals]


# edge values ported from asyncpg tests/test_codecs.py and psycopg tests/types/
CORPUS: Final[list[Case]] = [
    *_cases("bool", "bool", "true", "false"),
    *_cases("int2", "int2", "-32768", "0", "32767"),
    *_cases("int4", "int4", "-2147483648", "0", "2147483647"),
    *_cases("int8", "int8", "-9223372036854775808", "0", "9223372036854775807"),
    *_cases("oid", "oid", "0", "1", "4294967295"),
    *_cases("float4", "float4", "0", "-0", "1.5", "NaN", "Infinity", "-Infinity"),
    *_cases("float4", "float4", "3.4028235e38", "1e-45", "0.1", difference="float32"),
    *_cases("float8", "float8", "0", "-0", "1.7976931348623157e308", "5e-324", "NaN", "Infinity", "-Infinity"),
    *_cases(
        "numeric",
        "numeric",
        "0",
        "-0.0",
        "1.50",
        "0.0000000001",
        "123456789012345678901234567890.123456789",
        "-99999999999999999999999999999999999999",
        "1e100",
        "1e-100",
        "NaN",
        "Infinity",
        "-Infinity",
    ),
    *_cases("text", "text", "", "héllo", "😀 non-BMP", "it''s", "line\nbreak\ttab", "  padded  "),
    *_cases("varchar", "varchar", "", "var", "héllo ✓"),
    *_cases("bpchar", "char(6)", "", "ab", "abcdef"),
    *_cases("name", "name", "nm", "héllo"),
    *_cases("citext", "public.citext", "MiXed", ""),
    *_cases("char", '"char"', "x", " ", "~"),
    *_cases("char", '"char"', "é", difference="decode-error"),
    *_cases("bytea", "bytea", "\\x", "\\x00", "\\x00ff80", "\\x" + "ab" * 100_000),
    *_cases("date", "date", "0001-01-01", "1970-01-01", "2000-02-29", "9999-12-31"),
    *_cases("date", "date", "infinity", "-infinity", difference="decode-error"),
    *_cases("time", "time", "00:00:00", "01:02:03.000004", "23:59:59.999999"),
    *_cases("timetz", "timetz", "00:00:00+00", "01:02:03+02", "00:00:00-12", "12:30:00+05:30"),
    *_cases(
        "timestamp",
        "timestamp",
        "0001-01-01 00:00:00",
        "1970-01-01 00:00:00",
        "2000-02-29 12:34:56.789012",
        "9999-12-31 23:59:59.999999",
    ),
    *_cases("timestamp", "timestamp", "infinity", "-infinity", difference="decode-error"),
    *_cases(
        "timestamptz",
        "timestamptz",
        "2024-01-02 03:04:05+00",
        "2024-06-30 23:59:59.999999-08",
        "0001-01-02 00:00:00+00",
        "9999-12-30 00:00:00+05:30",
        difference="instant",
    ),
    *_cases("timestamptz", "timestamptz", "infinity", "-infinity", difference="decode-error"),
    *_cases(
        "interval",
        "interval",
        "0",
        "1 day",
        "-1 day 02:00:00",
        "00:00:00.000001",
        "1000000 hours",
        "-1000 days -00:00:00.5",
    ),
    *_cases("interval", "interval", "1 mon", "1 year", "-178000000 years", "1 mon 1 day", difference="decode-error"),
    *_cases("uuid", "uuid", "00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"),
    *_cases("inet", "inet", "10.0.0.1/24", "0.0.0.0/0", "::/0", "2001:db8::1/64", "255.255.255.255/31"),
    *_cases("inet", "inet", "127.0.0.1", "::1", "::ffff:1.2.3.4", "1.2.3.4/32", difference="host-address"),
    *_cases("cidr", "cidr", "10.0.0.0/8", "0.0.0.0/0", "::/0", "2001:db8::/32", "192.168.1.1/32", "::1/128"),
    *_cases("macaddr", "macaddr", "08:00:2b:01:02:03", "FF-FF-FF-FF-FF-FF", "08002b010203", "0800.2b01.0203"),
    *_cases("macaddr8", "macaddr8", "08:00:2b:01:02:03:04:05", "08:00:2b:01:02:03", "0800.2b01.0203.0405"),
    *_cases("json", "json", "null", "[]", '{"a": [1, null, "x"]}', '"é"', "1.5e3", "  [ 1 , 2 ]  "),
    *_cases("jsonb", "jsonb", "null", "[]", '{"a": [1, null, "x"], "b": {}}', "12345678901234567890", "1.50"),
    *_cases("enum", "mood", "happy", "ok"),
    *_cases(
        "array",
        "int[]",
        "{}",
        "{1,NULL,3}",
        "{{1,2},{3,4}}",
        "{{{1},{2}},{{3},{4}}}",
        "[2:3]={1,2}",
    ),
    *_cases(
        "array",
        "text[]",
        '{"a b","c\\"d",NULL,"NULL","","{}",é}',
        "{{a,b},{c,d}}",
    ),
    *_cases("array", "uuid[]", "{00000000-0000-0000-0000-000000000000,NULL}"),
    *_cases("array", "numeric[]", "{1.5,NaN,NULL}"),
    *_cases("array", "bytea[]", '{"\\\\x00ff",NULL}'),
    *_cases("array", "timestamptz[]", '{"2024-01-02 03:04:05+00",NULL}', difference="instant"),
    *_cases("array", "jsonb[]", '{"{\\"a\\": 1}",NULL}'),
    *_cases("array", "float8[]", "{NaN,Infinity,-0}"),
    *_cases("array", "cidr[]", "{10.0.0.0/8,NULL,2001:db8::/32}"),
    *_cases("array", "macaddr[]", "{08:00:2b:01:02:03,NULL}"),
    *_cases("array", "macaddr8[]", "{08:00:2b:01:02:03:04:05,NULL}"),
    *_cases("array", "inet[]", "{{10.0.0.1/24,NULL},{2001:db8::1/64,10.0.0.2/24}}"),
    *_cases(
        "int4range",
        "int4range",
        "[1,5]",
        "(1,5)",
        "(,)",
        "empty",
        "[1,)",
        "(,5)",
        "[-2147483648,2147483647)",
        difference="range",
    ),
    *_cases(
        "int8range",
        "int8range",
        "[1,5]",
        "(,)",
        "empty",
        "[-9223372036854775808,9223372036854775807)",
        difference="range",
    ),
    *_cases("numrange", "numrange", "[1.5,2.5)", "(0,1e100]", "(,)", "empty", "[0,)", "(-1.50,)", difference="range"),
    *_cases(
        "daterange",
        "daterange",
        "[2024-01-01,2024-02-01)",
        "[0001-01-01,)",
        "(,9999-12-30]",
        "empty",
        difference="range",
    ),
    *_cases("daterange", "daterange", "[2024-01-01,infinity)", "[-infinity,2024-01-01)", difference="decode-error"),
    *_cases(
        "tsrange",
        "tsrange",
        "[2024-01-02 03:04:05,2024-01-02 03:04:06)",
        "(,2000-02-29 12:34:56.789012]",
        "empty",
        difference="range",
    ),
    *_cases("tsrange", "tsrange", "[2024-01-02 03:04:05,infinity)", difference="decode-error"),
    *_cases(
        "tstzrange",
        "tstzrange",
        "[2024-01-02 03:04:05+00,2024-06-30 23:59:59.999999-08)",
        "(,)",
        "empty",
        difference="range",
    ),
    *_cases("tstzrange", "tstzrange", "[-infinity,2024-01-02 03:04:05+00)", difference="decode-error"),
    *_cases("array", "int4range[]", '{"[1,5)",NULL,empty}', '{{"(,)"},{"[1,)"}}', difference="range"),
    *_cases("array", "tstzrange[]", '{"[2024-01-02 03:04:05+00,)",NULL}', difference="range"),
    *_cases("void", "void", "", difference="void"),
]

DIFFERENCE_KEYS: Final[frozenset[str]] = frozenset(DIFFERENCES)


def _literal_sql(case: Case) -> str:
    if case.key == "void":
        return "SELECT pg_sleep(0)"
    return f"SELECT '{case.literal}'::{case.sql_type}"


def _case_id(case: Case) -> str:
    return f"{case.key}-{case.sql_type}-{case.literal[:30]}"


def _rowx_outcome(dsn: str, sql: str) -> tuple[str, Any]:
    async def run() -> tuple[str, Any]:
        conn = await connect(dsn)
        try:
            rows = await conn._impl._fetch(sql, [], None, scalar=True, limit_one=True, arity=1)
        except DecodeError as exc:
            return "error", exc
        finally:
            await conn.close()
        return "ok", rows[0]

    return asyncio.run(run())


def _psycopg_outcome(dsn: str, sql: str) -> tuple[str, Any]:
    # a zone unlike UTC makes the timestamptz difference real
    options = f"{conninfo_to_dict(dsn).get('options', '')} -c TimeZone=America/Chicago"
    conninfo = make_conninfo(dsn, options=options)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        try:
            row = conn.execute(cast("LiteralString", sql)).fetchone()
        except psycopg.DataError as exc:
            return "error", exc
    assert row is not None
    return "ok", row[0]


def same(left: object, right: object) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(same, left, right, strict=True))
    if type(left) is not type(right):
        return False
    if isinstance(left, float) and isnan(left):
        return isinstance(right, float) and isnan(right)
    if isinstance(left, Decimal) and left.is_nan():
        return isinstance(right, Decimal) and right.is_nan()
    if isinstance(left, float) and left == 0:
        # -0.0 == 0.0, but the sign is part of the value
        return isinstance(right, float) and str(left) == str(right)
    return left == right


def instants_equal(left: object, right: object) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(instants_equal, left, right, strict=True))
    if isinstance(left, dt.datetime) and isinstance(right, dt.datetime):
        return left == right and left.utcoffset() == dt.timedelta(0)
    return left == right


def float32_equal(left: object, right: object) -> bool:
    if not (isinstance(left, float) and isinstance(right, float)):
        return False
    return struct.pack("<f", left) == struct.pack("<f", right)


# a naive bound compares by value, an aware one as an instant, since psycopg uses the session zone
def bounds_equal(left: object, right: object) -> bool:
    if isinstance(left, dt.datetime) and left.tzinfo is not None:
        return instants_equal(left, right)
    return left == right


def ranges_equal(left: object, right: object) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(ranges_equal, left, right, strict=True))
    if left is None or right is None:
        return left is None and right is None
    assert isinstance(left, Range)
    assert isinstance(right, PsycopgRange)
    if left.empty or right.isempty:
        return left.empty and right.isempty
    return (
        bounds_equal(left.lower, right.lower)
        and bounds_equal(left.upper, right.upper)
        and (left.lower_inc, left.upper_inc) == (right.lower_inc, right.upper_inc)
    )


def test_corpus_covers_the_type_table() -> None:
    supported = {t.key for t in type_table() if t.decode != "unsupported"}
    assert {case.key for case in CORPUS} == supported


@pytest.mark.parametrize("case", CORPUS, ids=_case_id)
def test_decode_matches_psycopg(schema: str, case: Case) -> None:
    sql = _literal_sql(case)
    status, rowx_value = _rowx_outcome(schema, sql)
    match case.difference:
        case None:
            _, psycopg_value = _psycopg_outcome(schema, sql)
            assert status == "ok", rowx_value
            assert same(rowx_value, psycopg_value), (rowx_value, psycopg_value)
        case "instant":
            _, psycopg_value = _psycopg_outcome(schema, sql)
            assert status == "ok", rowx_value
            assert instants_equal(rowx_value, psycopg_value), (rowx_value, psycopg_value)
        case "float32":
            _, psycopg_value = _psycopg_outcome(schema, sql)
            assert status == "ok", rowx_value
            assert float32_equal(rowx_value, psycopg_value), (rowx_value, psycopg_value)
        case "decode-error":
            assert status == "error", rowx_value
        case "host-address":
            _, psycopg_value = _psycopg_outcome(schema, sql)
            assert status == "ok", rowx_value
            assert isinstance(rowx_value, ipaddress.IPv4Interface | ipaddress.IPv6Interface)
            assert isinstance(psycopg_value, ipaddress.IPv4Address | ipaddress.IPv6Address)
            assert rowx_value.ip == psycopg_value
            assert rowx_value.network.num_addresses == 1
        case "range":
            _, psycopg_value = _psycopg_outcome(schema, sql)
            assert status == "ok", rowx_value
            assert ranges_equal(rowx_value, psycopg_value), (rowx_value, psycopg_value)
        case "void":
            assert (status, rowx_value) == ("ok", None)
            assert _psycopg_outcome(schema, sql) == ("ok", "")


def test_psycopg_approximates_a_month_as_thirty_days(schema: str) -> None:
    assert _psycopg_outcome(schema, "SELECT '1 mon'::interval") == ("ok", dt.timedelta(days=30))


def test_psycopg_rejects_infinite_dates_and_timestamps(schema: str) -> None:
    for sql_type in ("date", "timestamp", "timestamptz"):
        status, _ = _psycopg_outcome(schema, f"SELECT 'infinity'::{sql_type}")
        assert status == "error", sql_type


def test_difference_keys_name_corpus_cases() -> None:
    # an array of timestamptz is covered by the timestamptz entry
    differing = {case.key for case in CORPUS if case.difference is not None and case.key != "array"}
    assert differing == DIFFERENCE_KEYS


def test_difference_list_matches_doc() -> None:
    text = TYPES_DOC.read_text()
    section = text.split(DOC_HEADING, 1)[1]
    # a bullet may wrap onto indented continuation lines
    bullets = " ".join(section.splitlines()).split("- `")[1:]
    documented = {}
    for bullet in bullets:
        key, text = bullet.split("`: ", 1)
        documented[key] = " ".join(text.split())
    assert documented == DIFFERENCES
