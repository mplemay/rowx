import asyncio
import datetime as dt
import decimal
import ipaddress
import re
import struct
import sys
import time
import tracemalloc
from array import array as std_array
from contextlib import suppress
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, time as time_of_day, timedelta, timezone
from decimal import Decimal, DecimalTuple
from ipaddress import ip_address, ip_network
from itertools import starmap
from math import isnan
from pathlib import Path as FilePath
from random import Random
from typing import TYPE_CHECKING, Any, Final, Literal, LiteralString, cast, override
from uuid import UUID

import psycopg
import pytest
import rowx_postgres as postgres
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types import TypeInfo as PsycopgTypeInfo
from psycopg.types.hstore import register_hstore
from psycopg.types.range import Range as PsycopgRange
from rowx_core.errors import DatabaseError, DecodeError, EncodeError, InterfaceError, RowConstructError, RowShapeError
from rowx_postgres import (
    Box,
    Circle,
    Connection,
    Cube,
    Interval,
    Line,
    LineSegment,
    Money,
    Path,
    Point,
    Polygon,
    Pool,
    Range,
    connect,
)
from rowx_postgres.__tests__.support.type_goldens import DECODE_GOLDENS
from rowx_postgres._core import Connection as ConnectionImpl, RowShape, TypeInfo, _decode, type_table

from rowx import query

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

# from _core/decode/test__core.py

type Spec = int | tuple[object, ...]


def be32(*values: int) -> bytes:
    return b"".join(struct.pack(">i", value) for value in values)


def be16(*values: int) -> bytes:
    return b"".join(struct.pack(">H", value) for value in values)


def numeric(ndigits: int, weight: int, sign: int, scale: int, *digits: int) -> bytes:
    return be16(ndigits) + struct.pack(">h", weight) + be16(sign, scale, *digits)


def array(oid: int, dims: list[tuple[int, int]], *elements: bytes | None, flags: int = 0) -> bytes:
    out = be32(len(dims), flags, oid)
    for length, lower in dims:
        out += be32(length, lower)
    for element in elements:
        out += be32(-1) if element is None else be32(len(element)) + element
    return out


def sized(raw: bytes) -> bytes:
    return be32(len(raw)) + raw


def floats(*values: float) -> bytes:
    return b"".join(struct.pack(">d", value) for value in values)


def fixed(length: int) -> list[bytes]:
    return [b"\x00" * wrong for wrong in sorted({0, 1, length - 1, length + 1, length + 8} - {length})]


INVALID_UTF8: Final[list[bytes]] = [b"\xff", b"\xc3", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\xc0\x80", b"ok\xe2\x82"]
NAN_PAYLOADS: Final[list[bytes]] = [
    struct.pack(">I", 0x7FC00001),
    struct.pack(">I", 0x7F800001),
    struct.pack(">Q", 0x7FF8000000000001),
    struct.pack(">Q", 0xFFF0000000000001),
]
HUGE: Final[int] = 0x7FFFFFFF
INET_BAD: Final[list[bytes]] = [
    b"",
    b"\x02\x20\x00",
    b"\x02\x20\x00\x04\x7f\x00\x00",
    b"\x02\x20\x00\x04\x7f\x00\x00\x01\x00",
    b"\x09\x20\x00\x04\x7f\x00\x00\x01",
    b"\x02\x21\x00\x04\x7f\x00\x00\x01",
    b"\x02\x20\x02\x04\x7f\x00\x00\x01",
    b"\x02\x20\x00\x10\x7f\x00\x00\x01",
    b"\x03\x81\x00\x10" + b"\x00" * 16,
    b"\x03\x80\x00\x04" + b"\x00" * 16,
    b"\xff" * 20,
]
BITS_BAD: Final[list[bytes]] = [
    b"",
    b"\x00\x00",
    be32(-1),
    be32(-HUGE - 1),
    be32(HUGE),
    be32(8),
    be32(0) + b"\x00",
    be32(9) + b"\x00",
    be32(8) + b"\x00\x00",
    be32(83_886_081) + b"\x00",
]
POINTS_BAD: Final[list[bytes]] = [
    b"",
    be32(0),
    be32(-1),
    be32(HUGE),
    be32(-HUGE - 1),
    be32(2) + floats(0, 0),
    be32(1) + floats(0),
]
HSTORE_BAD: Final[list[bytes]] = [
    b"",
    b"\x00\x00",
    be32(-1),
    be32(HUGE),
    be32(1),
    be32(1) + be32(-1),
    be32(1) + be32(HUGE) + b"k",
    be32(1) + be32(1) + b"k",
    be32(1) + be32(1) + b"k" + be32(-2),
    be32(1) + be32(1) + b"k" + be32(HUGE),
    be32(1) + be32(1) + b"\xff" + be32(-1),
    be32(1) + be32(1) + b"k" + be32(1) + b"\xff",
    be32(0) + b"\x00",
    be32(1) + be32(-HUGE - 1),
]
CUBE_BAD: Final[list[bytes]] = [
    b"",
    b"\x00\x00\x00",
    be32(101) + floats(*[0.0] * 202),
    be32(HUGE),
    be32(2) + floats(0.0),
    be32(2) + floats(0.0, 0.0, 0.0, 0.0, 0.0),
    struct.pack(">I", 0x80000002) + floats(0.0),
    struct.pack(">I", 0x80000000 | HUGE),
]
GEOMETRIC_BAD: Final[dict[str, list[bytes]]] = {
    "point": fixed(16),
    "line": fixed(24),
    "lseg": fixed(32),
    "box": fixed(32),
    "circle": fixed(24),
    "path": [b"", b"\x01", *(b"\x01" + raw for raw in POINTS_BAD)],
    "polygon": POINTS_BAD,
}
LTREE_BAD: Final[list[bytes]] = [b"", b"\x00a", b"\x02a", b"\xffa", b"\x01\xff", b"\x01a\xc3"]
JSON_BAD: Final[list[bytes]] = [b"", b"{", b"nul", b'{"a":', b"\xff", b'"\xff"', b"[1,]", b"\x00"]
JSONB_BAD: Final[list[bytes]] = [b"", b"\x00", b"\x02{}", b"\xff{}", b"\x01", b"\x01{", b"\x01\xff", b"\x01[1,]"]
NUMERIC_BAD: Final[list[bytes]] = [
    b"",
    b"\x00" * 7,
    numeric(1, 0, 0, 0),
    numeric(0xFFFF, 0, 0, 0),
    numeric(0xFFFF, 0, 0, 0, 1),
    numeric(1, 0, 0x1234, 0, 1),
    numeric(1, 0, 0, 0, 10_000),
    numeric(1, 0, 0, 0, 0xFFFF),
    numeric(1, 0, 0, 16_384, 1),
    numeric(1, 0, 0, 0xFFFF, 1),
    numeric(0, 0, 0xC000, 0, 1),
    numeric(1, 0, 0, 0, 1) + b"\x00",
    numeric(2, 0, 0, 0, 1),
]

# payloads that must be refused, per decodable type key (an array or a range adds the same payloads for its element)
MUST_FAIL: Final[dict[str, list[bytes]]] = {
    "bool": fixed(1),
    "int2": fixed(2),
    "int4": fixed(4),
    "int8": fixed(8),
    "oid": fixed(4),
    "float4": fixed(4),
    "float8": fixed(8),
    "numeric": NUMERIC_BAD,
    "text": INVALID_UTF8,
    "varchar": INVALID_UTF8,
    "bpchar": INVALID_UTF8,
    "name": INVALID_UTF8,
    "citext": INVALID_UTF8,
    "char": [b"", b"ab", b"\xff"],
    "enum": INVALID_UTF8,
    "uuid": fixed(16),
    "inet": INET_BAD,
    "cidr": INET_BAD,
    "macaddr": fixed(6),
    "macaddr8": fixed(8),
    "date": [*fixed(4), be32(HUGE), be32(-HUGE - 1), be32(2_000_000_000), be32(-2_000_000_000)],
    "time": [*fixed(8), struct.pack(">q", -1), struct.pack(">q", 86_400_000_000), struct.pack(">q", 2**63 - 1)],
    "timetz": [
        *fixed(12),
        struct.pack(">qi", -1, 0),
        struct.pack(">qi", 86_400_000_000, 0),
        struct.pack(">qi", 0, 86_400),
        struct.pack(">qi", 0, -(2**31)),
    ],
    "timestamp": [*fixed(8), struct.pack(">q", 2**63 - 1), struct.pack(">q", -(2**63)), struct.pack(">q", 2**62)],
    "timestamptz": [*fixed(8), struct.pack(">q", 2**63 - 1), struct.pack(">q", -(2**63)), struct.pack(">q", -(2**62))],
    "interval": [
        *fixed(16),
        struct.pack(">qii", 0, 0, 1),
        struct.pack(">qii", 2**63 - 1, 2**31 - 1, 2**31 - 1),
    ],
    "money": fixed(8),
    "bit": BITS_BAD,
    "varbit": BITS_BAD,
    "json": JSON_BAD,
    "jsonb": JSONB_BAD,
    "hstore": HSTORE_BAD,
    "ltree": LTREE_BAD,
    "lquery": LTREE_BAD,
    "cube": CUBE_BAD,
    **GEOMETRIC_BAD,
}
# every byte string is a bytea
ANYTHING_GOES: Final[frozenset[str]] = frozenset({"bytea"})

RANGE_BAD: Final[list[bytes]] = [
    b"",
    b"\x80",
    b"\xff",
    b"\x01\x00",
    b"\x03",
    b"\x02",
    b"\x02" + be32(-1),
    b"\x02" + be32(HUGE),
    b"\x02" + be32(4) + b"\x00",
    b"\x08\x04" + be32(-1),
    b"\x02\x14" + be32(0),
    b"\x18\x00",
    b"\x00" + sized(b"\x00" * 4),
]


def array_bad(oid: int) -> list[bytes]:
    return [
        b"",
        be32(1, 0, oid)[:-1],
        be32(7, 0, oid),
        be32(-1, 0, oid),
        be32(-HUGE - 1, 0, oid),
        be32(1, 0, oid) + be32(1),
        be32(1, 0, oid) + be32(-1, 1),
        be32(1, 0, oid) + be32(HUGE, 1),
        be32(1, 0, oid) + be32(1, 1),
        be32(1, 0, oid) + be32(1, 1) + be32(-2),
        be32(1, 0, oid) + be32(1, 1) + be32(HUGE),
        be32(1, 0, oid) + be32(1, 1) + be32(4) + b"\x00",
        be32(2, 0, oid) + be32(HUGE, 1) + be32(HUGE, 1),
        be32(3, 0, oid) + be32(HUGE, 1) * 3,
        be32(6, 0, oid) + be32(65_536, 1) * 6,
        array(oid, [(1, 1)], b"\x00\x00\x00\x01") + b"\x00",
        array(oid, [(2, 1)], b"\x00\x00\x00\x01"),
        be32(0, 0, oid) + b"\x00",
        be32(1, 0, oid) + be32(0, 1) + be32(1),
        be32(2, 0, oid) + be32(HUGE, 1) + be32(0, 1) + sized(b"\x00"),
        be32(3, 0, oid) + be32(65_536, 1) + be32(65_536, 1) + be32(0, 1) + sized(b"\x00" * 4),
    ]


def decodable(info: TypeInfo) -> bool:
    return info.decode not in {"unsupported", "void"}


def scalar_spec(info: TypeInfo) -> Spec | None:
    match info.matcher:
        case "oid" if info.matcher_oid is not None:
            return info.matcher_oid
        case "range" if info.matcher_oid is not None:
            return ("range", info.matcher_oid)
        case "extension" if info.matcher_name is not None:
            return ("name", info.matcher_name)
        case "enum":
            return ("enum",)
        case _:
            return None


def scalar_types() -> list[tuple[str, Spec]]:
    return [(info.key, spec) for info in type_table() if decodable(info) and (spec := scalar_spec(info)) is not None]


def malformed(key: str) -> list[bytes]:
    info = next(t for t in type_table() if t.key == key)
    if info.matcher == "range" and info.matcher_oid is not None:
        subtype = next(k for k, s in scalar_types() if s == info.matcher_oid)
        return [*RANGE_BAD, *(b"\x02\x04" + sized(raw) + sized(raw) for raw in MUST_FAIL.get(subtype, [])[:6])]
    return MUST_FAIL[key]


SCALARS: Final[list[tuple[str, Spec]]] = scalar_types()
# the oid is not read from the container, so any value stands in for the element type's own
ARRAY_OID: Final[int] = 23


def test_every_decodable_type_has_malformed_payloads() -> None:
    keys = {info.key for info in type_table() if decodable(info) and scalar_spec(info) is not None}
    ranges = {info.key for info in type_table() if info.matcher == "range"}
    assert keys - ranges - ANYTHING_GOES <= MUST_FAIL.keys()
    assert {info.key for info in type_table() if info.matcher in {"array", "domain"}} == {"array", "domain"}


@pytest.mark.parametrize(("key", "spec"), SCALARS, ids=[key for key, _ in SCALARS])
def test_a_malformed_value_raises_decode_error(key: str, spec: Spec) -> None:
    if key in ANYTHING_GOES:
        assert _decode(spec, b"\xff\x00") == b"\xff\x00"
        return
    payloads = malformed(key)
    assert payloads
    for raw in payloads:
        with pytest.raises(DecodeError):
            _decode(spec, raw)


@pytest.mark.parametrize("payload", NAN_PAYLOADS, ids=lambda raw: raw.hex())
def test_a_nan_payload_decodes_to_a_float(payload: bytes) -> None:
    oid = 700 if len(payload) == 4 else 701
    value = _decode(oid, payload)
    assert isinstance(value, float)
    assert value != value  # ruff: ignore[comparison-with-itself]


@pytest.mark.parametrize("key", [key for key, _ in SCALARS if key != "bytea"])
def test_a_malformed_array_raises_decode_error(key: str) -> None:
    spec = dict(SCALARS)[key]
    for raw in array_bad(ARRAY_OID):
        with pytest.raises(DecodeError):
            _decode(("array", spec), raw)
    # a well formed container around a malformed element names the element's error
    for element in malformed(key)[:8]:
        with pytest.raises(DecodeError):
            _decode(("array", spec), array(ARRAY_OID, [(1, 1)], element))
        with pytest.raises(DecodeError):
            _decode(("array", spec), array(ARRAY_OID, [(2, 1), (1, 1)], None, element))


@pytest.mark.parametrize("key", [key for key, _ in SCALARS if key != "bytea"])
def test_a_malformed_domain_value_raises_decode_error(key: str) -> None:
    spec = dict(SCALARS)[key]
    for raw in malformed(key)[:4]:
        with pytest.raises(DecodeError):
            _decode(("domain", spec), raw)


def test_a_declared_count_larger_than_the_input_does_not_allocate() -> None:
    huge = be32(1, 0, ARRAY_OID) + be32(HUGE, 1)
    for _ in range(50):
        with pytest.raises(DecodeError):
            _decode(("array", 25), huge)
    with pytest.raises(DecodeError):
        _decode(("name", "hstore"), be32(HUGE))
    with pytest.raises(DecodeError):
        _decode(604, be32(HUGE))


ZERO_DIMENSION_SHAPES: Final[list[list[tuple[int, int]]]] = [
    [(HUGE, 1), (0, 1)],
    [(10_000_000, 1), (0, 1)],
    [(65_536, 1), (65_536, 1), (0, 1)],
    [(0, 1)],
]


def decode_zero_dimension_arrays() -> None:
    for dims in ZERO_DIMENSION_SHAPES:
        raw = be32(len(dims), 0, ARRAY_OID) + b"".join(starmap(be32, dims))
        assert _decode(("array", 25), raw) == []


def test_a_zero_length_dimension_is_an_empty_array_without_allocating() -> None:
    tracemalloc.start()
    try:
        decode_zero_dimension_arrays()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000


@pytest.mark.timing
def test_a_zero_length_dimension_is_an_empty_array_decoded_quickly() -> None:
    started = time.perf_counter()
    decode_zero_dimension_arrays()
    assert time.perf_counter() - started < 1


def test_a_spec_nested_past_the_depth_cap_is_refused() -> None:
    spec: Spec = 23
    for _ in range(17):
        spec = ("array", spec)
    with pytest.raises(ValueError, match="nests deeper"):
        _decode(spec, b"")
    deep: Spec = 23
    for _ in range(100_000):
        deep = ("array", deep)
    with pytest.raises(ValueError, match="nests deeper"):
        _decode(deep, b"")


@pytest.mark.parametrize("oid", [114, 3802])
def test_json_nested_past_the_recursion_limit_is_a_decode_error(oid: int) -> None:
    body = b"[" * 200_000 + b"]" * 200_000
    with pytest.raises(DecodeError):
        _decode(oid, (b"\x01" if oid == 3802 else b"") + body)


def test_a_well_formed_container_still_decodes() -> None:
    assert _decode(("array", 23), array(23, [(2, 1)], be32(1), None)) == [1, None]
    assert _decode(("array", 23), be32(0, 0, 23)) == []
    assert _decode(("name", "hstore"), be32(1) + be32(1) + b"k" + be32(-1)) == {"k": None}
    assert _decode(("name", "ltree"), b"\x01a.b") == "a.b"


@pytest.mark.parametrize("key", [key for key, _ in SCALARS])
def test_random_bytes_never_panic(key: str) -> None:
    spec = dict(SCALARS)[key]
    rng = Random(f"rowx-{key}")  # ruff: ignore[suspicious-non-cryptographic-random-usage]
    for _ in range(400):
        raw = rng.randbytes(rng.randrange(0, 40))
        for wrapped in (spec, ("array", spec), ("domain", spec)):
            with suppress(DecodeError):
                _decode(wrapped, raw)
    # mutations of a plausible array container reach the element decoders
    for _ in range(200):
        elements = [rng.randbytes(rng.randrange(0, 20)) for _ in range(rng.randrange(0, 4))]
        raw = bytearray(array(ARRAY_OID, [(len(elements), 1)], *elements))
        if raw:
            raw[rng.randrange(len(raw))] = rng.randrange(256)
        with suppress(DecodeError):
            _decode(("array", spec), bytes(raw))


# from _core/encode/test__core.py

UTC: Final[dt.timezone] = dt.UTC
MIB: Final[int] = 1 << 20


def fetch_scalar_params(dsn: str, sql: str, params: list[object]) -> object:
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
@pytest.mark.db
def test_integer_limits_round_trip(schema: str, sql_type: str) -> None:
    low, high = INT_LIMITS[sql_type]
    for value in (low, high):
        assert fetch_scalar_params(schema, f"SELECT $1::{sql_type}", [value]) == value


@pytest.mark.parametrize("sql_type", sorted(INT_LIMITS))
@pytest.mark.db
def test_integer_beyond_limits_is_encode_error(schema: str, sql_type: str) -> None:
    low, high = INT_LIMITS[sql_type]
    for value in (low - 1, high + 1):
        with pytest.raises(EncodeError, match=f"out of range for {sql_type}"):
            fetch_scalar_params(schema, f"SELECT $1::{sql_type}", [value])


@pytest.mark.parametrize("sql_type", ["float4", "float8"])
@pytest.mark.db
def test_float_specials_round_trip(schema: str, sql_type: str) -> None:
    nan = fetch_scalar_params(schema, f"SELECT $1::{sql_type}", [float("nan")])
    assert isinstance(nan, float)
    assert isnan(nan)
    for value in (float("inf"), float("-inf")):
        assert fetch_scalar_params(schema, f"SELECT $1::{sql_type}", [value]) == value
    negative_zero = fetch_scalar_params(schema, f"SELECT $1::{sql_type}", [-0.0])
    assert negative_zero == 0
    assert isinstance(negative_zero, float)
    assert str(negative_zero) == "-0.0"


@pytest.mark.db
def test_float4_overflow_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match="out of range for float4"):
        fetch_scalar_params(schema, "SELECT $1::float4", [1e300])


