import datetime as dt
import ipaddress
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from rowx_postgres import Box, Circle, Cube, Line, LineSegment, Money, Path, Point, Polygon, Range

UTC: Final[dt.timezone] = dt.UTC

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
    "hstore": ("public.hstore", 'a=>1, b=>NULL, "c d"=>"e"', {"a": "1", "b": None, "c d": "e"}),
    "ltree": ("public.ltree", "Top.Science.Astronomy", "Top.Science.Astronomy"),
    "lquery": ("public.lquery", "Top.*{1,2}.Astronomy", "Top.*{1,2}.Astronomy"),
    "cube": ("public.cube", "(1,2),(3,4)", Cube((1.0, 2.0), (3.0, 4.0))),
    "json": ("json", '{"a": [1, null]}', {"a": [1, None]}),
    "jsonb": ("jsonb", '{"a": [1, null]}', {"a": [1, None]}),
    "enum": ("mood", "happy", "happy"),
    "array": ("int[]", "{1,NULL,3}", [1, None, 3]),
    "void": ("void", "", None),
}
