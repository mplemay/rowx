import asyncio
import datetime as dt
import ipaddress
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import pytest

from rowx._core.postgres import type_table
from rowx.errors import DatabaseError, DecodeError
from rowx.postgres import Box, Circle, Line, LineSegment, Money, Path, Point, Polygon, Range, connect

pytestmark = [pytest.mark.integration, pytest.mark.db]

UTC: Final = dt.UTC

# key -> (SQL type, SQL literal, expected Python value); one entry per supported row of the type table
DECODE_GOLDENS: Final[dict[str, tuple[str, str, Any]]] = {
    "bool": ("bool", "true", True),
    "int2": ("int2", "-32768", -32768),
    "int4": ("int4", "2147483647", 2147483647),
    "int8": ("int8", "-9223372036854775808", -(2**63)),
    "oid": ("oid", "4000000000", 4_000_000_000),
    "float4": ("float4", "1.5", 1.5),
    "float8": ("float8", "-2.25", -2.25),
    "numeric": ("numeric", "1.50", Decimal("1.50")),
    "text": ("text", "héllo", "héllo"),
    "varchar": ("varchar", "var", "var"),
    "bpchar": ("char(4)", "ab", "ab  "),
    "name": ("name", "nm", "nm"),
    "citext": ("public.citext", "MiXed", "MiXed"),
    "char": ('"char"', "x", "x"),
    "bytea": ("bytea", "\\x00ff", b"\x00\xff"),
    "date": ("date", "2024-02-29", dt.date(2024, 2, 29)),
    "time": ("time", "01:02:03.000004", dt.time(1, 2, 3, 4)),
    "timetz": ("timetz", "01:02:03+02", dt.time(1, 2, 3, tzinfo=dt.timezone(dt.timedelta(hours=2)))),
    "timestamp": ("timestamp", "2024-01-02 03:04:05", dt.datetime(2024, 1, 2, 3, 4, 5)),  # ruff: ignore[call-datetime-without-tzinfo]
    "timestamptz": ("timestamptz", "2024-01-02 03:04:05+00", dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)),
    "interval": ("interval", "1 day 02:00:00", dt.timedelta(days=1, hours=2)),
    "uuid": ("uuid", "12345678-1234-5678-1234-567812345678", UUID("12345678-1234-5678-1234-567812345678")),
    "inet": ("inet", "192.168.0.1/24", ipaddress.IPv4Interface("192.168.0.1/24")),
    "cidr": ("cidr", "2001:db8::/32", ipaddress.IPv6Network("2001:db8::/32")),
    "macaddr": ("macaddr", "08:00:2B:01:02:03", "08:00:2b:01:02:03"),
    "macaddr8": ("macaddr8", "08:00:2b:01:02:03", "08:00:2b:ff:fe:01:02:03"),
    "int4range": ("int4range", "[1,5]", Range(1, 6)),
    "int8range": ("int8range", "(,9)", Range(None, 9)),
    "numrange": ("numrange", "(1.50,2.5]", Range(Decimal("1.50"), Decimal("2.5"), lower_inc=False, upper_inc=True)),
    "daterange": ("daterange", "[2024-02-28,2024-03-01]", Range(dt.date(2024, 2, 28), dt.date(2024, 3, 2))),
    "tsrange": (
        "tsrange",
        "[2024-01-02 03:04:05,)",
        Range(dt.datetime(2024, 1, 2, 3, 4, 5), None),  # ruff: ignore[call-datetime-without-tzinfo]
    ),
    "tstzrange": (
        "tstzrange",
        "[2024-01-02 03:04:05+00,2024-01-03 03:04:05+00)",
        Range(dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC), dt.datetime(2024, 1, 3, 3, 4, 5, tzinfo=UTC)),
    ),
    "money": ("money", "12.34", Money(1234)),
    "bit": ("bit(9)", "101010101", "101010101"),
    "varbit": ("varbit", "1011", "1011"),
    "point": ("point", "(1.5,-2)", Point(1.5, -2.0)),
    "line": ("line", "{1,-2,3}", Line(1.0, -2.0, 3.0)),
    "lseg": ("lseg", "[(0,0),(1,2)]", LineSegment(Point(0.0, 0.0), Point(1.0, 2.0))),
    # the server stores the upper right corner first
    "box": ("box", "(0,0),(2,3)", Box(Point(2.0, 3.0), Point(0.0, 0.0))),
    "path": ("path", "[(0,0),(1,1)]", Path((Point(0.0, 0.0), Point(1.0, 1.0)), closed=False)),
    "polygon": ("polygon", "((0,0),(1,0),(1,1))", Polygon((Point(0.0, 0.0), Point(1.0, 0.0), Point(1.0, 1.0)))),
    "circle": ("circle", "<(1,2),3>", Circle(1.0, 2.0, 3.0)),
    "json": ("json", '{"a": [1, null]}', {"a": [1, None]}),
    "jsonb": ("jsonb", '{"a": [1, null]}', {"a": [1, None]}),
    "enum": ("mood", "happy", "happy"),
    "array": ("int[]", "{1,NULL,3}", [1, None, 3]),
    "void": ("void", "", None),
}