@pytest.mark.db
def test_decimal_signalling_nan_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match="sNaN"):
        fetch_scalar_params(schema, "SELECT $1::numeric", [Decimal("sNaN")])


@pytest.mark.parametrize("value", [Decimal("1E+1000"), Decimal("1E-1000"), Decimal("-1.5E+1000"), Decimal("1E+100000")])
@pytest.mark.db
def test_decimal_large_exponent_round_trips(schema: str, value: Decimal) -> None:
    assert fetch_scalar_params(schema, "SELECT $1::numeric", [value]) == value


@pytest.mark.db
def test_decimal_beyond_numeric_range_is_database_error(schema: str) -> None:
    with pytest.raises(DatabaseError) as info:
        fetch_scalar_params(schema, "SELECT $1::numeric", [Decimal("1E+200000")])
    assert info.value.sqlstate == "22003"


NAIVE_DATETIME: Final[dt.datetime] = dt.datetime(2024, 1, 2, 3, 4, 5)  # ruff: ignore[call-datetime-without-tzinfo]
AWARE_DATETIME: Final[dt.datetime] = NAIVE_DATETIME.replace(tzinfo=dt.timezone(dt.timedelta(hours=-5)))
NAIVE_TIME: Final[dt.time] = dt.time(1, 2, 3)
AWARE_TIME: Final[dt.time] = NAIVE_TIME.replace(tzinfo=UTC)


@pytest.mark.db
def test_naive_and_aware_datetime(schema: str) -> None:
    assert fetch_scalar_params(schema, "SELECT $1::timestamp", [NAIVE_DATETIME]) == NAIVE_DATETIME
    # an aware value is stored as an instant and read back in UTC
    echoed = fetch_scalar_params(schema, "SELECT $1::timestamptz", [AWARE_DATETIME])
    assert echoed == AWARE_DATETIME
    assert isinstance(echoed, dt.datetime)
    assert echoed.utcoffset() == dt.timedelta(0)
    with pytest.raises(EncodeError, match="naive"):
        fetch_scalar_params(schema, "SELECT $1::timestamptz", [NAIVE_DATETIME])
    with pytest.raises(EncodeError, match="timezone-aware"):
        fetch_scalar_params(schema, "SELECT $1::timestamp", [AWARE_DATETIME])


@pytest.mark.db
def test_naive_and_aware_time(schema: str) -> None:
    assert fetch_scalar_params(schema, "SELECT $1::time", [NAIVE_TIME]) == NAIVE_TIME
    assert fetch_scalar_params(schema, "SELECT $1::timetz", [AWARE_TIME]) == AWARE_TIME
    with pytest.raises(EncodeError, match="naive"):
        fetch_scalar_params(schema, "SELECT $1::timetz", [NAIVE_TIME])
    with pytest.raises(EncodeError, match="timezone-aware"):
        fetch_scalar_params(schema, "SELECT $1::time", [AWARE_TIME])


@pytest.mark.parametrize("sql_type", ["text", "varchar", "jsonb"])
@pytest.mark.db
def test_str_with_nul_is_rejected(schema: str, sql_type: str) -> None:
    value = "a\x00b"
    if sql_type == "jsonb":
        # json.dumps escapes NUL, so the server is the one that rejects it
        with pytest.raises(DatabaseError) as info:
            fetch_scalar_params(schema, "SELECT $1::jsonb", [value])
        assert info.value.sqlstate == "22P05"
    else:
        with pytest.raises(EncodeError, match="NUL"):
            fetch_scalar_params(schema, f"SELECT $1::{sql_type}", [value])


@pytest.mark.db
def test_nul_inside_an_array_element_is_rejected(schema: str) -> None:
    with pytest.raises(EncodeError, match="NUL"):
        fetch_scalar_params(schema, "SELECT $1::text[]", [["ok", "a\x00"]])


@pytest.mark.db
def test_non_bmp_characters_round_trip(schema: str) -> None:
    value = "😀 𝄞 \U0010ffff"
    assert fetch_scalar_params(schema, "SELECT $1::text", [value]) == value
    assert fetch_scalar_params(schema, "SELECT octet_length($1::text)", [value]) == len(value.encode())


@pytest.mark.db
def test_bytes_over_one_mebibyte_round_trip(schema: str) -> None:
    value: Any = bytes(range(256)) * (4 * MIB // 256 + 1)
    assert len(value) > MIB
    assert fetch_scalar_params(schema, "SELECT $1::bytea", [value]) == value


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
@pytest.mark.db
def test_text_arrays_keep_adversarial_strings(schema: str, sql_type: str) -> None:
    values = [value for value in ADVERSARIAL if not (sql_type == "name" and "\n" in value)]
    for array in shaped(values):
        assert fetch_scalar_params(schema, f"SELECT $1::{sql_type}[]", [array]) == array


@pytest.mark.db
def test_bpchar_arrays_keep_adversarial_strings(schema: str) -> None:
    for array in shaped(ADVERSARIAL):
        assert fetch_scalar_params(schema, "SELECT $1::bpchar[]", [array]) == array


@pytest.mark.parametrize("sql_type", ["json", "jsonb"])
@pytest.mark.db
def test_json_arrays_round_trip_adversarial_documents(schema: str, sql_type: str) -> None:
    documents: list[Any] = [{"k": value, value: [value, None, 1.5]} for value in ADVERSARIAL]
    # a bare list would read as another array dimension, so lists only appear inside documents
    documents += ["just a string", 7, {}, True, {"l": []}]
    for array in shaped(documents):
        assert fetch_scalar_params(schema, f"SELECT $1::{sql_type}[]", [array]) == array


@pytest.mark.db
def test_json_parameters_are_sent_as_ascii(schema: str) -> None:
    # json.dumps runs with ensure_ascii=True, so a non-ASCII character is a \uXXXX escape and never raw UTF-8
    assert fetch_scalar_params(schema, "SELECT $1::json::text", ["ā表😀"]) == '"\\u0101\\u8868\\ud83d\\ude00"'
    assert fetch_scalar_params(schema, "SELECT $1::json::text", [{"ā": ["é"]}]) == '{"\\u0101": ["\\u00e9"]}'
    assert fetch_scalar_params(schema, "SELECT $1::json[]::text", [["ā", None]]) == '{"\\"\\\\u0101\\"",NULL}'
    assert fetch_scalar_params(schema, "SELECT $1::jsonb::text", ["ā"]) == '"ā"'


@pytest.mark.db
def test_enum_and_domain_arrays_keep_their_element_types(schema: str) -> None:
    moods = ["sad", "ok", "happy"]
    for array in shaped(moods):
        assert fetch_scalar_params(schema, "SELECT $1::mood[]", [array]) == array
    for array in shaped([1, 5, 2147483647]):
        assert fetch_scalar_params(schema, "SELECT $1::positive_int[]", [array]) == array
    assert fetch_scalar_params(schema, "SELECT $1::positive_ints", [[1, 2]]) == [1, 2]
    with pytest.raises(DatabaseError, match="positive_int"):
        fetch_scalar_params(schema, "SELECT $1::positive_int[]", [[1, -1]])
    with pytest.raises(DatabaseError):
        fetch_scalar_params(schema, "SELECT $1::mood[]", [["nope"]])


@pytest.mark.db
def test_uuid_str_elements_are_parsed_on_the_client(schema: str) -> None:
    from uuid import UUID  # ruff: ignore[import-outside-top-level]

    text = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    expected = UUID(text)
    for spelling in [text, text.upper(), text.replace("-", ""), f"{{{text}}}", "a0eebc99-9c0b4ef8-bb6d6bb9-bd380a11"]:
        assert fetch_scalar_params(schema, "SELECT $1::uuid[]", [[spelling, None, expected]]) == [
            expected,
            None,
            expected,
        ]
    for bad in ["", "not a uuid", text + "0", text[:-1], f"{text}}}", "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a1g", "'}"]:
        with pytest.raises(EncodeError, match="uuid"):
            fetch_scalar_params(schema, "SELECT $1::uuid[]", [[bad]])
    # a scalar uuid str is still handed to the server as text
    assert fetch_scalar_params(schema, "SELECT $1::uuid", [text]) == expected


@pytest.mark.db
def test_timetz_array_elements_carry_the_zone(schema: str) -> None:
    zone = dt.timezone(dt.timedelta(hours=5, minutes=30))
    values = [
        dt.time(12, 0, tzinfo=zone),
        None,
        dt.time(23, 59, 59, 999999, tzinfo=dt.timezone(-dt.timedelta(hours=7))),
    ]
    assert fetch_scalar_params(schema, "SELECT $1::timetz[]", [values]) == values
    assert fetch_scalar_params(schema, "SELECT $1::timetz[]::text", [values[:1]]) == "{12:00:00+05:30}"
    with pytest.raises(EncodeError, match="naive"):
        fetch_scalar_params(schema, "SELECT $1::timetz[]", [[dt.time(1)]])
    over = dt.time(0, tzinfo=dt.timezone(dt.timedelta(hours=16)))
    with pytest.raises(EncodeError, match="out of range"):
        fetch_scalar_params(schema, "SELECT $1::timetz[]", [[over]])


class ShiftedDecimal(Decimal):
    @override
    def as_tuple(self) -> DecimalTuple:
        return Decimal(999).as_tuple()

    @override
    def __str__(self) -> str:
        return "999"


@pytest.mark.db
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
    got = fetch_scalar_params(schema, "SELECT $1::numeric[]", [values])
    assert isinstance(got, list)
    for sent, back in zip(values, got, strict=True):
        assert back == sent
    # the display scale survives as well as the value
    assert (
        fetch_scalar_params(schema, "SELECT $1::numeric[]::text", [[Decimal("1.500"), Decimal("-0.00"), 7]])
        == "{1.500,0.00,7}"
    )
    nan = fetch_scalar_params(schema, "SELECT $1::numeric[]", [[Decimal("NaN"), Decimal("-NaN123")]])
    assert isinstance(nan, list)
    assert all(isinstance(item, Decimal) and item.is_nan() for item in nan)
    for array in shaped([Decimal("1.5"), 2, Decimal(-3)]):
        assert fetch_scalar_params(schema, "SELECT $1::numeric[]", [array]) == array


@pytest.mark.db
def test_numeric_array_decimals_that_cannot_be_encoded_are_encode_errors(schema: str) -> None:
    with pytest.raises(EncodeError, match="sNaN"):
        fetch_scalar_params(schema, "SELECT $1::numeric[]", [[Decimal(1), Decimal("sNaN")]])
    with pytest.raises(EncodeError, match="element 1"):
        fetch_scalar_params(schema, "SELECT $1::numeric[]", [[Decimal(1), Decimal("1E+131072")]])
    with pytest.raises(EncodeError, match="scale"):
        fetch_scalar_params(schema, "SELECT $1::numeric[]", [[Decimal("1E-16384")]])
    with pytest.raises(EncodeError, match="range"):
        fetch_scalar_params(schema, "SELECT $1::numeric[]", [[Decimal("1E+999999999999999999")]])
    with pytest.raises(EncodeError, match="not accepted for numeric"):
        fetch_scalar_params(schema, "SELECT $1::numeric[]", [["1.5"]])


@pytest.mark.db
def test_a_decimal_subclass_cannot_choose_what_is_encoded(schema: str) -> None:
    # as_tuple and __str__ are read through the unbound Decimal methods, so the subclass overrides are ignored
    value = ShiftedDecimal("12.5")
    assert fetch_scalar_params(schema, "SELECT $1::numeric[]", [[value]]) == [Decimal("12.5")]
    assert fetch_scalar_params(schema, "SELECT $1::numeric", [value]) == Decimal("12.5")


@pytest.mark.db
def test_an_int_past_the_str_digit_limit_does_not_crash_the_array_encoder(schema: str) -> None:
    # str(int) is capped, so the digits are read through Decimal; golden/test_numeric.py pins the same for scalars
    limit = sys.get_int_max_str_digits()
    assert limit > 0
    for value in (10**limit + 7, -(10**limit), 10 ** (limit - 1)):
        assert fetch_scalar_params(schema, "SELECT $1::numeric[]", [[value]]) == [Decimal(value)]
    with pytest.raises(EncodeError, match="int8"):
        fetch_scalar_params(schema, "SELECT $1::int8[]", [[10**limit]])


def family_point(x: float = 1.0) -> Point:
    return Point(x, 2.0)


# one value of every element family in TYPE_TABLE that has an array type; void and nested arrays are the only
# entries left out, because neither can be a parameter value or an element
ARRAY_FAMILIES: Final[dict[str, object]] = {
    "bool": True,
    "int2": 7,
    "int4": 7,
    "int8": 2**62,
    "oid": 4_000_000_000,
    "float4": 1.5,
    "float8": 2.5,
    "numeric": Decimal("1.50E+10"),
    "text": 'a"b\\c,{}NULL ā',
    "varchar": "va",
    "bpchar": "bp",
    "name": 'a"b',
    "public.citext": "AbC",
    '"char"': "a",
    "bytea": b"\x00\xff",
    "date": dt.date(2024, 1, 2),
    "time": dt.time(1, 2, 3),
    "timetz": dt.time(1, 2, 3, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))),
    "timestamp": dt.datetime(2024, 1, 2, 3, 4, 5),  # ruff: ignore[call-datetime-without-tzinfo]
    "timestamptz": dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
    "interval": dt.timedelta(days=2, microseconds=3),
    "uuid": UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
    "inet": ip_address("10.0.0.1"),
    "cidr": ip_network("10.0.0.0/8"),
    "macaddr": "08:00:2b:01:02:03",
    "macaddr8": "08:00:2b:01:02:03:04:05",
    "int4range": Range(1, 5),
    "int8range": Range(2**40, 2**41),
    "numrange": Range(Decimal("1.5"), Decimal("2.5")),
    "daterange": Range(dt.date(2024, 1, 1), dt.date(2024, 2, 1)),
    "tsrange": Range(dt.datetime(2024, 1, 1), dt.datetime(2024, 2, 1)),  # ruff: ignore[call-datetime-without-tzinfo]
    "tstzrange": Range(dt.datetime(2024, 1, 1, tzinfo=dt.UTC), dt.datetime(2024, 2, 1, tzinfo=dt.UTC)),
    "money": Money(150),
    "bit(3)": "101",
    "varbit": "1010",
    "json": {"k": ["ā", 1]},
    "jsonb": {"k": ["ā", 1]},
    "point": family_point(),
    "line": Line(1.0, 2.0, 3.0),
    "lseg": LineSegment(family_point(), family_point(3.0)),
    "box": Box(family_point(3.0), family_point()),
    "path": Path((family_point(), family_point(3.0)), closed=False),
    "polygon": Polygon((family_point(), family_point(3.0), family_point(5.0))),
    "circle": Circle(1.0, 2.0, 3.0),
    "public.hstore": {"a": "b", "k": None},
    "public.ltree": "a.b.c",
    "public.lquery": "a.*",
    "public.cube": Cube((1.0, 2.0), (3.0, 4.0)),
    "mood": "ok",
    "positive_int": 5,
}


@pytest.mark.parametrize("sql_type", sorted(ARRAY_FAMILIES))
@pytest.mark.db
def test_every_element_family_round_trips_as_an_array_parameter(schema: str, sql_type: str) -> None:
    value = ARRAY_FAMILIES[sql_type]
    # the oracle builds the same array on the server from scalar parameters, so decoding and the server's text
    # rendering are compared without guessing either
    one = f"ARRAY[$1::{sql_type}, NULL]::{sql_type}[]"
    two = f"ARRAY[ARRAY[$1::{sql_type}, NULL], ARRAY[NULL, $1::{sql_type}]]::{sql_type}[]"
    for param_array, oracle in (([value, None], one), ([[value, None], [None, value]], two)):
        sent = fetch_scalar_params(schema, f"SELECT $1::{sql_type}[]", [param_array])
        assert sent == fetch_scalar_params(schema, f"SELECT {oracle}", [value])
        assert fetch_scalar_params(schema, f"SELECT $1::{sql_type}[]::text", [param_array]) == fetch_scalar_params(
            schema, f"SELECT {oracle}::text", [value]
        )


# no supported range subtype has a bound text that can hold a quote, backslash, comma, bracket or whitespace, or
# spell `empty`; the literal grammar is gone, so the checks that matter are the exact `range_send` bytes below
TS_LOW: Final[dt.datetime] = dt.datetime(1900, 1, 1, 0, 0, 0, 1)  # ruff: ignore[call-datetime-without-tzinfo]
TS_HIGH: Final[dt.datetime] = dt.datetime(2262, 4, 11, 23, 47, 16, 854775)  # ruff: ignore[call-datetime-without-tzinfo]
RANGE_BOUNDS: Final[dict[str, tuple[object, object]]] = {
    "int4range": (-(2**31), 2**31 - 2),
    "int8range": (-(2**63), 2**63 - 2),
    "numrange": (Decimal("-1E+5"), Decimal("1.50E+400")),
    "daterange": (dt.date(1, 1, 1), dt.date(9999, 12, 30)),
    "tsrange": (TS_LOW, TS_HIGH),
    "tstzrange": (
        dt.datetime(1999, 12, 31, 23, 59, 59, 999999, tzinfo=dt.timezone(dt.timedelta(hours=-5, minutes=-30))),
        dt.datetime(2100, 2, 28, 12, tzinfo=UTC),
    ),
}
RANGE_SHAPES: Final[list[tuple[bool, bool, bool, bool]]] = [
    # lower bounded, upper bounded, lower inclusive, upper inclusive
    (True, True, True, False),
    (True, True, True, True),
    (True, True, False, False),
    (True, True, False, True),
    (True, False, True, False),
    (False, True, False, True),
    (False, False, False, False),
]


