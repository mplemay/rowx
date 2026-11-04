import struct
from contextlib import suppress
from random import Random
from typing import Final

import pytest

from rowx._core.postgres import TypeInfo, _decode, type_table
from rowx.errors import DecodeError

pytestmark = pytest.mark.integration

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