# key -> (SQL type, Python value); the value must survive a bind and a read back
ENCODE_GOLDENS: Final[dict[str, tuple[str, Any]]] = {
    "bool": ("bool", False),
    "int2": ("int2", 32767),
    "int4": ("int4", -2147483648),
    "int8": ("int8", 2**63 - 1),
    "oid": ("oid", 4_000_000_000),
    "float4": ("float4", 0.5),
    "float8": ("float8", 1e300),
    "numeric": ("numeric", Decimal("-0.0010")),
    "text": ("text", "héllo ✓"),
    "varchar": ("varchar", "var"),
    "bpchar": ("char(4)", "ab  "),
    "name": ("name", "nm"),
    "citext": ("public.citext", "MiXed"),
    "char": ('"char"', "x"),
    "bytea": ("bytea", b"\x00\xff"),
    "date": ("date", dt.date(1, 1, 1)),
    "time": ("time", dt.time(23, 59, 59, 999999)),
    "timetz": ("timetz", dt.time(1, 2, 3, tzinfo=dt.timezone(dt.timedelta(hours=-5)))),
    "timestamp": ("timestamp", dt.datetime(2024, 1, 2, 3, 4, 5, 6)),  # ruff: ignore[call-datetime-without-tzinfo]
    "timestamptz": ("timestamptz", dt.datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=UTC)),
    "interval": ("interval", dt.timedelta(days=-1, seconds=5, microseconds=7)),
    "uuid": ("uuid", UUID("12345678-1234-5678-1234-567812345678")),
    "inet": ("inet", ipaddress.IPv6Interface("2001:db8::1/64")),
    "cidr": ("cidr", ipaddress.IPv4Network("10.0.0.0/8")),
    "macaddr": ("macaddr", "08:00:2b:01:02:03"),
    "macaddr8": ("macaddr8", "08:00:2b:01:02:03:04:05"),
    "int4range": ("int4range", Range(-5, 5, upper_inc=False)),
    "int8range": ("int8range", Range(None, None)),
    "numrange": ("numrange", Range(Decimal("-0.0010"), Decimal("1E+3"), lower_inc=False)),
    "daterange": ("daterange", Range(dt.date(1, 1, 1), dt.date(9999, 12, 31))),
    "tsrange": ("tsrange", Range(dt.datetime(2024, 1, 2, 3, 4, 5, 6), None)),  # ruff: ignore[call-datetime-without-tzinfo]
    "tstzrange": ("tstzrange", Range(None, dt.datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=UTC))),
    "money": ("money", Money(-1234)),
    "bit": ("bit(9)", "101010101"),
    "varbit": ("varbit", "1011"),
    "point": ("point", Point(-0.5, 1e300)),
    "line": ("line", Line(0.0, 1.0, -4.5)),
    "lseg": ("lseg", LineSegment(Point(-1.0, -1.0), Point(1.0, 1.0))),
    "box": ("box", Box(Point(2.0, 3.0), Point(-1.0, 0.0))),
    "path": ("path", Path((Point(0.0, 0.0), Point(1.0, 2.0), Point(3.0, 4.0)), closed=True)),
    "polygon": ("polygon", Polygon((Point(0.0, 0.0), Point(4.0, 0.0), Point(0.0, 4.0)))),
    "circle": ("circle", Circle(0.0, 0.0, 0.5)),
    "json": ("json", {"a": [1, None]}),
    "jsonb": ("jsonb", {"a": [1, None]}),
    "enum": ("mood", "ok"),
    "array": ("int[]", [[1, 2], [3, None]]),
}