@pytest.mark.parametrize("sql_type", sorted(RANGE_BOUNDS))
@pytest.mark.db
def test_every_range_subtype_round_trips_as_a_binary_scalar(schema: str, sql_type: str) -> None:
    low, high = RANGE_BOUNDS[sql_type]
    cases: list[tuple[Range[Any], str, list[object]]] = [(Range(empty=True), f"'empty'::{sql_type}", [])]
    for lower_bounded, upper_bounded, lower_inc, upper_inc in RANGE_SHAPES:
        lower, upper = (low if lower_bounded else None), (high if upper_bounded else None)
        bounds = ("[" if lower_inc else "(") + ("]" if upper_inc else ")")
        cases.append((
            Range(lower, upper, lower_inc=lower_inc, upper_inc=upper_inc),
            f"{sql_type}($1, $2, '{bounds}')",
            [lower, upper],
        ))
    for value, oracle, oracle_params in cases:
        sent = fetch_scalar_params(schema, f"SELECT $1::{sql_type}", [value])
        # the oracle builds the same range on the server from its bounds, so nothing is guessed
        built = fetch_scalar_params(schema, f"SELECT {oracle}", oracle_params)
        assert sent == built
        for expr in ("{}::text", "range_send({})"):
            assert fetch_scalar_params(
                schema, f"SELECT {expr.format(f'$1::{sql_type}')}", [value]
            ) == fetch_scalar_params(schema, f"SELECT {expr.format(oracle)}", oracle_params)


@pytest.mark.parametrize("sql_type", sorted(RANGE_BOUNDS))
@pytest.mark.db
def test_every_range_subtype_round_trips_as_an_array_element(schema: str, sql_type: str) -> None:
    low, high = RANGE_BOUNDS[sql_type]
    values: list[object] = [
        Range(empty=True),
        Range(low, high),
        Range(low, None, lower_inc=False),
        Range(None, high, upper_inc=True),
        Range(),
        None,
    ]
    marks = [f"${i}::{sql_type}" for i in range(1, 7)]
    oracles = {
        "flat": f"ARRAY[{', '.join(marks)}]",
        "nested": f"ARRAY[ARRAY[{', '.join(marks[:3])}], ARRAY[{', '.join(marks[3:])}]]",
    }
    for sent, oracle in ((values, oracles["flat"]), ([values[:3], values[3:]], oracles["nested"])):
        got = fetch_scalar_params(schema, f"SELECT $1::{sql_type}[]", [sent])
        text = fetch_scalar_params(schema, f"SELECT $1::{sql_type}[]::text", [sent])
        assert got == fetch_scalar_params(schema, f"SELECT {oracle}::{sql_type}[]", values)
        assert text == fetch_scalar_params(schema, f"SELECT {oracle}::{sql_type}[]::text", values)


@pytest.mark.db
def test_decimals_encode_under_a_lowercase_exponent_context(schema: str) -> None:
    # `capitals = 0` makes `Decimal.__str__` print `e` instead of `E`
    values = [Decimal("1E+5"), Decimal("-1.5E-3"), Decimal("0E-10"), Decimal("Infinity"), Decimal("1E+100")]
    with decimal.localcontext() as context:
        context.capitals = 0
        assert "e" in str(values[0])
        for value in values:
            assert fetch_scalar_params(schema, "SELECT $1::numeric", [value]) == value
        sent = fetch_scalar_params(schema, "SELECT $1::numeric[]", [[*values, None]])
        assert sent == [*values, None]
        assert fetch_scalar_params(schema, "SELECT $1::numrange", [Range(Decimal("1E+5"), Decimal("2E+5"))]) == Range(
            Decimal("1E+5"), Decimal("2E+5")
        )
        assert fetch_scalar_params(schema, "SELECT $1::numrange[]", [[Range(Decimal("1E+5"), Decimal("2E+5"))]]) == [
            Range(Decimal("1E+5"), Decimal("2E+5"))
        ]


# the overridden properties report other values, some far outside any range
def lie(value: object) -> property:
    return property(lambda _self: value, lambda _self, _value: None)


# the same for a slotted dataclass: the setter still stores the real value in the base class's slot
def lie_slot(base: type, name: str, *, value: object) -> property:
    slot = getattr(base, name)

    def store(self: object, real: object) -> None:
        slot.__set__(self, real)

    return property(lambda _self: value, store)


class LyingTimedelta(dt.timedelta):
    days = lie(0)
    seconds = lie(5)
    microseconds = lie(7)


class HugeTimedelta(dt.timedelta):
    days = lie(2**62)
    seconds = lie(2**62)
    microseconds = lie(2**62)


class LyingTime(dt.time):
    hour = lie(23)
    minute = lie(59)
    second = lie(59)
    microsecond = lie(2**62)

    @override
    def utcoffset(self) -> dt.timedelta | None:
        return dt.timedelta(hours=9)


class LyingDate(dt.date):
    year = lie(1)
    month = lie(12)
    day = lie(2**62)


class LyingDatetime(dt.datetime):
    year = lie(1)
    month = lie(12)
    day = lie(2**62)
    hour = lie(23)
    minute = lie(2**62)
    second = lie(59)
    microsecond = lie(2**62)

    @override
    def utcoffset(self) -> dt.timedelta | None:
        return dt.timedelta(hours=9)


@pytest.mark.db
def test_timedelta_subclasses_encode_their_real_value(schema: str) -> None:
    expected = dt.timedelta(hours=1, microseconds=250)
    for cls in (LyingTimedelta, HugeTimedelta):
        value = cls(hours=1, microseconds=250)
        assert fetch_scalar_params(schema, "SELECT $1::interval", [value]) == expected
        assert fetch_scalar_params(schema, "SELECT $1::interval[]", [[value, None, value]]) == [
            expected,
            None,
            expected,
        ]
        assert fetch_scalar_params(schema, "SELECT $1::interval[]::text", [[value]]) == "{01:00:00.00025}"


@pytest.mark.db
def test_time_subclasses_encode_their_real_value(schema: str) -> None:
    plain = dt.time(1, 2, 3, 4)
    value = LyingTime(1, 2, 3, 4)
    assert fetch_scalar_params(schema, "SELECT $1::time", [value]) == plain
    assert fetch_scalar_params(schema, "SELECT $1::time[]", [[value, None]]) == [plain, None]
    # the zone comes from the tzinfo, not from the subclass's own utcoffset
    zone = dt.timezone(dt.timedelta(hours=5, minutes=30))
    zoned = LyingTime(1, 2, 3, 4, tzinfo=zone)
    expected = dt.time(1, 2, 3, 4, tzinfo=zone)
    assert fetch_scalar_params(schema, "SELECT $1::timetz", [zoned]) == expected
    assert fetch_scalar_params(schema, "SELECT $1::timetz[]", [[zoned]]) == [expected]


@pytest.mark.db
def test_date_and_datetime_subclasses_encode_their_real_value(schema: str) -> None:
    day = dt.date(2024, 3, 4)
    assert fetch_scalar_params(schema, "SELECT $1::date", [LyingDate(2024, 3, 4)]) == day
    assert fetch_scalar_params(schema, "SELECT $1::date[]", [[LyingDate(2024, 3, 4), None]]) == [day, None]
    naive = dt.datetime(2024, 3, 4, 5, 6, 7, 8)  # ruff: ignore[call-datetime-without-tzinfo]
    lying = LyingDatetime(2024, 3, 4, 5, 6, 7, 8)
    assert fetch_scalar_params(schema, "SELECT $1::timestamp", [lying]) == naive
    assert fetch_scalar_params(schema, "SELECT $1::timestamp[]", [[lying, None]]) == [naive, None]
    zone = dt.timezone(dt.timedelta(hours=-3))
    aware = LyingDatetime(2024, 3, 4, 5, 6, 7, 8, tzinfo=zone)
    expected = dt.datetime(2024, 3, 4, 5, 6, 7, 8, tzinfo=zone)
    assert fetch_scalar_params(schema, "SELECT $1::timestamptz", [aware]) == expected
    assert fetch_scalar_params(schema, "SELECT $1::timestamptz[]", [[aware]]) == [expected]
    assert fetch_scalar_params(schema, "SELECT $1::tstzrange", [Range(aware, aware + dt.timedelta(days=1))]) == Range(
        expected, expected + dt.timedelta(days=1)
    )


class OffsetZone(dt.tzinfo):
    def __init__(self, offset: dt.timedelta | None) -> None:
        self.offset = offset

    @override
    def utcoffset(self, when: dt.datetime | None) -> dt.timedelta | None:
        return self.offset

    @override
    def dst(self, when: dt.datetime | None) -> dt.timedelta | None:
        return None

    @override
    def tzname(self, when: dt.datetime | None) -> str | None:
        return None


@pytest.mark.db
def test_a_tzinfo_returning_a_timedelta_subclass_is_read_exactly(schema: str) -> None:
    offset = HugeTimedelta(hours=5, minutes=30)
    zone = OffsetZone(offset)
    moment = dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=zone)
    assert fetch_scalar_params(schema, "SELECT $1::timestamptz", [moment]) == moment
    assert fetch_scalar_params(schema, "SELECT $1::timestamptz::text", [moment]) == "2024-01-01 21:34:05+00"
    assert fetch_scalar_params(schema, "SELECT $1::timestamptz[]::text", [[moment]]) == '{"2024-01-01 21:34:05+00"}'
    stamped = dt.time(1, 2, 3, tzinfo=OffsetZone(LyingTimedelta(hours=5, minutes=30)))
    assert fetch_scalar_params(schema, "SELECT $1::timetz[]::text", [[stamped]]) == "{01:02:03+05:30}"
    with pytest.raises(EncodeError, match="naive"):
        fetch_scalar_params(schema, "SELECT $1::timestamptz", [dt.datetime(2024, 1, 2, tzinfo=OffsetZone(None))])


class LyingInterval(Interval):
    months = lie_slot(Interval, "months", value=99)  # ty: ignore[invalid-assignment]
    days = lie_slot(Interval, "days", value=2**62)  # ty: ignore[invalid-assignment]
    microseconds = lie_slot(Interval, "microseconds", value=2**62)  # ty: ignore[invalid-assignment]


class LyingMoney(Money):
    minor_units = lie_slot(Money, "minor_units", value=2**62)  # ty: ignore[invalid-assignment]


class LyingPoint(Point):
    x = lie_slot(Point, "x", value=100.0)  # ty: ignore[invalid-assignment]
    y = lie_slot(Point, "y", value=float("nan"))  # ty: ignore[invalid-assignment]


class LyingCube(Cube):
    lower_left = lie_slot(Cube, "lower_left", value=(9.0,))  # ty: ignore[invalid-assignment]
    upper_right = lie_slot(Cube, "upper_right", value=(9.0,))  # ty: ignore[invalid-assignment]


class LyingRange(Range[int]):
    lower = lie_slot(Range, "lower", value=99)  # ty: ignore[invalid-assignment]
    upper = lie_slot(Range, "upper", value=100)  # ty: ignore[invalid-assignment]
    lower_inc = lie_slot(Range, "lower_inc", value=False)  # ty: ignore[invalid-assignment]
    upper_inc = lie_slot(Range, "upper_inc", value=True)  # ty: ignore[invalid-assignment]


class LyingPath(Path):
    points = lie_slot(Path, "points", value=())  # ty: ignore[invalid-assignment]


class IteratingTuple(tuple[float, ...]):
    __slots__ = ()

    @override
    def __iter__(self) -> Any:
        return iter((9.0,))


@pytest.mark.db
def test_rowx_value_subclasses_encode_their_real_value(schema: str) -> None:
    assert fetch_scalar_params(schema, "SELECT $1::interval::text", [Interval(months=1, days=2, microseconds=3)]) == (
        fetch_scalar_params(schema, "SELECT $1::interval::text", [LyingInterval(months=1, days=2, microseconds=3)])
    )
    assert fetch_scalar_params(
        schema, "SELECT $1::interval[]::text", [[LyingInterval(months=1, days=2, microseconds=3)]]
    ) == ('{"1 mon 2 days 00:00:00.000003"}')
    assert fetch_scalar_params(schema, "SELECT $1::money[]::text", [[LyingMoney(150)]]) == fetch_scalar_params(
        schema, "SELECT $1::money[]::text", [[Money(150)]]
    )
    assert fetch_scalar_params(schema, "SELECT $1::point[]", [[LyingPoint(1.0, 2.0)]]) == [Point(1.0, 2.0)]
    assert fetch_scalar_params(schema, "SELECT $1::point", [LyingPoint(1.0, 2.0)]) == Point(1.0, 2.0)
    assert fetch_scalar_params(schema, "SELECT $1::path", [LyingPath((Point(1.0, 2.0),), closed=True)]) == Path(
        (Point(1.0, 2.0),), closed=True
    )
    assert fetch_scalar_params(schema, "SELECT $1::public.cube[]", [[LyingCube((1.0, 2.0), (3.0, 4.0))]]) == [
        Cube((1.0, 2.0), (3.0, 4.0))
    ]
    assert fetch_scalar_params(
        schema, "SELECT $1::public.cube", [Cube(IteratingTuple((1.0, 2.0)), (3.0, 4.0))]
    ) == Cube((1.0, 2.0), (3.0, 4.0))
    assert fetch_scalar_params(schema, "SELECT $1::int4range", [LyingRange(1, 5)]) == Range(1, 5)
    assert fetch_scalar_params(schema, "SELECT $1::int4range[]", [[LyingRange(1, 5)]]) == [Range(1, 5)]


@pytest.mark.db
def test_a_uuid_subclass_cannot_choose_the_bytes_that_are_sent(schema: str) -> None:
    class LyingUuid(UUID):
        bytes = lie(b"\xff" * 16)

    real = UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
    value = LyingUuid(str(real))
    assert fetch_scalar_params(schema, "SELECT $1::uuid", [value]) == real
    assert fetch_scalar_params(schema, "SELECT $1::uuid[]", [[value]]) == [real]


@pytest.mark.db
def test_an_ip_address_subclass_cannot_choose_the_bytes_that_are_sent(schema: str) -> None:
    class LyingAddress(ipaddress.IPv4Address): ...

    value = LyingAddress("1.2.3.4")
    LyingAddress.packed = lie(b"\x09\x09\x09\x09")
    assert fetch_scalar_params(schema, "SELECT $1::inet::text", [value]) == "1.2.3.4/32"
    assert fetch_scalar_params(schema, "SELECT $1::inet[]::text", [[value]]) == "{1.2.3.4}"


@pytest.mark.db
def test_an_ip_interface_subclass_cannot_choose_the_prefix_that_is_sent(schema: str) -> None:
    class LyingInterface(ipaddress.IPv4Interface): ...

    # the lie goes on after construction, because `__init__` stores the real network through the same name
    value = LyingInterface("1.2.3.4/24")
    LyingInterface.network = lie(ipaddress.IPv4Network("10.0.0.0/8"))  # ty: ignore[invalid-assignment]
    assert fetch_scalar_params(schema, "SELECT $1::inet::text", [value]) == "1.2.3.4/24"
    assert fetch_scalar_params(schema, "SELECT $1::inet[]::text", [[value]]) == "{1.2.3.4/24}"


@pytest.mark.db
def test_an_ip_network_subclass_cannot_choose_the_address_or_prefix_that_is_sent(schema: str) -> None:
    class LyingNetwork(ipaddress.IPv4Network): ...

    value = LyingNetwork("1.2.3.0/24")
    LyingNetwork.network_address = lie(ipaddress.IPv4Address("9.9.9.9"))  # ty: ignore[invalid-assignment]
    LyingNetwork.prefixlen = lie(8)
    assert fetch_scalar_params(schema, "SELECT $1::cidr::text", [value]) == "1.2.3.0/24"
    assert fetch_scalar_params(schema, "SELECT $1::cidr[]::text", [[value]]) == "{1.2.3.0/24}"


@pytest.mark.db
def test_an_ip_subclass_cannot_hide_a_zone_id(schema: str) -> None:
    class HidingAddress(ipaddress.IPv6Address):
        scope_id = lie(None)

    value = HidingAddress("fe80::1%eth0")
    with pytest.raises(EncodeError, match="zone id"):
        fetch_scalar_params(schema, "SELECT $1::inet::text", [value])
    with pytest.raises(EncodeError, match="zone id"):
        fetch_scalar_params(schema, "SELECT $1::inet[]::text", [[value]])


@pytest.mark.db
def test_an_object_claiming_an_ip_class_is_an_encode_error(schema: str) -> None:
    class Liar:
        @property
        @override
        def __class__(self) -> type:
            return ipaddress.IPv4Address

        packed = b"\x09\x09\x09\x09"
        scope_id = None

    with pytest.raises(EncodeError, match=r"\$1"):
        fetch_scalar_params(schema, "SELECT $1::inet::text", [Liar()])
    with pytest.raises(EncodeError, match=r"\$1"):
        fetch_scalar_params(schema, "SELECT $1::inet[]::text", [[Liar()]])


def evil_buffer(_self: object, _flags: int) -> memoryview:
    return memoryview(b"EVIL")


@pytest.mark.db
def test_bytes_subclasses_cannot_choose_the_content_that_is_sent(schema: str) -> None:
    named_bytes = type("bytes", (bytes,), {"__buffer__": evil_buffer})
    named_bytearray = type("bytearray", (bytearray,), {"__buffer__": evil_buffer})
    for value in (named_bytes(b"good"), named_bytearray(b"good")):
        assert fetch_scalar_params(schema, "SELECT $1::bytea", [value]) == b"good"
        assert fetch_scalar_params(schema, "SELECT $1::bytea[]", [[value]]) == [b"good"]


@pytest.mark.db
def test_a_class_named_bytes_that_is_not_bytes_is_an_encode_error(schema: str) -> None:
    fake = type("bytes", (), {"__buffer__": lambda _self, _flags: memoryview(b"EVIL")})
    with pytest.raises(EncodeError):
        fetch_scalar_params(schema, "SELECT $1::bytea", [fake()])


class ShiftyList(list[int]):  # ruff: ignore[subclass-builtin]
    @override
    def __iter__(self) -> Iterator[int]:
        return iter([7, 7])

    @override
    def __len__(self) -> int:
        return 5

    @override
    def __getitem__(self, _index: object) -> int:  # ty: ignore[incompatible-method-override]
        return 8


class ShiftyTuple(tuple[int, ...]):
    __slots__ = ()

    @override
    def __iter__(self) -> Iterator[int]:
        return iter([7, 7])


def shifty_iter(_self: object) -> Iterator[int]:
    return iter([7, 7])


@pytest.mark.db
def test_list_and_tuple_subclasses_send_their_real_items(schema: str) -> None:
    named_list = type("list", (list,), {"__iter__": shifty_iter})
    named_tuple = type("tuple", (tuple,), {"__iter__": shifty_iter})
    assert fetch_scalar_params(schema, "SELECT $1::int4[]", [ShiftyList([1, 2])]) == [1, 2]
    assert fetch_scalar_params(schema, "SELECT $1::int4[]", [ShiftyTuple((1, 2))]) == [1, 2]
    assert fetch_scalar_params(schema, "SELECT $1::int4[]", [named_list([1, 2])]) == [1, 2]
    assert fetch_scalar_params(schema, "SELECT $1::int4[]", [named_tuple((1, 2))]) == [1, 2]
    assert fetch_scalar_params(schema, "SELECT $1::int4[]", [[ShiftyList([1, 2])]]) == [[1, 2]]
    nested = [ShiftyTuple((3, 4)), named_tuple((5, 6))]
    assert fetch_scalar_params(schema, "SELECT $1::int4[]", [nested]) == [[3, 4], [5, 6]]


