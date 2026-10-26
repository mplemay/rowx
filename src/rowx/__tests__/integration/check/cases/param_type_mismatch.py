import ipaddress
from decimal import Decimal

from rowx import query
from rowx.postgres import Range

type Inet = ipaddress.IPv4Interface | ipaddress.IPv6Interface
type Cidr = ipaddress.IPv4Network | ipaddress.IPv6Network


def run() -> None:
    query[int](t'SELECT {"a"}::int AS "n!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {42}::text AS "s!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {True}::int AS "n!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {1}::int + {"b"}::int AS "n!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {[1, 2]}::int AS "n!"')  # error: [param-type-mismatch]
    query[int](t'SELECT count(*)::int AS "n!" WHERE 1 = ANY({["a", "b"]}::int[])')  # error: [param-type-mismatch]
    query[Inet](t'SELECT {1}::inet AS "n!"')  # error: [param-type-mismatch]
    query[Inet](t'SELECT {ipaddress.IPv4Network("10.0.0.0/8")}::inet AS "n!"')  # error: [param-type-mismatch]
    query[Cidr](t'SELECT {ipaddress.IPv4Interface("10.0.0.1/8")}::cidr AS "n!"')  # error: [param-type-mismatch]
    query[Cidr](t'SELECT {ipaddress.IPv4Address("10.0.0.1")}::cidr AS "n!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {ipaddress.IPv4Address("10.0.0.1")}::macaddr AS "n!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {1}::macaddr8 AS "n!"')  # error: [param-type-mismatch]
    query[list[Inet]](t'SELECT {[ipaddress.IPv4Network("10.0.0.0/8")]}::inet[] AS "n!"')  # error: [param-type-mismatch]
    query[Inet](t'SELECT {ipaddress.IPv4Interface("10.0.0.1/8")}::inet AS "n!"')
    query[Inet](t'SELECT {ipaddress.IPv6Address("::1")}::inet AS "n!"')
    query[Inet](t'SELECT {"10.0.0.1"}::inet AS "n!"')
    query[Cidr](t'SELECT {ipaddress.IPv4Network("10.0.0.0/8")}::cidr AS "n!"')
    query[str](t'SELECT {"08:00:2b:01:02:03"}::macaddr AS "n!"')
    query[Range[int]](t'SELECT {Range(1, 5)}::int4range AS "r!"')
    query[Range[Decimal]](t'SELECT {Range(1, 5)}::numrange AS "r!"')
    query[Range[int]](t'SELECT {Range(1, 5)}::rowx_intrange AS "r!"')
    query[Range[int]](t'SELECT {1}::int4range AS "r!"')  # error: [param-type-mismatch]
    query[Range[int]](t'SELECT {"[1,5)"}::int4range AS "r!"')  # error: [param-type-mismatch]
    query[Range[int]](t'SELECT {(1, 5)}::int4range AS "r!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {Range(1, 5)}::int AS "n!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {Range(1, 5)}::text AS "n!"')  # error: [param-type-mismatch]
    query[list[Range[int]]](t'SELECT {[Range(1, 5)]}::int4range[] AS "r!"')
    query[list[Range[int]]](t'SELECT {[1]}::int4range[] AS "r!"')  # error: [param-type-mismatch]