# rows with nothing to bind; void is only ever read
ENCODE_EXEMPT: Final[frozenset[str]] = frozenset({"void"})

# type -> literal that parses; none has a codec so reading one must fail cleanly
UNSUPPORTED_DECODE: Final[dict[str, str]] = {
    "xml": "<a/>",
    "tsvector": "a",
    "int4multirange": "{[1,5)}",
}


def supported(side: str) -> set[str]:
    return {t.key for t in type_table() if getattr(t, side) != "unsupported"}


def fetch_scalar(dsn: str, sql: str, params: list[object] | None = None) -> list[object]:
    async def run() -> list[object]:
        conn = await connect(dsn)
        try:
            return await conn._impl._fetch(sql, params or [], None, scalar=True, limit_one=True, arity=1)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_table_coverage() -> None:
    table = type_table()
    # a row is supported unless the table says so; no supported row may keep an unsupported codec
    for row in table:
        if row.py_result:
            assert row.decode != "unsupported", f"{row.key} is supported but has no decode codec"
            assert row.encode != "unsupported" or row.key in ENCODE_EXEMPT, f"{row.key} has no encode codec"
        else:
            assert row.decode == row.encode == "unsupported", f"{row.key} has codecs but no py_result"
    assert set(DECODE_GOLDENS) == supported("decode"), "decode goldens differ from the table"
    assert set(ENCODE_GOLDENS) == supported("encode") - ENCODE_EXEMPT, "encode goldens differ from the table"
    assert {row.key for row in table} >= ENCODE_EXEMPT


@pytest.mark.parametrize("key", sorted(DECODE_GOLDENS))
def test_decode_golden(schema: str, key: str) -> None:
    sql_type, literal, expected = DECODE_GOLDENS[key]
    sql = f"SELECT '{literal}'::{sql_type}" if key != "void" else "SELECT pg_sleep(0)"
    assert fetch_scalar(schema, sql) == [expected]


@pytest.mark.parametrize("key", sorted(ENCODE_GOLDENS))
def test_encode_golden(schema: str, key: str) -> None:
    sql_type, value = ENCODE_GOLDENS[key]
    assert fetch_scalar(schema, f"SELECT $1::{sql_type}", [value]) == [value]


@pytest.mark.parametrize("sql_type", sorted(UNSUPPORTED_DECODE))
def test_unsupported_type_is_decode_error(schema: str, sql_type: str) -> None:
    with pytest.raises(DecodeError, match=sql_type):
        fetch_scalar(schema, f"SELECT '{UNSUPPORTED_DECODE[sql_type]}'::{sql_type}")


def test_aclitem_has_no_binary_send(schema: str) -> None:
    with pytest.raises(DatabaseError) as info:
        # built from role oids (PUBLIC by the bootstrap superuser), so no role name has to exist
        fetch_scalar(schema, "SELECT makeaclitem(0, 10, 'SELECT', false)")
    assert info.value.sqlstate == "42883"