@pytest.mark.db
def test_a_class_named_list_that_is_not_a_list_is_an_encode_error(schema: str) -> None:
    fake = type("list", (), {"__iter__": lambda _self: iter([1, 2])})
    with pytest.raises(EncodeError):
        fetch_scalar_params(schema, "SELECT $1::int4[]", [fake()])


@pytest.mark.db
def test_a_descriptor_failure_on_a_class_liar_is_an_encode_error(schema: str) -> None:
    class Liar:
        @property
        @override
        def __class__(self) -> type:
            return dt.date

        year = 2024

    with pytest.raises(EncodeError, match=r"\$1"):
        fetch_scalar_params(schema, "SELECT $1::date", [Liar()])


@pytest.mark.db
def test_a_range_with_a_non_bool_flag_is_an_encode_error(schema: str) -> None:
    value = Range(1, 5)
    Range.lower_inc.__set__(value, "yes")
    with pytest.raises(EncodeError, match=r"\$1"):
        fetch_scalar_params(schema, "SELECT $1::int4range", [value])


# from _core/differential/test__core.py

TYPES_DOC: Final[FilePath] = (
    next(parent for parent in FilePath(__file__).resolve().parents if (parent / "Cargo.lock").is_file())
    / "docs"
    / "types.md"
)
DOC_HEADING: Final[str] = "## Differences from psycopg"

# how a case may differ from psycopg: equal as instants, equal after rounding to float32, rowx raising DecodeError,
# rowx returning None for void, rowx returning an interface where psycopg returns the bare host address, or rowx's
# own `Range` class holding the bounds psycopg's `Range` holds, or rowx returning the minor units of a money value
# where psycopg returns its formatted text, or rowx returning a geometric dataclass where psycopg returns the server's
# text, or rowx returning a `Cube` where psycopg returns the server's text
type Difference = Literal[
    "instant", "float32", "decode-error", "void", "host-address", "range", "money", "geometric", "cube"
]

RANGE_CLASS: Final[str] = (
    "rowx returns `rowx_postgres.Range`, while psycopg returns its own `Range`; the bounds are equal"
)
RANGE_BOUNDS_NOTE: Final[str] = (
    "an infinite bound of this subtype raises `DecodeError`, while psycopg raises its own `DataError`"
)
RANGE_DIFFERENCES: Final[dict[str, str]] = {
    "int4range": f"{RANGE_CLASS}.",
    "int8range": f"{RANGE_CLASS}.",
    "numrange": f"{RANGE_CLASS}.",
    "daterange": f"{RANGE_CLASS}, but {RANGE_BOUNDS_NOTE}.",
    "tsrange": f"{RANGE_CLASS}, but {RANGE_BOUNDS_NOTE}.",
    "tstzrange": f"{RANGE_CLASS} as instants, but {RANGE_BOUNDS_NOTE}.",
}


GEOMETRIC_CLASSES: Final[dict[str, str]] = {
    "point": "Point",
    "line": "Line",
    "lseg": "LineSegment",
    "box": "Box",
    "path": "Path",
    "polygon": "Polygon",
    "circle": "Circle",
}
GEOMETRIC_DIFFERENCES: Final[dict[str, str]] = {
    key: (
        f"rowx returns `rowx_postgres.{name}`, while psycopg returns the text the server formats; the coordinates are "
        "equal."
    )
    for key, name in GEOMETRIC_CLASSES.items()
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
        "An interval with a month or year part raises `DecodeError` unless the annotation is `rowx_postgres.Interval`, "
        "while psycopg approximates a month as 30 days."
    ),
    "date": "An infinite date raises `DecodeError`, while psycopg raises its own `DataError`.",
    "timestamp": "An infinite timestamp raises `DecodeError`, while psycopg raises its own `DataError`.",
    "void": "`void` decodes to `None`, while psycopg returns an empty string.",
    "inet": (
        "rowx always returns an interface, even for a full-width mask, while psycopg returns an address for `/32` and "
        "`/128`; the interface's `ip` is that address."
    ),
    **RANGE_DIFFERENCES,
    "money": (
        "rowx returns `rowx_postgres.Money`, the int8 minor units with no scale, while psycopg returns the text the "
        "server formats under `lc_monetary`."
    ),
    **GEOMETRIC_DIFFERENCES,
    "cube": (
        "rowx returns `rowx_postgres.Cube`, while psycopg returns the text the server formats; the coordinates are "
        "equal."
    ),
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
    *_cases(
        "money",
        "money",
        "0",
        "1.00",
        "-1.00",
        "0.01",
        "92233720368547758.07",
        "-92233720368547758.08",
        difference="money",
    ),
    *_cases("array", "money[]", "{1.00,NULL,-2.50}", difference="money"),
    *_cases(
        "point",
        "point",
        "(0,0)",
        "(1.5,-2)",
        "(1e300,-1e-300)",
        "(NaN,Infinity)",
        "(-Infinity,-0)",
        difference="geometric",
    ),
    *_cases("line", "line", "{1,-2,3}", "{0,1,0}", "{1e10,1e-10,-0.5}", "{NaN,1,Infinity}", difference="geometric"),
    *_cases("lseg", "lseg", "[(0,0),(1,2)]", "[(1,1),(1,1)]", "[(-1.5,2),(NaN,Infinity)]", difference="geometric"),
    *_cases("box", "box", "(0,0),(2,3)", "(2,3),(0,0)", "(1,1),(1,1)", "(0,3),(2,0)", difference="geometric"),
    *_cases("path", "path", "[(0,0)]", "((0,0))", "[(0,0),(1,1),(2,0)]", "((0,0),(1,1),(2,0))", difference="geometric"),
    *_cases(
        "polygon", "polygon", "((0,0))", "((0,0),(1,0),(1,1))", "((0,0),(1e300,0),(NaN,1))", difference="geometric"
    ),
    *_cases("circle", "circle", "<(0,0),0>", "<(1,2),3>", "<(-1.5,1e300),Infinity>", difference="geometric"),
    # the extensions live in public, which the test search_path leaves out
    *_cases(
        "hstore",
        "public.hstore",
        "",
        "a=>1",
        "a=>NULL",
        '"a b"=>"c, d"',
        '"k"=>"", "n"=>"NULL"',
        "é=>ü",
        '"a=>b"=>"c=>d"',
    ),
    *_cases("array", "public.hstore[]", '{"a=>1",NULL,""}'),
    *_cases("ltree", "public.ltree", "", "a", "a.b.c", "Top.Science.Astronomy", "A_1.b2"),
    *_cases("lquery", "public.lquery", "*", "a.*{1,2}.b", "a|b.c@", "!a.*", "*.a.*"),
    *_cases(
        "cube",
        "public.cube",
        "()",
        "(1)",
        "(1),(2)",
        "(1,2)",
        "(1,2),(3,4)",
        "(-1.5,0),(2,1e10)",
        "(3,4),(1,2)",
        difference="cube",
    ),
    *_cases("bit", "bit(1)", "0", "1"),
    *_cases("bit", "bit(8)", "00000000", "10101010", "11111111"),
    *_cases("bit", "bit(9)", "101010101", "000000001"),
    *_cases("bit", "bit(70)", "1" * 70, "10" * 35),
    *_cases("varbit", "varbit", "", "0", "1", "1010101", "10101010", "101010101", "1" * 200),
    *_cases("array", "bit(3)[]", "{101,NULL,000}"),
    *_cases("array", "varbit[]", '{"",1,NULL,10101010101}'),
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
            rows = await conn._impl._fetch(sql, [], None, scalar=True, limit_one=True, arity=1, shape=None)
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
        # without this psycopg returns an hstore as text
        if (info := PsycopgTypeInfo.fetch(conn, "public.hstore")) is not None:
            register_hstore(info, conn)
        try:
            row = conn.execute(cast("LiteralString", sql)).fetchone()
        except psycopg.DataError as exc:
            return "error", exc
    assert row is not None
    return "ok", row[0]


def same_nested(left: object, right: object) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(same_nested, left, right, strict=True))
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


# the server's own numeric reading of the same literal, at the scale lc_monetary gives it (two places here)
def _money_matches(dsn: str, case: Case, value: object) -> bool:
    if isinstance(value, list):
        scaled = f"SELECT array(SELECT (e::numeric * 100)::int8 FROM unnest('{case.literal}'::money[]) e)"  # ruff: ignore[hardcoded-sql-expression]
        _, expected = _psycopg_outcome(dsn, scaled)
        return [item.minor_units if isinstance(item, Money) else None for item in value] == expected
    _, expected = _psycopg_outcome(dsn, f"SELECT ('{case.literal}'::money::numeric * 100)::int8")
    return value == Money(expected)


GEOMETRIC_NUMBER: Final[str] = r"[-+]?(?:Infinity|NaN|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)"


def _geometric_numbers(value: object) -> list[float]:
    # the coordinates in field order, which is also the order the server prints them in
    if isinstance(value, bool):
        return []
    if isinstance(value, float | int):
        return [float(value)]
    if isinstance(value, tuple):
        return [number for item in value for number in _geometric_numbers(item)]
    assert is_dataclass(value), value
    return [number for field in fields(value) for number in _geometric_numbers(getattr(value, field.name))]


def _geometric_matches(rowx_value: object, psycopg_text: object) -> bool:
    assert isinstance(psycopg_text, str)
    expected = [float(number) for number in re.findall(GEOMETRIC_NUMBER, psycopg_text)]
    got = _geometric_numbers(rowx_value)
    if len(got) != len(expected) or not all(
        (isnan(a) and isnan(b)) or (a == b and str(a) == str(b)) for a, b in zip(got, expected, strict=True)
    ):
        return False
    # a path is closed when the server prints it with parentheses
    if isinstance(rowx_value, postgres.Path):
        return rowx_value.closed == psycopg_text.startswith("(")
    return True


def _cube_matches(rowx_value: postgres.Cube, psycopg_text: object) -> bool:
    assert isinstance(psycopg_text, str)
    numbers = [float(number) for number in re.findall(GEOMETRIC_NUMBER, psycopg_text)]
    dim = len(rowx_value.lower_left)
    if len(rowx_value.upper_right) != dim:
        return False
    # the server prints a point with one corner, and the codec reads it back as two equal ones
    if len(numbers) == dim:
        return list(rowx_value.lower_left) == numbers == list(rowx_value.upper_right)
    return len(numbers) == 2 * dim and [*rowx_value.lower_left, *rowx_value.upper_right] == numbers


def _structured_matches(rowx_value: object, psycopg_value: object) -> bool:
    if isinstance(rowx_value, postgres.Cube):
        return _cube_matches(rowx_value, psycopg_value)
    classes = tuple(getattr(postgres, name) for name in GEOMETRIC_CLASSES.values())
    return isinstance(rowx_value, classes) and _geometric_matches(rowx_value, psycopg_value)


@pytest.mark.db
def test_corpus_covers_the_type_table() -> None:
    supported = {t.key for t in type_table() if t.decode != "unsupported"}
    assert {case.key for case in CORPUS} == supported


@pytest.mark.parametrize("case", CORPUS, ids=_case_id)
@pytest.mark.db
def test_decode_matches_psycopg(schema: str, case: Case) -> None:
    sql = _literal_sql(case)
    status, rowx_value = _rowx_outcome(schema, sql)
    match case.difference:
        case None:
            _, psycopg_value = _psycopg_outcome(schema, sql)
            assert status == "ok", rowx_value
            assert same_nested(rowx_value, psycopg_value), (rowx_value, psycopg_value)
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
        case "money":
            assert status == "ok", rowx_value
            assert _money_matches(schema, case, rowx_value)
        case "geometric" | "cube":
            _, psycopg_value = _psycopg_outcome(schema, sql)
            assert status == "ok", rowx_value
            assert _structured_matches(rowx_value, psycopg_value), (rowx_value, psycopg_value)
        case "void":
            assert (status, rowx_value) == ("ok", None)
            assert _psycopg_outcome(schema, sql) == ("ok", "")


@pytest.mark.db
def test_psycopg_approximates_a_month_as_thirty_days(schema: str) -> None:
    assert _psycopg_outcome(schema, "SELECT '1 mon'::interval") == ("ok", dt.timedelta(days=30))


@pytest.mark.db
def test_psycopg_rejects_infinite_dates_and_timestamps(schema: str) -> None:
    for sql_type in ("date", "timestamp", "timestamptz"):
        status, _ = _psycopg_outcome(schema, f"SELECT 'infinity'::{sql_type}")
        assert status == "error", sql_type


@pytest.mark.db
def test_difference_keys_name_corpus_cases() -> None:
    # an array of timestamptz is covered by the timestamptz entry
    differing = {case.key for case in CORPUS if case.difference is not None and case.key != "array"}
    assert differing == DIFFERENCE_KEYS


@pytest.mark.db
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


# from _core/named/test__core.py

NULLABLE: Final[int] = 1
SPAN: Final[int] = 2

type Field = tuple[str, tuple[str, ...] | None, bool, int]


def spec(
    key: str, *, names: tuple[str, ...] | None = None, unknown: bool = False, required: bool = True, flags: int = 0
) -> Field:
    return key, None if unknown else (names or (key,)), required, flags


def shape(  # ruff: ignore[too-many-arguments]
    ctor: object,
    form: str,
    fields: list[Field],
    *,
    matching: str = "by_name",
    validate: bool = True,
    name: str = "Row",
) -> RowShape:
    return RowShape(ctor, form, matching, name, fields, validate=validate)  # ty: ignore[invalid-argument-type]


def kwargs_of(**kw: object) -> dict[str, object]:
    return kw


def args_of(*args: object) -> tuple[object, ...]:
    return args


@dataclass(slots=True)
class Validator:
    seen: list[dict[str, object]] = field(default_factory=list)

    def validate_python(self, row: dict[str, object]) -> dict[str, object]:
        self.seen.append(row)
        return {"validated": row}


class Plain:
    def __init__(self, **kw: object) -> None:
        self.kw = kw


class Stop(BaseException):
    pass


def raising_on(bad: set[int], exc: type[BaseException] = ValueError) -> Callable[..., object]:
    def ctor(**kw: int) -> dict[str, int]:
        if kw["n"] in bad:
            msg = "boom"
            raise exc(msg)
        return kw

    return ctor


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


async def fetch_with_shape(conn: Connection, sql: str, row_shape: RowShape, *, limit_one: bool = False) -> list[object]:
    return await conn._impl._fetch(sql, [], None, False, limit_one, None, row_shape)  # ruff: ignore[boolean-positional-value-in-call]


def stream(conn: ConnectionImpl, sql: str, row_shape: RowShape, batch_size: int = 1024) -> object:
    return conn._stream(sql, [], None, False, None, batch_size, row_shape)  # ruff: ignore[boolean-positional-value-in-call]


async def drain(handle: object) -> tuple[list[object], BaseException | None]:
    rows: list[object] = []
    try:
        while batch := await handle._next():  # ty: ignore[missing-attribute]
            rows.extend(batch)
    except BaseException as err:  # ruff: ignore[blind-except]
        return rows, err
    return rows, None


@pytest.mark.db
def test_keyword_form_matches_by_name_when_the_column_order_differs(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("a"), spec("b")])

    async def body(conn: Connection) -> list[object]:
        return await fetch_with_shape(conn, "SELECT 2::int4 AS b, 'x'::text AS a", row_shape)

    assert run_with_conn(schema, body) == [{"a": "x", "b": 2}]


@pytest.mark.db
def test_keyword_form_leaves_out_an_absent_optional_field(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("a"), spec("b", required=False)])

    async def body(conn: Connection) -> list[object]:
        return await fetch_with_shape(conn, "SELECT 'x'::text AS a", row_shape)

    assert run_with_conn(schema, body) == [{"a": "x"}]


@pytest.mark.db
def test_positional_form_orders_the_arguments_by_field(schema: str) -> None:
    row_shape = shape(
        args_of,
        "positional",
        [spec("id"), spec("name"), spec("note", required=False)],
        matching="positional",
    )

    async def body(conn: Connection) -> tuple[list[object], list[object]]:
        two = await fetch_with_shape(conn, "SELECT 1::int4 AS id, 'n'::text AS name", row_shape)
        three = await fetch_with_shape(conn, "SELECT 1::int4 AS id, 'n'::text AS name, 'z'::text AS note", row_shape)
        return two, three

    assert run_with_conn(schema, body) == ([(1, "n")], [(1, "n", "z")])


@pytest.mark.db
def test_dict_form_is_the_dict_itself_without_absent_keys(schema: str) -> None:
    row_shape = shape(None, "dict", [spec("a"), spec("b", required=False)])

    async def body(conn: Connection) -> tuple[list[object], list[object]]:
        both = await fetch_with_shape(conn, "SELECT 2::int4 AS b, 1::int4 AS a", row_shape)
        one = await fetch_with_shape(conn, "SELECT 1::int4 AS a", row_shape)
        return both, one

    assert run_with_conn(schema, body) == ([{"a": 1, "b": 2}], [{"a": 1}])


@pytest.mark.db
def test_validator_form_keys_by_column_name_with_pass_through_and_last_duplicate(schema: str) -> None:
    validator = Validator()
    # a field with unknown names turns the by-name rules off, so every column reaches the dict
    row_shape = shape(
        validator, "validator", [spec("id", names=("user_id",)), spec("any", unknown=True, required=False)]
    )

    async def body(conn: Connection) -> list[object]:
        return await fetch_with_shape(
            conn, "SELECT 5::int4 AS user_id, 1::int4 AS extra, 2::int4 AS extra, 'k'::text AS \"u!\"", row_shape
        )

    assert run_with_conn(schema, body) == [{"validated": {"user_id": 5, "extra": 2, "u": "k"}}]
    assert validator.seen == [{"user_id": 5, "extra": 2, "u": "k"}]


@pytest.mark.db
def test_validator_form_can_call_the_class_with_the_dict_as_keywords(schema: str) -> None:
    row_shape = shape(Plain, "validator", [spec("a"), spec("b")], validate=False)

    async def body(conn: Connection) -> list[object]:
        return await fetch_with_shape(conn, "SELECT 2::int4 AS b, 1::int4 AS a", row_shape)

    (row,) = run_with_conn(schema, body)
    assert isinstance(row, Plain)
    assert row.kw == {"a": 1, "b": 2}


