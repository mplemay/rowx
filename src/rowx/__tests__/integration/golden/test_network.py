import asyncio
import ipaddress
from typing import TYPE_CHECKING, Any

import pytest

from rowx import query
from rowx.errors import DatabaseError, EncodeError
from rowx.postgres import Connection, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


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
def test_inet_decodes_to_an_interface(schema: str, literal: str, expected: object) -> None:
    got = read(schema, f"SELECT '{literal}'::inet")
    assert type(got) is type(expected)
    assert got == expected
    assert got.network.prefixlen == expected.network.prefixlen  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize("literal", ["10.0.0.0/8", "0.0.0.0/0", "2001:db8::/32", "::/0", "1.2.3.4/32"])
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
def test_inet_accepts_addresses_and_strings(schema: str, value: object, expected: object) -> None:
    assert read(schema, "SELECT $1::inet", [value]) == expected


@pytest.mark.parametrize(
    "value",
    [ipaddress.IPv4Network("10.0.0.0/8"), ipaddress.IPv6Network("2001:db8::/32"), ipaddress.IPv4Network("0.0.0.0/0")],
)
def test_cidr_round_trips(schema: str, value: object) -> None:
    assert read(schema, "SELECT $1::cidr", [value]) == value


def test_cidr_accepts_a_string(schema: str) -> None:
    assert read(schema, "SELECT $1::cidr", ["10.0.0.0/8"]) == ipaddress.IPv4Network("10.0.0.0/8")


@pytest.mark.parametrize("value", ["10.0.0.1/8", "2001:db8::1/32"])
def test_cidr_with_host_bits_is_an_encode_error(schema: str, value: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*host bits"):
        read(schema, "SELECT $1::cidr", [value])


@pytest.mark.parametrize("value", ["not an address", "1.2.3.4/33", "", "fe80::1%eth0"])
def test_inet_rejects_unparseable_strings_and_zone_ids(schema: str, value: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, "SELECT $1::inet", [value])


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
def test_mac_decodes_to_lower_case_colons(schema: str, sql_type: str, literal: str, expected: str) -> None:
    assert read(schema, f"SELECT '{literal}'::{sql_type}") == expected


@pytest.mark.parametrize(
    ("sql_type", "value", "expected"),
    [
        ("macaddr", "08:00:2b:01:02:03", "08:00:2b:01:02:03"),
        ("macaddr", "08-00-2B-01-02-03", "08:00:2b:01:02:03"),
        ("macaddr", "08002b010203", "08:00:2b:01:02:03"),
        # the server parses the text, so its own spellings work too
        ("macaddr", "0800.2b01.0203", "08:00:2b:01:02:03"),
        ("macaddr8", "0800.2b01.0203.0405", "08:00:2b:01:02:03:04:05"),
        ("macaddr8", "08:00:2b:01:02:03:04:05", "08:00:2b:01:02:03:04:05"),
        ("macaddr8", "08:00:2b:01:02:03", "08:00:2b:ff:fe:01:02:03"),
    ],
)
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
def test_bad_mac_is_rejected_by_the_server(schema: str, sql_type: str, value: str) -> None:
    with pytest.raises(DatabaseError) as info:
        read(schema, f"SELECT $1::{sql_type}", [value])
    assert info.value.sqlstate == "22P02"


def test_mac_with_a_nul_is_an_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*NUL"):
        read(schema, "SELECT $1::macaddr", ["08:00:2b:01:02:03\0"])


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


def test_mac_arrays_round_trip(schema: str) -> None:
    assert read(schema, "SELECT $1::macaddr[]", [["08:00:2b:01:02:03", None]]) == ["08:00:2b:01:02:03", None]
    assert read(schema, "SELECT $1::macaddr8[]", [[["08:00:2b:01:02:03:04:05"], [None]]]) == [
        ["08:00:2b:01:02:03:04:05"],
        [None],
    ]


def test_array_element_error_names_the_element(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*element 1"):
        read(schema, "SELECT $1::cidr[]", [["10.0.0.0/8", "10.0.0.1/8"]])


def test_query_annotated_with_the_pair_decodes(schema: str) -> None:
    pair = ipaddress.IPv4Interface | ipaddress.IPv6Interface
    q = query[tuple[pair, pair | None, str]](t"SELECT '10.0.0.1/24'::inet, NULL::inet, '08:00:2b:01:02:03'::macaddr")
    assert run_with_conn(schema, q.one) == (ipaddress.IPv4Interface("10.0.0.1/24"), None, "08:00:2b:01:02:03")