@pytest.mark.db
def test_interval_flag_reads_a_lossless_interval_for_the_mapped_field(schema: str) -> None:
    sql = "SELECT interval '1 month 2 days' AS i, interval '3 hours' AS t"
    plain = shape(kwargs_of, "keyword", [spec("t"), spec("i", flags=SPAN)])

    async def body(conn: Connection) -> list[object]:
        return await fetch_with_shape(conn, sql, plain)

    (found,) = run_with_conn(schema, body)
    row = cast("dict[str, object]", found)
    assert isinstance(row["i"], Interval)
    # the other interval field has no flag, so it decodes as a timedelta
    assert not isinstance(row["t"], Interval)

    async def without(conn: Connection) -> list[object]:
        return await fetch_with_shape(conn, sql, shape(kwargs_of, "keyword", [spec("t"), spec("i")]))

    with pytest.raises(DecodeError):
        run_with_conn(schema, without)


@pytest.mark.db
def test_null_in_a_non_nullable_field_names_the_field_and_column(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("b"), spec("a", flags=NULLABLE)])

    async def body(conn: Connection) -> list[object]:
        return await fetch_with_shape(conn, "SELECT 1::int4 AS a, NULL::int4 AS b", row_shape)

    with pytest.raises(RowShapeError) as caught:
        run_with_conn(schema, body)
    assert str(caught.value) == 'Row: NULL in column 2 "b" for non-nullable field "b" of `Row` (row 0)'


@pytest.mark.db
def test_null_in_a_stream_names_its_row_after_the_rows_before_it(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("n")], name="User")
    sql = "SELECT CASE WHEN g = 4 THEN NULL ELSE g END::int4 AS n FROM generate_series(1, 5) g"

    async def body(conn: Connection) -> tuple[list[object], BaseException | None]:
        return await drain(stream(conn._impl, sql, row_shape, 2))

    rows, err = run_with_conn(schema, body)
    assert rows == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert isinstance(err, RowShapeError)
    assert str(err) == 'User: NULL in column 1 "n" for non-nullable field "n" of `User` (row 3)'


@pytest.mark.db
def test_mapping_error_on_an_empty_result_is_exact(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("a"), spec("b")], name="User")

    async def body(conn: Connection) -> list[object]:
        return await fetch_with_shape(conn, "SELECT 1::int4 AS a, 2::int4 AS c WHERE false", row_shape)

    with pytest.raises(RowShapeError) as caught:
        run_with_conn(schema, body)
    assert str(caught.value) == 'User: column 2 "c" matches no field of `User`; field "b" of `User` has no column'


@pytest.mark.parametrize(
    ("sql", "fields", "kwargs", "message"),
    [
        (
            "SELECT 1 AS id, 2 AS id",
            [spec("id")],
            {},
            'M: columns 1 and 2 are both named "id"',
        ),
        (
            "SELECT 1, 2",
            [spec("id")],
            {},
            'M: columns 1 and 2 are both unnamed (`?column?`); field "id" of `M` has no column',
        ),
        (
            "SELECT 1 AS id, 2 AS user_id",
            [spec("id", names=("id", "user_id"))],
            {},
            'M: columns 1 "id" and 2 "user_id" both map to field "id" of `M`',
        ),
        (
            "SELECT 1 AS y",
            [spec("x", names=("y",)), spec("y")],
            {},
            'M: column 1 "y" matches fields "x" and "y" of `M`',
        ),
        (
            "SELECT 1 AS login, 2 AS id",
            [spec("id"), spec("email")],
            {"matching": "positional"},
            'M: column 1 "login" faces field "id" of `M` (position 1); column 2 "id" faces field "email" of `M` (position 2)',
        ),
        (
            "SELECT 1 AS a, 2 AS b, 3 AS c, 4 AS d",
            [spec("a"), spec("b", required=False)],
            {"matching": "positional"},
            "M: the query returns 4 columns but `M` takes between 1 and 2",
        ),
    ],
)
@pytest.mark.db
def test_mapping_messages(schema: str, sql: str, fields: list[Field], kwargs: dict[str, object], message: str) -> None:
    matching = str(kwargs.get("matching", "by_name"))
    row_shape = shape(
        kwargs_of, "keyword" if matching == "by_name" else "positional", fields, matching=matching, name="M"
    )

    async def body(conn: Connection) -> list[object]:
        return await fetch_with_shape(conn, sql, row_shape)

    with pytest.raises(RowShapeError) as caught:
        run_with_conn(schema, body)
    assert str(caught.value) == message


@pytest.mark.db
def test_constructor_error_in_all_and_one_is_wrapped_with_its_cause(schema: str) -> None:
    row_shape = shape(raising_on({2}), "keyword", [spec("n")], name="Num")
    sql = "SELECT g::int4 AS n FROM generate_series(1, 4) g"

    async def body(conn: Connection) -> tuple[RowConstructError, list[object]]:
        with pytest.raises(RowConstructError) as caught:
            await fetch_with_shape(conn, sql, row_shape)
        # `one` and `optional` stop at the first row, so a failure there is row 0
        first = shape(raising_on({1}), "keyword", [spec("n")], name="Num")
        with pytest.raises(RowConstructError) as one:
            await fetch_with_shape(conn, sql, first, limit_one=True)
        assert one.value.row_index == 0
        return caught.value, await fetch_with_shape(
            conn, "SELECT 1::int4 AS n", shape(kwargs_of, "keyword", [spec("n")])
        )

    err, after = run_with_conn(schema, body)
    assert isinstance(err, DecodeError)
    assert (err.type_name, err.row_index) == ("Num", 1)
    assert str(err) == "Num row 1: ValueError('boom')"
    assert isinstance(err.__cause__, ValueError)
    assert after == [{"n": 1}]


@pytest.mark.db
def test_constructor_error_text_from_user_code_passes_through_verbatim(schema: str) -> None:
    text = "postgresql://u:hunter2@h/db password=hunter2"

    def ctor(**_: int) -> dict[str, int]:
        raise ValueError(text)

    row_shape = shape(ctor, "keyword", [spec("n")], name="Num")

    async def body(conn: Connection) -> RowConstructError:
        with pytest.raises(RowConstructError) as caught:
            await fetch_with_shape(conn, "SELECT 1::int4 AS n", row_shape)
        return caught.value

    err = run_with_conn(schema, body)
    assert str(err) == f"Num row 0: ValueError({text!r})"
    assert isinstance(err.__cause__, ValueError)
    assert err.__cause__.args == (text,)


@pytest.mark.parametrize(("bad", "yielded"), [(2, 2), (3, 3), (0, 0), (1, 1)])
@pytest.mark.db
def test_constructor_error_in_a_stream_yields_earlier_rows_across_batches(schema: str, bad: int, yielded: int) -> None:
    row_shape = shape(raising_on({bad + 1}), "keyword", [spec("n")], name="Num")
    sql = "SELECT g::int4 AS n FROM generate_series(1, 5) g"

    async def body(conn: Connection) -> tuple[list[object], BaseException | None, list[object]]:
        rows, err = await drain(stream(conn._impl, sql, row_shape, 2))
        return rows, err, await fetch_with_shape(conn, "SELECT 7::int4 AS n", shape(kwargs_of, "keyword", [spec("n")]))

    rows, err, after = run_with_conn(schema, body)
    assert rows == [{"n": n} for n in range(1, yielded + 1)]
    assert isinstance(err, RowConstructError)
    assert err.row_index == bad
    assert isinstance(err.__cause__, ValueError)
    assert after == [{"n": 7}]


@pytest.mark.db
def test_base_exception_from_the_constructor_passes_through(schema: str) -> None:
    row_shape = shape(raising_on({1}, Stop), "keyword", [spec("n")])

    async def body(conn: Connection) -> BaseException | None:
        with pytest.raises(Stop):
            await fetch_with_shape(conn, "SELECT 1::int4 AS n", row_shape)
        _, err = await drain(stream(conn._impl, "SELECT 1::int4 AS n", row_shape))
        return err

    assert isinstance(run_with_conn(schema, body), Stop)


@pytest.mark.db
def test_pool_fetch_and_stream_build_named_rows(schema: str) -> None:
    good = shape(kwargs_of, "keyword", [spec("n")])
    bad = shape(raising_on({3}), "keyword", [spec("n")], name="Num")
    sql = "SELECT g::int4 AS n FROM generate_series(1, 4) g"

    async def run() -> tuple[list[object], list[object], BaseException | None, list[object]]:
        async with Pool(schema, max_size=1) as pool:
            rows = await pool._fetch(sql, [], None, False, False, None, good)  # ruff: ignore[boolean-positional-value-in-call]
            streamed, _ = await drain(pool._stream(sql, [], None, False, None, 2, good))  # ruff: ignore[boolean-positional-value-in-call]
            failed, err = await drain(pool._stream(sql, [], None, False, None, 2, bad))  # ruff: ignore[boolean-positional-value-in-call]
            with pytest.raises(RowConstructError):
                await pool._fetch(sql, [], None, False, False, None, bad)  # ruff: ignore[boolean-positional-value-in-call]
            return rows, streamed, err, failed

    rows, streamed, err, failed = asyncio.run(run())
    assert rows == streamed == [{"n": n} for n in range(1, 5)]
    assert failed == [{"n": 1}, {"n": 2}]
    assert isinstance(err, RowConstructError)
    assert err.row_index == 2


@pytest.mark.db
def test_the_mapping_is_rechecked_when_a_table_gains_a_column(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("a")])

    async def body(conn: Connection) -> None:
        await conn._impl._execute("CREATE TABLE named_t (a int4 NOT NULL)", [])
        await conn._impl._execute("INSERT INTO named_t VALUES (1)", [])
        assert await fetch_with_shape(conn, "SELECT * FROM named_t", row_shape) == [{"a": 1}]
        await conn._impl._execute("ALTER TABLE named_t ADD COLUMN b int4", [])
        with pytest.raises(RowShapeError, match='column 2 "b" matches no field of `Row`'):
            await fetch_with_shape(conn, "SELECT * FROM named_t", row_shape)

    run_with_conn(schema, body)


@pytest.mark.db
def test_named_fetch_and_stream_through_a_transaction(schema: str) -> None:
    row_shape = shape(kwargs_of, "keyword", [spec("n")])
    sql = "SELECT g::int4 AS n FROM generate_series(1, 5) g"

    async def body(conn: Connection) -> tuple[list[object], list[object]]:
        async with conn.transaction() as tx:
            fetched = await tx._fetch(sql, [], None, False, False, None, row_shape)  # ruff: ignore[boolean-positional-value-in-call]
            streamed, err = await drain(tx._stream(sql, [], None, False, None, 2, row_shape))  # ruff: ignore[boolean-positional-value-in-call]
            assert err is None
        return fetched, streamed

    fetched, streamed = run_with_conn(schema, body)
    assert fetched == streamed == [{"n": n} for n in range(1, 6)]


@pytest.mark.db
def test_pool_recovers_after_a_named_fetch_fails(schema: str) -> None:
    good = shape(kwargs_of, "keyword", [spec("n")])
    mapping = shape(kwargs_of, "keyword", [spec("n"), spec("m")], name="Two")
    null = shape(kwargs_of, "keyword", [spec("n")], name="Num")
    ctor = shape(raising_on({2}), "keyword", [spec("n")], name="Num")

    async def fetch_on(pool: Pool, sql: str, row_shape: RowShape) -> list[object]:
        return await pool._fetch(sql, [], None, False, False, None, row_shape)  # ruff: ignore[boolean-positional-value-in-call]

    async def run() -> list[object]:
        async with Pool(schema, max_size=1) as pool:
            with pytest.raises(RowShapeError, match="matches no field"):
                await fetch_on(pool, "SELECT 1::int4 AS n, 2::int4 AS z", mapping)
            assert await fetch_on(pool, "SELECT 1::int4 AS n", good) == [{"n": 1}]
            with pytest.raises(RowShapeError, match="NULL in column 1"):
                await fetch_on(pool, "SELECT NULL::int4 AS n", null)
            assert await fetch_on(pool, "SELECT 2::int4 AS n", good) == [{"n": 2}]
            with pytest.raises(RowConstructError):
                await fetch_on(pool, "SELECT g::int4 AS n FROM generate_series(1, 3) g", ctor)
            return await fetch_on(pool, "SELECT 3::int4 AS n", good)

    assert asyncio.run(run()) == [{"n": 3}]


@pytest.mark.db
def test_positional_form_that_skips_a_field_is_an_interface_error(schema: str) -> None:
    # by-name mapping with a positional constructor is not a shape the checker produces
    row_shape = shape(
        args_of,
        "positional",
        [spec("a"), spec("b", required=False), spec("c")],
        matching="by_name",
    )

    async def body(conn: Connection) -> list[object]:
        with pytest.raises(InterfaceError, match="cannot skip a field"):
            await fetch_with_shape(conn, "SELECT 1::int4 AS a, 3::int4 AS c", row_shape)
        return await fetch_with_shape(conn, "SELECT 1::int4 AS a, 2::int4 AS b, 3::int4 AS c", row_shape)

    assert run_with_conn(schema, body) == [(1, 2, 3)]


# from _core/session/test__core.py

# one value per setting that changes how the server parses or prints text
SETTINGS: Final[list[tuple[str, str]]] = [
    ("DateStyle", "ISO, YMD"),
    ("DateStyle", "SQL, DMY"),
    ("DateStyle", "German"),
    ("DateStyle", "Postgres, MDY"),
    ("IntervalStyle", "sql_standard"),
    ("IntervalStyle", "postgres_verbose"),
    ("IntervalStyle", "iso_8601"),
    ("TimeZone", "UTC"),
    ("TimeZone", "Asia/Kolkata"),
    ("TimeZone", "America/Los_Angeles"),
    ("extra_float_digits", "-15"),
    ("extra_float_digits", "0"),
    ("extra_float_digits", "3"),
]

PLUS_530: Final[dt.timezone] = dt.timezone(dt.timedelta(hours=5, minutes=30))
MINUS_8: Final[dt.timezone] = dt.timezone(dt.timedelta(hours=-8))

# SQL type -> value sent as text; the numeric, timetz, json, enum and array codecs all travel as text
TEXT_PARAMS: Final[list[tuple[str, Any]]] = [
    ("numeric", Decimal("-1234.5678")),
    ("numeric", Decimal("NaN")),
    ("numeric", Decimal("1E+30")),
    ("timetz", dt.time(1, 2, 3, 4, tzinfo=PLUS_530)),
    ("timetz", dt.time(23, 59, 59, tzinfo=MINUS_8)),
    ("json", {"d": "2024-01-02", "f": 0.1, "n": [1, None]}),
    ("jsonb", {"d": "2024-01-02", "f": 0.1, "n": [1, None]}),
    ("mood", "happy"),
    ("numeric[]", [Decimal("1.50"), None, Decimal("-0.001")]),
    ("timetz[]", [dt.time(1, 2, 3, tzinfo=PLUS_530), None]),
    ("mood[]", ["happy", "ok", None]),
    ("jsonb[]", [{"a": 1}, None]),
    ("float8[]", [0.1, 1e-320, None]),
]

# binary parameters are not affected by the settings, so they act as a control
BINARY_PARAMS: Final[list[tuple[str, Any]]] = [
    ("interval", dt.timedelta(days=1, hours=2, microseconds=5)),
    ("timestamptz", dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC)),
    ("timestamp", dt.datetime(2024, 1, 2, 3, 4, 5)),  # ruff: ignore[call-datetime-without-tzinfo]
    ("date", dt.date(2024, 2, 29)),
    ("float8", 0.1),
]


def with_setting(dsn: str, name: str, value: str) -> str:
    options = f"{conninfo_to_dict(dsn).get('options', '')} -c {name}={value.replace(' ', chr(92) + ' ')}"
    return make_conninfo(dsn, options=options)


def fetch_scalar_value(dsn: str, sql: str, value: object) -> object:
    async def run() -> object:
        conn = await connect(dsn)
        try:
            rows = await conn._impl._fetch(sql, [value], None, scalar=True, limit_one=True, arity=1, shape=None)
        finally:
            await conn.close()
        return rows[0]

    return asyncio.run(run())


def same_decimal_nan(left: object, right: object) -> bool:
    if isinstance(left, Decimal) and left.is_nan():
        return isinstance(right, Decimal) and right.is_nan()
    return left == right


@pytest.mark.parametrize(("name", "setting"), SETTINGS)
@pytest.mark.parametrize(("sql_type", "value"), [*TEXT_PARAMS, *BINARY_PARAMS], ids=lambda v: str(v)[:24])
@pytest.mark.db
def test_parameter_survives_session_settings(
    schema: str, name: str, setting: str, sql_type: str, value: object
) -> None:
    dsn = with_setting(schema, name, setting)
    # results come back in binary, so the value read back is the one the server parsed from the parameter
    assert same_decimal_nan(fetch_scalar_value(dsn, f"SELECT $1::{sql_type}", value), value)


# from codec/test_extension.py

INVALID_TEXT_REPRESENTATION: Final[str] = "22P02"

# the extensions live in public, which the test search_path leaves out
VALUES: Final[dict[str, Any]] = {
    "public.hstore": {"a": "1", "b": None, "c d": "", "é": "ü"},
    "public.ltree": "Top.Science.Astronomy",
    "public.lquery": "Top.*{1,2}.Astronomy",
    "public.cube": Cube((-1.5, 2.0), (3.0, 1e10)),
    "public.citext": "MiXed",
}


def read(dsn: str, sql: str, params: list[object] | None = None) -> Any:  # ruff: ignore[any-type]
    async def run(conn: Connection) -> Any:  # ruff: ignore[any-type]
        return (await conn._impl._fetch(sql, params or [], None, scalar=True, limit_one=True, arity=1, shape=None))[0]

    return run_with_conn(dsn, run)


@pytest.mark.parametrize("sql_type", sorted(VALUES))
@pytest.mark.db
def test_extension_value_round_trips(schema: str, sql_type: str) -> None:
    assert read(schema, f"SELECT $1::{sql_type}", [VALUES[sql_type]]) == VALUES[sql_type]


@pytest.mark.parametrize("sql_type", sorted(VALUES))
@pytest.mark.db
def test_extension_array_decodes_and_encodes_with_null_elements_and_dimensions(schema: str, sql_type: str) -> None:
    value = VALUES[sql_type]
    assert read(schema, f"SELECT $1::{sql_type}[]", [[value, None, value]]) == [value, None, value]
    matrix = [[value, None], [value, value]]
    assert read(schema, f"SELECT $1::{sql_type}[]", [matrix]) == matrix
    assert read(schema, f"SELECT '{{}}'::{sql_type}[]") == []
    assert read(schema, f"SELECT ARRAY[NULL]::{sql_type}[]") == [None]


@pytest.mark.parametrize("sql_type", sorted(VALUES))
@pytest.mark.db
def test_extension_is_read_back_as_null_for_null(schema: str, sql_type: str) -> None:
    assert read(schema, f"SELECT $1::{sql_type}", [None]) is None


@pytest.mark.db
def test_citext_array_binds_a_list_of_str_and_reads_back_str(schema: str) -> None:
    # the server treats citext[] as its own type (sqlx notes it is not text[] for parameters), so the element
    # type must come from the parameter, not from the Python values
    assert read(schema, "SELECT $1::public.citext[]", [["a", "B", None]]) == ["a", "B", None]
    assert read(schema, "SELECT ($1::public.citext[])[1] OPERATOR(public.=) 'A'", [["a"]]) is True
    assert read(schema, "SELECT $1::public.citext[] = ARRAY['A']::public.citext[]", [["a"]]) is True


@pytest.mark.db
def test_citext_array_is_not_accepted_for_a_text_array_parameter_by_the_server(schema: str) -> None:
    # not a rowx rule: it documents why the checker and the encoder follow the parameter's own type
    with pytest.raises(DatabaseError, match="operator does not exist"):
        read(schema, "SELECT $1::text[] = ARRAY['a']::public.citext[]", [["a"]])


# --- hstore ---


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("", {}),
        ("a=>1", {"a": "1"}),
        ("a=>NULL", {"a": None}),
        ('a=>"NULL"', {"a": "NULL"}),
        ('"a b"=>"c, d"', {"a b": "c, d"}),
        ('k=>""', {"k": ""}),
        ('"a=>b"=>"c=>d"', {"a=>b": "c=>d"}),
        ("a=>1, a=>2", {"a": "1"}),
        ('"quote\\"d"=>"back\\\\slash"', {'quote"d': "back\\slash"}),
    ],
)
@pytest.mark.db
def test_hstore_decodes_from_server_literals(schema: str, literal: str, expected: dict[str, str | None]) -> None:
    assert read(schema, f"SELECT '{literal}'::public.hstore") == expected


@pytest.mark.db
def test_hstore_empty_value_is_not_null(schema: str) -> None:
    value = read(schema, "SELECT $1::public.hstore", [{"empty": "", "null": None}])
    assert value == {"empty": "", "null": None}
    assert value["empty"] is not None


@pytest.mark.db
def test_hstore_encoded_value_means_the_same_to_the_server(schema: str) -> None:
    value = {"k": "v", "e": "", "n": None}
    assert read(schema, "SELECT $1::public.hstore OPERATOR(public.->) 'k'", [value]) == "v"
    assert len(read(schema, "SELECT $1::public.hstore OPERATOR(public.->) 'e'", [value])) == 0
    assert read(schema, "SELECT public.exist($1::public.hstore, 'n')", [value]) is True
    assert read(schema, "SELECT ($1::public.hstore OPERATOR(public.->) 'n') IS NULL", [value]) is True
    assert read(schema, "SELECT public.akeys($1::public.hstore)", [{"b": "1", "a": "2"}]) == ["a", "b"]


@pytest.mark.db
def test_hstore_null_key_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*key cannot be NULL"):
        read(schema, "SELECT $1::public.hstore", [{None: "x"}])


@pytest.mark.parametrize("value", [{"a": 1}, {1: "a"}, {"a": b"x"}, {"a": ["x"]}, {("a",): "x"}])
@pytest.mark.db
def test_hstore_non_str_key_or_value_is_encode_error(schema: str, value: dict[Any, Any]) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*hstore (key|value) must be a str"):
        read(schema, "SELECT $1::public.hstore", [value])


@pytest.mark.parametrize("value", ["a=>1", ["a"], 1, b"a=>1"])
@pytest.mark.db
def test_hstore_only_takes_a_dict(schema: str, value: object) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, "SELECT $1::public.hstore", [value])


@pytest.mark.db
def test_hstore_nul_in_a_string_is_rejected_by_the_server(schema: str) -> None:
    with pytest.raises(DatabaseError):
        read(schema, "SELECT $1::public.hstore", [{"a\0b": "x"}])


@pytest.mark.db
def test_hstore_large_value_round_trips(schema: str) -> None:
    value = {f"key{index}": None if index % 3 == 0 else "v" * index for index in range(300)}
    assert read(schema, "SELECT $1::public.hstore", [value]) == value


# --- ltree and lquery ---


@pytest.mark.parametrize("sql_type", ["public.ltree", "public.lquery"])
@pytest.mark.db
def test_label_tree_nul_is_encode_error(schema: str, sql_type: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*NUL"):
        read(schema, f"SELECT $1::{sql_type}", ["a\0b"])


@pytest.mark.parametrize("sql_type", ["public.ltree", "public.lquery"])
@pytest.mark.parametrize("value", [1, b"a", ["a"], {"a": "b"}, 1.5])
@pytest.mark.db
def test_label_tree_only_takes_a_str(schema: str, sql_type: str, value: object) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, f"SELECT $1::{sql_type}", [value])


@pytest.mark.parametrize("value", ["a b", "a..b", ".a", "a.", "a.*"])
@pytest.mark.db
def test_ltree_invalid_text_is_a_server_error(schema: str, value: str) -> None:
    with pytest.raises(DatabaseError) as info:
        read(schema, "SELECT $1::public.ltree", [value])
    assert info.value.sqlstate in {"42601", INVALID_TEXT_REPRESENTATION, "22023"}


@pytest.mark.db
def test_ltree_empty_path_round_trips(schema: str) -> None:
    assert len(read(schema, "SELECT $1::public.ltree", [""])) == 0
    assert len(read(schema, "SELECT ''::public.ltree")) == 0


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("a", "a"),
        ("a.b.c", "a.b.c"),
        ("A_1.b-2", "A_1.b-2"),
    ],
)
@pytest.mark.db
def test_ltree_decodes_from_server_literals(schema: str, literal: str, expected: str) -> None:
    assert read(schema, f"SELECT '{literal}'::public.ltree") == expected


@pytest.mark.db
def test_ltree_and_lquery_work_in_server_operators(schema: str) -> None:
    assert (
        read(schema, "SELECT $1::public.ltree OPERATOR(public.~) $2::public.lquery", ["Top.Science", "Top.*"]) is True
    )
    assert read(schema, "SELECT public.nlevel($1::public.ltree)", ["a.b.c"]) == 3


# --- cube ---


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("()", Cube((), ())),
        ("(1)", Cube((1.0,), (1.0,))),
        ("(1),(2)", Cube((1.0,), (2.0,))),
        ("(1,2)", Cube((1.0, 2.0), (1.0, 2.0))),
        ("(1,2),(3,4)", Cube((1.0, 2.0), (3.0, 4.0))),
        # the server keeps the corners in the order given, so `lower_left` is only the first corner written
        ("(3,4),(1,2)", Cube((3.0, 4.0), (1.0, 2.0))),
        ("(-1.5e3,0),(2,1e-3)", Cube((-1500.0, 0.0), (2.0, 0.001))),
    ],
)
@pytest.mark.db
def test_cube_decodes_from_server_literals(schema: str, literal: str, expected: Cube) -> None:
    assert read(schema, f"SELECT '{literal}'::public.cube") == expected


@pytest.mark.db
def test_cube_point_is_sent_as_a_point(schema: str) -> None:
    point = Cube((1.0, 2.0), (1.0, 2.0))
    assert read(schema, "SELECT public.cube_is_point($1::public.cube)", [point]) is True
    assert read(schema, "SELECT $1::public.cube::text", [point]) == "(1, 2)"
    # corners that differ anywhere make a box
    box = Cube((1.0, 2.0), (1.0, 3.0))
    assert read(schema, "SELECT public.cube_is_point($1::public.cube)", [box]) is False
    assert read(schema, "SELECT $1::public.cube::text", [box]) == "(1, 2),(1, 3)"


@pytest.mark.db
def test_cube_dimension_is_kept(schema: str) -> None:
    assert read(schema, "SELECT public.cube_dim($1::public.cube)", [Cube((1.0,) * 5, (2.0,) * 5)]) == 5
    assert read(schema, "SELECT public.cube_dim($1::public.cube)", [Cube((), ())]) == 0
    assert read(schema, "SELECT public.cube_dim($1::public.cube)", [Cube((1.0,) * 100, (1.0,) * 100)]) == 100


@pytest.mark.db
def test_cube_corners_of_different_lengths_are_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*same number"):
        read(schema, "SELECT $1::public.cube", [Cube((1.0,), (1.0, 2.0))])


@pytest.mark.db
def test_cube_past_the_dimension_limit_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*100"):
        read(schema, "SELECT $1::public.cube", [Cube((0.0,) * 101, (0.0,) * 101)])


@pytest.mark.parametrize("value", ["(1,2)", (1.0, 2.0), [1.0], 1.0, {"a": 1}])
@pytest.mark.db
def test_cube_only_takes_a_cube(schema: str, value: object) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, "SELECT $1::public.cube", [value])


@pytest.mark.db
def test_cube_with_a_non_number_coordinate_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*not a number"):
        read(schema, "SELECT $1::public.cube", [Cube(("a",), (1.0,))])  # ty: ignore[invalid-argument-type]


@pytest.mark.db
def test_cube_takes_ints_and_keeps_special_floats(schema: str) -> None:
    assert read(schema, "SELECT $1::public.cube", [Cube((1,), (2,))]) == Cube((1.0,), (2.0,))
    assert read(schema, "SELECT $1::public.cube", [Cube((float("inf"),), (float("inf"),))]) == Cube(
        (float("inf"),), (float("inf"),)
    )
    assert read(schema, "SELECT $1::public.cube", [Cube((-0.0,), (0.0,))]) == Cube((-0.0,), (0.0,))


@pytest.mark.db
def test_cube_server_operators_see_the_value(schema: str) -> None:
    assert (
        read(
            schema,
            "SELECT $1::public.cube OPERATOR(public.&&) $2::public.cube",
            [Cube((0.0,), (2.0,)), Cube((1.0,), (3.0,))],
        )
        is True
    )
    distance = read(
        schema,
        "SELECT public.cube_distance($1::public.cube, $2::public.cube)",
        [Cube((0.0, 0.0), (0.0, 0.0)), Cube((3.0, 4.0), (3.0, 4.0))],
    )
    assert distance == pytest.approx(5.0)


# from codec/test_array_decode.py


def fetch_query[T](dsn: str, q: query[T]) -> T:
    return run_with_conn(dsn, q.one)


@pytest.mark.db
def test_one_dimension_with_null(schema: str) -> None:
    assert fetch_query(schema, query[list[int | None]](t"SELECT ARRAY[1,NULL,3]")) == [1, None, 3]


@pytest.mark.db
def test_two_dimensions(schema: str) -> None:
    got = fetch_query(schema, query[list[list[int]]](t"SELECT '{{{{1,2}},{{3,4}}}}'::int[]"))
    assert got == [[1, 2], [3, 4]]


@pytest.mark.db
def test_three_dimensions(schema: str) -> None:
    got = fetch_query(
        schema, query[list[list[list[int]]]](t"SELECT '{{{{{{1,2}},{{3,4}}}},{{{{5,6}},{{7,8}}}}}}'::int[]")
    )
    assert got == [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]


@pytest.mark.db
def test_empty_array(schema: str) -> None:
    assert fetch_query(schema, query[list[str]](t"SELECT '{{}}'::text[]")) == []


@pytest.mark.db
def test_all_null_array(schema: str) -> None:
    assert fetch_query(schema, query[list[int | None]](t"SELECT ARRAY[NULL, NULL]::int[]")) == [None, None]


@pytest.mark.db
def test_null_array_is_none(schema: str) -> None:
    assert fetch_query(schema, query[list[int] | None](t"SELECT NULL::int[]")) is None


@pytest.mark.db
def test_text_elements_keep_special_characters(schema: str) -> None:
    got = fetch_query(schema, query[list[str | None]](t"""SELECT ARRAY['a,b', 'q"r', '', 'NULL', NULL]"""))
    assert got == ["a,b", 'q"r', "", "NULL", None]


@pytest.mark.db
def test_non_default_lower_bound_is_ignored(schema: str) -> None:
    got = fetch_query(schema, query[list[int]](t"SELECT '[5:7]={{1,2,3}}'::int[]"))
    assert got == [1, 2, 3]


@pytest.mark.db
def test_enum_array(schema: str) -> None:
    got = fetch_query(schema, query[list[str]](t"SELECT ARRAY['sad','happy']::mood[]"))
    assert got == ["sad", "happy"]


@pytest.mark.db
def test_domain_array(schema: str) -> None:
    got = fetch_query(schema, query[list[int]](t"SELECT ARRAY[1,2]::positive_int[]"))
    assert got == [1, 2]


@pytest.mark.db
def test_domain_over_array(schema: str) -> None:
    got = fetch_query(schema, query[list[int]](t"SELECT ARRAY[3,4]::positive_ints"))
    assert got == [3, 4]


@pytest.mark.db
def test_uuid_array(schema: str) -> None:
    text = "123e4567-e89b-12d3-a456-426614174000"
    got = fetch_query(schema, query[list[UUID]](t"SELECT ARRAY[{text}::uuid]"))
    assert got == [UUID(text)]


@pytest.mark.db
def test_timestamptz_array(schema: str) -> None:
    got = fetch_query(schema, query[list[datetime]](t"SELECT ARRAY['2024-02-29 12:00:00+00'::timestamptz]"))
    assert got == [datetime(2024, 2, 29, 12, tzinfo=UTC)]


# from codec/test_array_encode.py

AWKWARD: Final[list[str]] = [
    'a"b',
    "a\\b",
    "a,b",
    "{x}",
    "NULL",
    "null",
    "",
    " lead",
    "trail ",
    "two  spaces",
    "é☃",
    "{",
    "}",
]


def fetch_all[T](dsn: str, q: query[T]) -> list[T]:
    return run_with_conn(dsn, q.all)


@pytest.mark.db
def test_any_with_list(schema: str) -> None:
    ids = [1, 2, 3]
    got = fetch_all(schema, query[int](t"SELECT n FROM generate_series(1, 5) AS n WHERE n = ANY({ids}) ORDER BY n"))
    assert got == [1, 2, 3]


@pytest.mark.db
def test_any_with_tuple(schema: str) -> None:
    ids = (4, 5)
    got = fetch_all(schema, query[int](t"SELECT n FROM generate_series(1, 5) AS n WHERE n = ANY({ids}) ORDER BY n"))
    assert got == [4, 5]


@pytest.mark.db
def test_int_array_round_trip(schema: str) -> None:
    value = [1, None, 3]
    assert fetch_query(schema, query[list[int | None]](t"SELECT {value}::int[]")) == value


@pytest.mark.db
def test_two_dimensions_encode(schema: str) -> None:
    value = [[1, 2], [3, 4], [5, 6]]
    assert fetch_query(schema, query[list[list[int]]](t"SELECT {value}::int[]")) == value


@pytest.mark.parametrize("value", AWKWARD)
@pytest.mark.db
def test_awkward_text_binary(schema: str, value: str) -> None:
    assert fetch_query(schema, query[list[str]](t"SELECT {[value]}::text[]")) == [value]


@pytest.mark.db
def test_all_awkward_strings_together(schema: str) -> None:
    assert fetch_query(schema, query[list[str | None]](t"SELECT {[*AWKWARD, None]}::text[]")) == [*AWKWARD, None]


@pytest.mark.parametrize("value", AWKWARD)
@pytest.mark.db
def test_awkward_text_literal(schema: str, value: str) -> None:
    # jsonb elements travel as text, so the array becomes a text literal
    assert fetch_query(schema, query[list[str]](t"SELECT {[value]}::jsonb[]")) == [value]


@pytest.mark.db
def test_text_literal_matrix_with_null(schema: str) -> None:
    value = [['a"b', None], ["NULL", "\\"]]
    assert fetch_query(schema, query[list[list[str | None]]](t"SELECT {value}::jsonb[]")) == value


@pytest.mark.db
def test_decimal_array(schema: str) -> None:
    value = [Decimal("1.50"), Decimal("-0.001"), None, Decimal("12345678901234567890.123456789")]
    assert fetch_query(schema, query[list[Decimal | None]](t"SELECT {value}::numeric[]")) == value


@pytest.mark.db
def test_decimal_matrix(schema: str) -> None:
    value = [[Decimal("1.5"), Decimal(2)], [Decimal(3), Decimal("4.25")]]
    assert fetch_query(schema, query[list[list[Decimal]]](t"SELECT {value}::numeric[]")) == value


@pytest.mark.db
def test_empty_list(schema: str) -> None:
    assert fetch_query(schema, query[list[int]](t"SELECT {[]}::int[]")) == []


@pytest.mark.db
def test_empty_decimal_list(schema: str) -> None:
    assert fetch_query(schema, query[list[Decimal]](t"SELECT {[]}::numeric[]")) == []


@pytest.mark.db
def test_list_with_none(schema: str) -> None:
    value = [1, None]
    assert fetch_query(schema, query[list[int | None]](t"SELECT {value}::int[]")) == value


@pytest.mark.db
def test_all_null_numeric(schema: str) -> None:
    value = [None, None]
    assert fetch_query(schema, query[list[Decimal | None]](t"SELECT {value}::numeric[]")) == value


@pytest.mark.db
def test_domain_element_array(schema: str) -> None:
    value = [1, 2, 3]
    assert fetch_query(schema, query[list[int]](t"SELECT {value}::positive_int[]")) == value


@pytest.mark.db
def test_domain_element_violation(schema: str) -> None:
    with pytest.raises(DatabaseError):
        fetch_query(schema, query[list[int]](t"SELECT {[0, 1]}::positive_int[]"))


@pytest.mark.db
def test_none_array(schema: str) -> None:
    assert fetch_query(schema, query[list[int] | None](t"SELECT {None}::int[]")) is None


@pytest.mark.db
def test_uuid_list_mixing_uuid_and_str(schema: str) -> None:
    u = UUID("12345678-1234-5678-1234-567812345678")
    v = UUID("87654321-4321-8765-4321-876543218765")
    value = [u, str(v), None]
    assert fetch_query(schema, query[list[UUID | None]](t"SELECT {value}::uuid[]")) == [u, v, None]


@pytest.mark.parametrize(
    "value",
    [[[1, 2], [3]], [1, [2]], [[1], 2], [[1, 2], [3, 4], [5]], [[], [1]], [[], 42], [[1], []]],
)
@pytest.mark.db
def test_ragged_raises(schema: str, value: list[object]) -> None:
    with pytest.raises(EncodeError):
        fetch_query(schema, query[list[list[int]]](t"SELECT {value}::int[]"))


@pytest.mark.db
def test_wrong_element_type_raises(schema: str) -> None:
    with pytest.raises(EncodeError):
        fetch_query(schema, query[list[int]](t"SELECT {['x']}::int[]"))


@pytest.mark.db
def test_str_is_not_a_sequence(schema: str) -> None:
    with pytest.raises(EncodeError):
        fetch_query(schema, query[list[str]](t"SELECT {'abc'}::text[]"))


@pytest.mark.db
def test_json_list_still_encodes(schema: str) -> None:
    value = [1, "a", {"k": [None]}]
    assert fetch_query(schema, query[list[object]](t"SELECT {value}::jsonb")) == value


# from codec/test_bits.py

STRING_DATA_LENGTH_MISMATCH: Final[str] = "22026"
# around the byte boundary, and past 64 bits where no integer holds the value
LENGTHS: Final[list[int]] = [1, 7, 8, 9, 15, 16, 17, 63, 64, 65, 129]


def pattern(length: int) -> str:
    # alternating, starting and ending with 1, so a swapped bit order or a lost last bit shows
    return ("10" * length)[: length - 1] + "1" if length > 1 else "1"


@pytest.mark.parametrize("length", LENGTHS)
@pytest.mark.db
def test_bit_decodes_to_its_digits(schema: str, length: int) -> None:
    bits = pattern(length)
    assert read(schema, f"SELECT B'{bits}'::bit({length})") == bits


@pytest.mark.parametrize("length", [0, *LENGTHS])
@pytest.mark.db
def test_varbit_decodes_to_its_digits(schema: str, length: int) -> None:
    bits = pattern(length) if length else ""
    assert read(schema, f"SELECT '{bits}'::varbit") == bits


@pytest.mark.db
def test_bit_order_is_high_bit_first(schema: str) -> None:
    assert read(schema, "SELECT B'10000000'") == "10000000"
    assert read(schema, "SELECT B'00000001'") == "00000001"
    assert read(schema, "SELECT B'1000000000000001'") == "1000000000000001"


@pytest.mark.parametrize("length", LENGTHS)
@pytest.mark.db
def test_bit_round_trips(schema: str, length: int) -> None:
    bits = pattern(length)
    assert read(schema, f"SELECT $1::bit({length})", [bits]) == bits
    assert read(schema, "SELECT $1::varbit", [bits]) == bits


@pytest.mark.db
def test_empty_varbit_round_trips(schema: str) -> None:
    empty = read(schema, "SELECT $1::varbit", [""])
    assert isinstance(empty, str)
    assert len(empty) == 0
    assert read(schema, "SELECT length($1::varbit)", [""]) == 0


@pytest.mark.db
def test_all_zero_and_all_one_strings_keep_their_length(schema: str) -> None:
    for bits in ("0" * 9, "1" * 9, "0" * 70, "1" * 70):
        assert read(schema, "SELECT $1::varbit", [bits]) == bits


@pytest.mark.db
def test_server_sees_the_bits_we_send(schema: str) -> None:
    assert read(schema, "SELECT $1::varbit::text", ["101"]) == "101"
    assert read(schema, "SELECT get_bit($1::varbit, 0)", ["100000000"]) == 1
    assert read(schema, "SELECT get_bit($1::varbit, 8)", ["100000001"]) == 1
    assert read(schema, "SELECT bit_length($1::varbit)", ["1" * 70]) == 70


@pytest.mark.db
def test_bit_arrays_round_trip_with_null_elements(schema: str) -> None:
    assert read(schema, "SELECT '{101,NULL,000}'::bit(3)[]") == ["101", None, "000"]
    value = [["10101010101", None], ["1", ""]]
    assert read(schema, "SELECT $1::varbit[]", [value]) == value
    assert read(schema, "SELECT $1::bit(3)[]", [["101", None]]) == ["101", None]


@pytest.mark.db
def test_bit_is_read_through_a_query(schema: str) -> None:
    async def body(conn: Connection) -> tuple[str, str | None, list[str | None]]:
        one = await query[str](t"SELECT {'1011'}::varbit").one(conn)
        none = await query[str | None](t"SELECT NULL::bit(3)").one(conn)
        many = await query[list[str | None]](t"SELECT ARRAY[{'11'}, NULL]::varbit[]").one(conn)
        return one, none, many

    assert run_with_conn(schema, body) == ("1011", None, ["11", None])


# a parameter is typed `bit`, so only the server's assignment cast sees the declared length
@pytest.mark.parametrize("bits", ["101", "1010101010", ""])
@pytest.mark.db
def test_bit_n_parameter_of_the_wrong_length_is_rejected_by_the_server(schema: str, bits: str) -> None:
    async def body(conn: Connection) -> None:
        await conn._impl._execute("CREATE TEMP TABLE bit_fixed (b bit(8))", [])
        await conn._impl._execute("INSERT INTO bit_fixed VALUES ($1)", [bits])

    with pytest.raises(DatabaseError) as info:
        run_with_conn(schema, body)
    assert info.value.sqlstate == STRING_DATA_LENGTH_MISMATCH


@pytest.mark.db
def test_bit_n_parameter_of_the_right_length_is_accepted(schema: str) -> None:
    async def body(conn: Connection) -> object:
        await conn._impl._execute("CREATE TEMP TABLE bit_fixed (b bit(8))", [])
        await conn._impl._execute("INSERT INTO bit_fixed VALUES ($1)", ["10100101"])
        return (
            await conn._impl._fetch(
                "SELECT b FROM bit_fixed", [], None, scalar=True, limit_one=True, arity=1, shape=None
            )
        )[0]

    assert run_with_conn(schema, body) == "10100101"


@pytest.mark.parametrize("bits", ["102", "1 0", " 1", "x", "\uff11"])
@pytest.mark.db
def test_other_characters_are_an_encode_error(schema: str, bits: str) -> None:
    with pytest.raises(EncodeError, match="0` and `1"):
        read(schema, "SELECT $1::varbit", [bits])


@pytest.mark.db
def test_bit_takes_only_str(schema: str) -> None:
    for value in (5, b"101", [1, 0, 1]):
        with pytest.raises(EncodeError):
            read(schema, "SELECT $1::varbit", [value])


# from codec/test_network.py


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("10.0.0.1/24", ipaddress.IPv4Interface("10.0.0.1/24")),
        ("0.0.0.0/0", ipaddress.IPv4Interface("0.0.0.0/0")),
        ("2001:db8::1/64", ipaddress.IPv6Interface("2001:db8::1/64")),
        ("::/0", ipaddress.IPv6Interface("::/0")),
        # a full-width mask still decodes to an interface, so the result type never depends on the value
        ("1.2.3.4", ipaddress.IPv4Interface("1.2.3.4/32")),
        ("::1", ipaddress.IPv6Interface("::1/128")),
        ("::ffff:1.2.3.4", ipaddress.IPv6Interface("::ffff:1.2.3.4/128")),
    ],
)
@pytest.mark.db
def test_inet_decodes_to_an_interface(schema: str, literal: str, expected: object) -> None:
    got = read(schema, f"SELECT '{literal}'::inet")
    assert type(got) is type(expected)
    assert got == expected
    assert got.network.prefixlen == expected.network.prefixlen  # ty: ignore[missing-attribute]


@pytest.mark.parametrize("literal", ["10.0.0.0/8", "0.0.0.0/0", "2001:db8::/32", "::/0", "1.2.3.4/32"])
@pytest.mark.db
def test_cidr_decodes_to_a_network(schema: str, literal: str) -> None:
    got = read(schema, f"SELECT '{literal}'::cidr")
    assert got == ipaddress.ip_network(literal)
    assert type(got) is type(ipaddress.ip_network(literal))


@pytest.mark.parametrize(
    "value",
    [
        ipaddress.IPv4Interface("192.168.0.7/24"),
        ipaddress.IPv6Interface("2001:db8::7/48"),
        ipaddress.IPv4Interface("0.0.0.0/0"),
        ipaddress.IPv6Interface("::ffff:1.2.3.4/128"),
    ],
)
@pytest.mark.db
def test_inet_interface_round_trips(schema: str, value: object) -> None:
    assert read(schema, "SELECT $1::inet", [value]) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (ipaddress.IPv4Address("1.2.3.4"), ipaddress.IPv4Interface("1.2.3.4/32")),
        (ipaddress.IPv6Address("::ffff:1.2.3.4"), ipaddress.IPv6Interface("::ffff:1.2.3.4/128")),
        ("10.1.2.3/8", ipaddress.IPv4Interface("10.1.2.3/8")),
        ("2001:db8::1", ipaddress.IPv6Interface("2001:db8::1/128")),
    ],
)
@pytest.mark.db
def test_inet_accepts_addresses_and_strings(schema: str, value: object, expected: object) -> None:
    assert read(schema, "SELECT $1::inet", [value]) == expected


@pytest.mark.parametrize(
    "value",
    [ipaddress.IPv4Network("10.0.0.0/8"), ipaddress.IPv6Network("2001:db8::/32"), ipaddress.IPv4Network("0.0.0.0/0")],
)
@pytest.mark.db
def test_cidr_round_trips(schema: str, value: object) -> None:
    assert read(schema, "SELECT $1::cidr", [value]) == value


@pytest.mark.db
def test_cidr_accepts_a_string(schema: str) -> None:
    assert read(schema, "SELECT $1::cidr", ["10.0.0.0/8"]) == ipaddress.IPv4Network("10.0.0.0/8")


@pytest.mark.parametrize("value", ["10.0.0.1/8", "2001:db8::1/32"])
@pytest.mark.db
def test_cidr_with_host_bits_is_an_encode_error(schema: str, value: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*host bits"):
        read(schema, "SELECT $1::cidr", [value])


@pytest.mark.parametrize("value", ["not an address", "1.2.3.4/33", "", "fe80::1%eth0"])
@pytest.mark.db
def test_inet_rejects_unparseable_strings_and_zone_ids(schema: str, value: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, "SELECT $1::inet", [value])


@pytest.mark.db
def test_ipv6_zone_id_object_is_an_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match="zone id"):
        read(schema, "SELECT $1::inet", [ipaddress.IPv6Address("fe80::1%eth0")])


@pytest.mark.parametrize(
    ("sql_type", "value"),
    [
        ("inet", ipaddress.IPv4Network("10.0.0.0/8")),
        ("cidr", ipaddress.IPv4Interface("10.0.0.1/8")),
        ("cidr", ipaddress.IPv4Address("10.0.0.1")),
        ("inet", 167772161),
        ("macaddr", b"\x08\x00\x2b\x01\x02\x03"),
    ],
)
@pytest.mark.db
def test_wrong_python_type_is_an_encode_error(schema: str, sql_type: str, value: object) -> None:
    with pytest.raises(EncodeError, match=rf"\$1.*{sql_type}"):
        read(schema, f"SELECT $1::{sql_type}", [value])


@pytest.mark.parametrize(
    ("sql_type", "literal", "expected"),
    [
        ("macaddr", "08:00:2B:01:02:03", "08:00:2b:01:02:03"),
        ("macaddr", "FF-FF-FF-FF-FF-FF", "ff:ff:ff:ff:ff:ff"),
        ("macaddr8", "08:00:2b:01:02:03:04:05", "08:00:2b:01:02:03:04:05"),
        # a six byte value is widened by the server with ff:fe in the middle
        ("macaddr8", "08:00:2b:01:02:03", "08:00:2b:ff:fe:01:02:03"),
    ],
)
@pytest.mark.db
def test_mac_decodes_to_lower_case_colons(schema: str, sql_type: str, literal: str, expected: str) -> None:
    assert read(schema, f"SELECT '{literal}'::{sql_type}") == expected


@pytest.mark.parametrize(
    ("sql_type", "value", "expected"),
    [
        ("macaddr", "08:00:2b:01:02:03", "08:00:2b:01:02:03"),
        ("macaddr", "08-00-2B-01-02-03", "08:00:2b:01:02:03"),
        ("macaddr", "08002b010203", "08:00:2b:01:02:03"),
        ("macaddr8", "08:00:2b:01:02:03:04:05", "08:00:2b:01:02:03:04:05"),
        ("macaddr8", "08:00:2b:01:02:03", "08:00:2b:ff:fe:01:02:03"),
    ],
)
@pytest.mark.db
def test_mac_encodes_common_spellings(schema: str, sql_type: str, value: str, expected: str) -> None:
    assert read(schema, f"SELECT $1::{sql_type}", [value]) == expected


@pytest.mark.parametrize(
    ("sql_type", "value"),
    [
        ("macaddr", "08:00:2b"),
        ("macaddr", "zz:00:2b:01:02:03"),
        ("macaddr", "08:00:2b:01:02:03:04:05"),
        ("macaddr8", ""),
    ],
)
@pytest.mark.db
def test_bad_mac_is_an_encode_error(schema: str, sql_type: str, value: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, f"SELECT $1::{sql_type}", [value])


@pytest.mark.db
def test_network_arrays_round_trip_with_nulls_and_two_dimensions(schema: str) -> None:
    inets = [[ipaddress.IPv4Interface("10.0.0.1/24"), None], [ipaddress.IPv6Interface("::1/128"), None]]
    assert read(schema, "SELECT $1::inet[]", [inets]) == inets
    cidrs = [ipaddress.IPv4Network("10.0.0.0/8"), None, ipaddress.IPv6Network("2001:db8::/32")]
    assert read(schema, "SELECT $1::cidr[]", [cidrs]) == cidrs
    assert read(schema, "SELECT $1::inet[]", [["1.2.3.4", ipaddress.IPv4Address("5.6.7.8"), None]]) == [
        ipaddress.IPv4Interface("1.2.3.4/32"),
        ipaddress.IPv4Interface("5.6.7.8/32"),
        None,
    ]


@pytest.mark.db
def test_mac_arrays_round_trip(schema: str) -> None:
    assert read(schema, "SELECT $1::macaddr[]", [["08:00:2b:01:02:03", None]]) == ["08:00:2b:01:02:03", None]
    assert read(schema, "SELECT $1::macaddr8[]", [[["08:00:2b:01:02:03:04:05"], [None]]]) == [
        ["08:00:2b:01:02:03:04:05"],
        [None],
    ]


@pytest.mark.db
def test_array_element_error_names_the_element(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*element 1"):
        read(schema, "SELECT $1::cidr[]", [["10.0.0.0/8", "10.0.0.1/8"]])


@pytest.mark.db
def test_query_annotated_with_the_pair_decodes(schema: str) -> None:
    pair = ipaddress.IPv4Interface | ipaddress.IPv6Interface
    q = query[tuple[pair, pair | None, str]](t"SELECT '10.0.0.1/24'::inet, NULL::inet, '08:00:2b:01:02:03'::macaddr")
    assert run_with_conn(schema, q.one) == (ipaddress.IPv4Interface("10.0.0.1/24"), None, "08:00:2b:01:02:03")


# from codec/test_numeric.py


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
@pytest.mark.db
def test_numeric_round_trip(schema: str, value: Decimal) -> None:
    got = run_with_conn(schema, query[Decimal](t"SELECT {value}::numeric").one)
    if value.is_nan():
        assert got.is_nan()
    else:
        assert got == value


@pytest.mark.db
def test_numeric_keeps_trailing_zeros(schema: str) -> None:
    value = Decimal("1.5000")
    got = run_with_conn(schema, query[Decimal](t"SELECT {value}::numeric").one)
    assert str(got) == "1.5000"


@pytest.mark.db
def test_numeric_decodes_large_scale_literal(schema: str) -> None:
    q = query[Decimal](t"SELECT 1e-30::numeric")
    assert run_with_conn(schema, q.one) == Decimal("1E-30")


@pytest.mark.db
def test_int_to_numeric(schema: str) -> None:
    for x in (7, -(2**70)):
        got = run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one)
        assert got == Decimal(x)


@pytest.mark.db
def test_int_beyond_the_str_digit_limit_to_numeric(schema: str) -> None:
    x = 10**5000 + 7
    got = run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one)
    assert got == Decimal(x)


@pytest.mark.db
def test_int_beyond_the_str_digit_limit_to_int8_is_encode_error(schema: str) -> None:
    x = 10**5000
    with pytest.raises(EncodeError, match=r"\$1.*int8"):
        run_with_conn(schema, query[int](t"SELECT {x}::int8").one)


@pytest.mark.db
def test_numeric_decodes_more_than_32767_digit_groups(schema: str) -> None:
    q = query[Decimal](t"SELECT repeat('1', 131072)::numeric")
    assert run_with_conn(schema, q.one) == Decimal("1" * 131072)


@pytest.mark.parametrize("text", ["NaN123", "-NaN7"])
@pytest.mark.db
def test_nan_with_payload_is_sent_as_nan(schema: str, text: str) -> None:
    x = Decimal(text)
    assert run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one).is_nan()


@pytest.mark.parametrize("text", ["sNaN", "sNaN123"])
@pytest.mark.db
def test_snan_is_encode_error(schema: str, text: str) -> None:
    x = Decimal(text)
    with pytest.raises(EncodeError, match=r"\$1.*sNaN"):
        run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one)


@pytest.mark.db
def test_float_to_numeric_is_encode_error(schema: str) -> None:
    x = 1.5
    with pytest.raises(EncodeError, match=r"\$1.*float.*numeric"):
        run_with_conn(schema, query[Decimal](t"SELECT {x}::numeric").one)


@pytest.mark.db
def test_float8_round_trip(schema: str) -> None:
    x = 1.25e100
    assert run_with_conn(schema, query[float](t"SELECT {x}::float8").one) == x


@pytest.mark.db
def test_float8_special_values(schema: str) -> None:
    inf, nan = float("inf"), float("nan")
    q = query[tuple[float, float]](t"SELECT {inf}::float8, {nan}::float8")
    got_inf, got_nan = run_with_conn(schema, q.one)
    assert got_inf == inf
    assert got_nan != got_nan  # ruff: ignore[comparison-with-itself]


@pytest.mark.db
def test_float4_round_trip(schema: str) -> None:
    x = 0.5
    assert run_with_conn(schema, query[float](t"SELECT {x}::float4").one) == x


@pytest.mark.db
def test_float4_query_overflow_is_encode_error(schema: str) -> None:
    x = 1e300
    with pytest.raises(EncodeError, match=r"\$1.*float4"):
        run_with_conn(schema, query[float](t"SELECT {x}::float4").one)


@pytest.mark.db
def test_int_to_float8(schema: str) -> None:
    x = 3
    got = run_with_conn(schema, query[float](t"SELECT {x}::float8").one)
    assert got == 3.0  # ruff: ignore[float-equality-comparison]
    assert isinstance(got, float)


@pytest.mark.db
def test_oid_round_trip(schema: str) -> None:
    x = 4_000_000_000
    assert run_with_conn(schema, query[int](t"SELECT {x}::oid").one) == x


@pytest.mark.db
def test_oid_out_of_range_is_encode_error(schema: str) -> None:
    x = -1
    with pytest.raises(EncodeError, match=r"\$1.*oid"):
        run_with_conn(schema, query[int](t"SELECT {x}::oid").one)


# from codec/test_temporal_decode.py


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("0001-01-01", date(1, 1, 1)),
        ("9999-12-31", date(9999, 12, 31)),
        ("2000-01-01", date(2000, 1, 1)),
        ("2024-02-29", date(2024, 2, 29)),
        ("1999-12-31", date(1999, 12, 31)),
    ],
)
@pytest.mark.db
def test_date(schema: str, literal: str, expected: date) -> None:
    assert fetch_query(schema, query[date](t"SELECT {literal}::text::date")) == expected


@pytest.mark.parametrize("literal", ["infinity", "-infinity", "4713-01-01 BC", "10000-01-01"])
@pytest.mark.db
def test_date_out_of_range_is_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch_query(schema, query[date](t"SELECT {literal}::text::date"))


@pytest.mark.db
def test_time(schema: str) -> None:
    assert fetch_query(schema, query[time_of_day](t"SELECT '13:45:07.123456'::time")) == time_of_day(13, 45, 7, 123456)
    assert fetch_query(schema, query[time_of_day](t"SELECT '00:00'::time")) == time_of_day(0, 0)


@pytest.mark.db
def test_time_24_is_decode_error(schema: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch_query(schema, query[time_of_day](t"SELECT '24:00'::time"))


@pytest.mark.parametrize(
    ("literal", "hours"),
    [("12:00+02", 2), ("12:00-05", -5), ("12:00+00", 0), ("12:00+05:30", 5.5)],
)
@pytest.mark.db
def test_timetz_offset_sign(schema: str, literal: str, hours: float) -> None:
    value = fetch_query(schema, query[time_of_day](t"SELECT {literal}::text::timetz"))
    assert value.replace(tzinfo=None) == time_of_day(12, 0)
    assert value.utcoffset() == timedelta(hours=hours)


@pytest.mark.db
def test_timestamp_is_naive(schema: str) -> None:
    value = fetch_query(schema, query[datetime](t"SELECT '2024-03-05 06:07:08.000009'::timestamp"))
    assert value == datetime(2024, 3, 5, 6, 7, 8, 9)  # ruff: ignore[call-datetime-without-tzinfo]
    assert value.tzinfo is None


@pytest.mark.db
def test_timestamp_before_epoch(schema: str) -> None:
    value = fetch_query(schema, query[datetime](t"SELECT '1999-12-31 23:59:59.999999'::timestamp"))
    assert value == datetime(1999, 12, 31, 23, 59, 59, 999999)  # ruff: ignore[call-datetime-without-tzinfo]


@pytest.mark.db
def test_timestamp_bounds(schema: str) -> None:
    assert fetch_query(schema, query[datetime](t"SELECT '0001-01-01'::timestamp")) == datetime(1, 1, 1)  # ruff: ignore[call-datetime-without-tzinfo]
    last = fetch_query(schema, query[datetime](t"SELECT '9999-12-31 23:59:59.999999'::timestamp"))
    assert last == datetime(9999, 12, 31, 23, 59, 59, 999999)  # ruff: ignore[call-datetime-without-tzinfo]


@pytest.mark.db
def test_timestamptz_is_utc_aware(schema: str) -> None:
    value = fetch_query(schema, query[datetime](t"SELECT '2024-03-05 06:07:08+02'::timestamptz"))
    assert value == datetime(2024, 3, 5, 4, 7, 8, tzinfo=UTC)
    assert value.utcoffset() == timedelta(0)


@pytest.mark.parametrize("literal", ["infinity", "-infinity", "10000-01-01 00:00:00"])
@pytest.mark.db
def test_timestamp_out_of_range_is_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch_query(schema, query[datetime](t"SELECT {literal}::text::timestamp"))


@pytest.mark.parametrize("literal", ["infinity", "-infinity", "10000-01-01 00:00:00+00"])
@pytest.mark.db
def test_timestamptz_out_of_range_is_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch_query(schema, query[datetime](t"SELECT {literal}::text::timestamptz"))


@pytest.mark.db
def test_interval(schema: str) -> None:
    assert fetch_query(schema, query[timedelta](t"SELECT interval '1 day 2 hours'")) == timedelta(days=1, hours=2)
    assert fetch_query(schema, query[timedelta](t"SELECT interval '-3 days 4 microseconds'")) == timedelta(
        days=-3,
        microseconds=4,
    )
    assert fetch_query(schema, query[timedelta](t"SELECT interval '0'")) == timedelta(0)


@pytest.mark.parametrize("literal", ["1 month", "1 year", "infinity", "-infinity"])
@pytest.mark.db
def test_interval_months_or_infinity_is_decode_error(schema: str, literal: str) -> None:
    with pytest.raises(DecodeError, match="cast the column to text"):
        fetch_query(schema, query[timedelta](t"SELECT {literal}::text::interval"))


# from codec/test_temporal_encode.py

EAST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))
WEST: Final[timezone] = timezone(timedelta(hours=-8))


@pytest.mark.parametrize(
    "value",
    [date(1, 1, 1), date(1999, 12, 31), date(2000, 1, 1), date(2024, 2, 29), date(9999, 12, 31)],
)
@pytest.mark.db
def test_date_round_trip(schema: str, value: date) -> None:
    assert fetch_query(schema, query[date](t"SELECT {value}::date")) == value


@pytest.mark.parametrize("value", [time_of_day(0, 0), time_of_day(13, 45, 7, 123456), time_of_day(23, 59, 59, 999999)])
@pytest.mark.db
def test_time_round_trip(schema: str, value: time_of_day) -> None:
    assert fetch_query(schema, query[time_of_day](t"SELECT {value}::time")) == value


@pytest.mark.parametrize(
    "value",
    [
        time_of_day(13, 45, 7, 123456, tzinfo=UTC),
        time_of_day(1, 2, 3, tzinfo=EAST),
        time_of_day(23, 0, tzinfo=WEST),
    ],
)
@pytest.mark.db
def test_timetz_round_trip(schema: str, value: time_of_day) -> None:
    got = fetch_query(schema, query[time_of_day](t"SELECT {value}::timetz"))
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
@pytest.mark.db
def test_timestamp_round_trip(schema: str, value: datetime) -> None:
    assert fetch_query(schema, query[datetime](t"SELECT {value}::timestamp")) == value


@pytest.mark.parametrize(
    "value",
    [
        datetime(2000, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 29, 12, 30, 15, 250000, tzinfo=UTC),
        datetime(2024, 6, 1, 9, 0, tzinfo=EAST),
        datetime(2024, 6, 1, 9, 0, tzinfo=WEST),
    ],
)
@pytest.mark.db
def test_timestamptz_round_trip(schema: str, value: datetime) -> None:
    got = fetch_query(schema, query[datetime](t"SELECT {value}::timestamptz"))
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
@pytest.mark.db
def test_interval_round_trip(schema: str, value: timedelta) -> None:
    assert fetch_query(schema, query[timedelta](t"SELECT {value}::interval")) == value


@pytest.mark.db
def test_naive_datetime_to_timestamptz_is_encode_error(schema: str) -> None:
    value = datetime(2024, 1, 1)  # ruff: ignore[call-datetime-without-tzinfo]
    with pytest.raises(EncodeError, match=r"\$1.*naive"):
        fetch_query(schema, query[datetime](t"SELECT {value}::timestamptz"))


@pytest.mark.db
def test_aware_datetime_to_timestamp_is_encode_error(schema: str) -> None:
    value = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(EncodeError, match=r"\$1.*aware"):
        fetch_query(schema, query[datetime](t"SELECT {value}::timestamp"))


@pytest.mark.db
def test_naive_time_to_timetz_is_encode_error(schema: str) -> None:
    value = time_of_day(1, 2, 3)
    with pytest.raises(EncodeError, match=r"\$1.*naive"):
        fetch_query(schema, query[time_of_day](t"SELECT {value}::timetz"))


@pytest.mark.db
def test_aware_time_to_time_is_encode_error(schema: str) -> None:
    value = time_of_day(1, 2, 3, tzinfo=UTC)
    with pytest.raises(EncodeError, match=r"\$1.*aware"):
        fetch_query(schema, query[time_of_day](t"SELECT {value}::time"))


@pytest.mark.db
def test_datetime_to_date_is_encode_error(schema: str) -> None:
    value = datetime(2024, 1, 1)  # ruff: ignore[call-datetime-without-tzinfo]
    with pytest.raises(EncodeError, match=r"\$1.*datetime"):
        fetch_query(schema, query[date](t"SELECT {value}::date"))


@pytest.mark.db
def test_str_to_date_is_encode_error(schema: str) -> None:
    value = "2024-01-01"
    with pytest.raises(EncodeError, match=r"\$1.*str"):
        fetch_query(schema, query[date](t"SELECT {value}::date"))


# from codec/test_text_like.py


@pytest.mark.parametrize("value", ["", "hello", "héllo wörld ☃ 😀", "a" * 10_000])
@pytest.mark.db
def test_text_round_trip(schema: str, value: str) -> None:
    assert run_with_conn(schema, query[str](t"SELECT {value}::text").one) == value


@pytest.mark.db
def test_nul_str_is_encode_error(schema: str) -> None:
    value = "a\0b"
    with pytest.raises(EncodeError, match=r"\$1.*NUL"):
        run_with_conn(schema, query[str](t"SELECT {value}::text").one)


@pytest.mark.db
def test_bpchar_and_name(schema: str) -> None:
    q = query[tuple[str, str]](t"SELECT 'ab'::char(4), 'nm'::name")
    assert run_with_conn(schema, q.one) == ("ab  ", "nm")


@pytest.mark.db
def test_char_round_trip(schema: str) -> None:
    value = "x"
    assert run_with_conn(schema, query[str](t'SELECT {value}::"char"').one) == value


@pytest.mark.db
def test_char_multi_byte_is_encode_error(schema: str) -> None:
    value = "é"
    with pytest.raises(EncodeError, match=r"\$1.*char"):
        run_with_conn(schema, query[str](t'SELECT {value}::"char"').one)


@pytest.mark.parametrize(
    "value",
    [b"", b"abc", b"a\x00b", bytes(range(256))],
)
@pytest.mark.db
def test_bytea_round_trip(schema: str, value: bytes) -> None:
    assert run_with_conn(schema, query[bytes](t"SELECT {value}::bytea").one) == value


@pytest.mark.db
def test_bytea_accepts_bytes_likes(schema: str) -> None:
    for value in (bytearray(b"x\x00y"), memoryview(b"x\x00y")):
        got = run_with_conn(schema, query[bytes](t"SELECT {value}::bytea").one)
        assert got == b"x\x00y"


@pytest.mark.parametrize("value", [memoryview(std_array("b", [-1, 0, 1])), memoryview(std_array("I", [123]))])
@pytest.mark.db
def test_bytea_takes_raw_bytes_of_any_memoryview_format(schema: str, value: memoryview) -> None:
    got = run_with_conn(schema, query[bytes](t"SELECT {value}::bytea").one)
    assert got == value.tobytes()


@pytest.mark.db
def test_bytes_to_int4_is_encode_error(schema: str) -> None:
    value = b"1"
    with pytest.raises(EncodeError, match=r"\$1.*bytes.*int4"):
        run_with_conn(schema, query[int](t"SELECT {value}::int4").one)


@pytest.mark.db
def test_uuid_round_trip(schema: str) -> None:
    value = UUID("12345678-1234-5678-1234-567812345678")
    got = run_with_conn(schema, query[UUID](t"SELECT {value}::uuid").one)
    assert got == value
    assert isinstance(got, UUID)


@pytest.mark.db
def test_uuid_from_str(schema: str) -> None:
    value = "12345678-1234-5678-1234-567812345678"
    got = run_with_conn(schema, query[UUID](t"SELECT {value}::uuid").one)
    assert got == UUID(value)


@pytest.mark.parametrize("value", [None, 1, "s", [1, "a", None], {"a": {"b": [1.5, True]}}])
@pytest.mark.db
def test_jsonb_round_trip(schema: str, value: Any) -> None:  # ruff: ignore[any-type]
    got = run_with_conn(schema, query[Any](t"SELECT {value}::jsonb").one)
    assert got == value


@pytest.mark.parametrize("value", [1, "s", [1, 2], {"a": 1}])
@pytest.mark.db
def test_json_round_trip(schema: str, value: Any) -> None:  # ruff: ignore[any-type]
    got = run_with_conn(schema, query[Any](t"SELECT {value}::json").one)
    assert got == value


@pytest.mark.db
def test_str_binds_as_json_string(schema: str) -> None:
    value = "[1]"
    q = query[tuple[Any, str]](t"SELECT {value}::jsonb, jsonb_typeof({value}::jsonb)")
    assert run_with_conn(schema, q.one) == ("[1]", "string")


@pytest.mark.db
def test_raw_json_text_via_cast(schema: str) -> None:
    text = "[1]"
    q = query[tuple[Any, str]](t"SELECT {text}::text::jsonb, jsonb_typeof({text}::text::jsonb)")
    assert run_with_conn(schema, q.one) == ([1], "array")


@pytest.mark.db
def test_unserialisable_json_is_encode_error(schema: str) -> None:
    value = {1, 2}
    with pytest.raises(EncodeError, match=r"\$1.*JSON"):
        run_with_conn(schema, query[Any](t"SELECT {value}::jsonb").one)


@pytest.mark.db
def test_enum_round_trip(schema: str) -> None:
    value = "happy"
    got = run_with_conn(schema, query[str](t"SELECT {value}::mood").one)
    assert got == value


@pytest.mark.db
def test_enum_bad_label_is_database_error(schema: str) -> None:
    value = "furious"
    with pytest.raises(DatabaseError):
        run_with_conn(schema, query[str](t"SELECT {value}::mood").one)


@pytest.mark.db
def test_citext_round_trip(schema: str) -> None:
    value = "MiXed"
    q = query[tuple[str, bool]](t"SELECT {value}::public.citext, {value}::public.citext OPERATOR(public.=) 'mixed'")
    assert run_with_conn(schema, q.one) == (value, True)


@pytest.mark.db
def test_domain_column_decodes_as_base_type(schema: str) -> None:
    q = query[int](t"SELECT 5::positive_int")
    assert run_with_conn(schema, q.one) == 5


@pytest.mark.db
def test_domain_param_encodes(schema: str) -> None:
    value = 7
    q = query[int](t"SELECT ({value}::positive_int)::int")
    assert run_with_conn(schema, q.one) == 7


# from codec/test_params_basic.py


@pytest.mark.db
def test_int_round_trip(schema: str) -> None:
    x = 7
    assert run_with_conn(schema, query[int](t"SELECT {x}::int").one) == 7


@pytest.mark.db
def test_int8_extremes(schema: str) -> None:
    lo, hi = -(2**63), 2**63 - 1
    q = query[tuple[int, int]](t"SELECT {lo}::int8, {hi}::int8")
    assert run_with_conn(schema, q.one) == (lo, hi)


@pytest.mark.db
def test_bool_and_text_round_trip(schema: str) -> None:
    flag, word = True, "héllo ✓"
    q = query[tuple[bool, str]](t"SELECT {flag}::bool, {word}::text")
    assert run_with_conn(schema, q.one) == (flag, word)


@pytest.mark.db
def test_none_is_null(schema: str) -> None:
    missing = None
    q = query[int | None](t"SELECT {missing}::int")
    assert run_with_conn(schema, q.one) is None


@pytest.mark.db
def test_two_interpolations_bind_two_parameters(schema: str) -> None:
    a, b = 1, "b"
    q = query[tuple[int, str]](t"SELECT {a}::int, {b}::text")
    assert run_with_conn(schema, q.one) == (1, "b")


@pytest.mark.db
def test_execute_with_params(schema: str) -> None:
    n = 3
    assert run_with_conn(schema, query(t"SELECT {n}::int").execute).rows_affected == 1


@pytest.mark.db
def test_int2_out_of_range_names_the_parameter(schema: str) -> None:
    x = 40000
    with pytest.raises(EncodeError, match=r"\$1.*int2"):
        run_with_conn(schema, query[int](t"SELECT {x}::int2").one)


@pytest.mark.db
def test_int8_overflow_is_encode_error(schema: str) -> None:
    x = 2**63
    with pytest.raises(EncodeError, match=r"\$1"):
        run_with_conn(schema, query[int](t"SELECT {x}::int8").one)


@pytest.mark.db
def test_bool_is_not_an_int(schema: str) -> None:
    x = True
    with pytest.raises(EncodeError, match=r"\$1.*bool.*int4"):
        run_with_conn(schema, query[int](t"SELECT {x}::int4").one)


@pytest.mark.db
def test_int_is_not_text(schema: str) -> None:
    x = 42
    with pytest.raises(EncodeError, match=r"\$1.*int.*text"):
        run_with_conn(schema, query[str](t"SELECT {x}::text").one)


@pytest.mark.db
def test_nul_in_text_is_encode_error(schema: str) -> None:
    x = "a\0b"
    with pytest.raises(EncodeError, match=r"\$1"):
        run_with_conn(schema, query[str](t"SELECT {x}::text").one)


@pytest.mark.db
def test_interpolation_inside_literal_is_count_mismatch(schema: str) -> None:
    x = 1
    with pytest.raises(InterfaceError, match="parameters"):
        run_with_conn(schema, query[str](t"SELECT '{x}'").one)


@pytest.mark.db
def test_connection_usable_after_encode_error(schema: str) -> None:
    async def body(conn: Connection) -> int:
        x = 40000
        with pytest.raises(EncodeError):
            await query[int](t"SELECT {x}::int2").one(conn)
        return await query[int](t"SELECT 1").one(conn)

    assert run_with_conn(schema, body) == 1


# from codec/test_table_coverage.py

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
    "hstore": ("public.hstore", {"a": "1", "b": None, "": "", "é": "ü"}),
    "ltree": ("public.ltree", "A.b_2.c"),
    "lquery": ("public.lquery", "A.*{1,2}.c|d@"),
    "cube": ("public.cube", Cube((-1.5, 2.0), (3.0, 1e10))),
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


def fetch_scalar_rows(dsn: str, sql: str, params: list[object] | None = None) -> list[object]:
    async def run() -> list[object]:
        conn = await connect(dsn)
        try:
            return await conn._impl._fetch(sql, params or [], None, scalar=True, limit_one=True, arity=1, shape=None)
        finally:
            await conn.close()

    return asyncio.run(run())


@pytest.mark.db
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
@pytest.mark.db
def test_decode_golden(schema: str, key: str) -> None:
    sql_type, literal, expected = DECODE_GOLDENS[key]
    sql = f"SELECT '{literal}'::{sql_type}" if key != "void" else "SELECT pg_sleep(0)"
    assert fetch_scalar_rows(schema, sql) == [expected]


@pytest.mark.parametrize("key", sorted(ENCODE_GOLDENS))
@pytest.mark.db
def test_encode_golden(schema: str, key: str) -> None:
    sql_type, value = ENCODE_GOLDENS[key]
    assert fetch_scalar_rows(schema, f"SELECT $1::{sql_type}", [value]) == [value]


@pytest.mark.parametrize("sql_type", sorted(UNSUPPORTED_DECODE))
@pytest.mark.db
def test_unsupported_type_is_decode_error(schema: str, sql_type: str) -> None:
    with pytest.raises(DecodeError, match=sql_type):
        fetch_scalar_rows(schema, f"SELECT '{UNSUPPORTED_DECODE[sql_type]}'::{sql_type}")


@pytest.mark.db
def test_aclitem_has_no_binary_send(schema: str) -> None:
    with pytest.raises(DatabaseError) as info:
        # built from role oids (PUBLIC by the bootstrap superuser), so no role name has to exist
        fetch_scalar_rows(schema, "SELECT makeaclitem(0, 10, 'SELECT', false)")
    assert info.value.sqlstate == "42883"
