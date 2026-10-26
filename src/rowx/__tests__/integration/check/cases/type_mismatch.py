import datetime as dt
import ipaddress
import uuid
from decimal import Decimal

from rowx import query

type Inet = ipaddress.IPv4Interface | ipaddress.IPv6Interface
type Cidr = ipaddress.IPv4Network | ipaddress.IPv6Network


def run() -> None:
    query[str](t'SELECT 1 AS "n!"')  # error: [type-mismatch]
    query[bool](t'SELECT 1 AS "n!"')  # error: [type-mismatch]
    query[int](t'SELECT true AS "b!"')  # error: [type-mismatch]
    query[float](t'SELECT 1.5::numeric AS "n!"')  # error: [type-mismatch]
    query[Decimal](t'SELECT 1.5::numeric AS "n!"')
    query[dt.date](t'SELECT now() AS "n!"')  # error: [type-mismatch]
    query[uuid.UUID](t'SELECT gen_random_uuid() AS "u!"')
    query[list[str]](t'SELECT ARRAY[1, 2] AS "a!"')  # error: [type-mismatch]
    query[list[int | None]](t'SELECT ARRAY[1, 2] AS "a!"')
    query[int](t'SELECT c_json AS "j!" FROM all_types WHERE c_json IS NOT NULL')
    query[tuple[None]](t'SELECT 1 AS "n!"')  # error: [type-mismatch]
    query[str](t'''SELECT '10.0.0.1/24'::inet AS "n!"''')  # error: [type-mismatch]
    query[Cidr](t'''SELECT '10.0.0.1/24'::inet AS "n!"''')  # error: [type-mismatch]
    query[Inet](t'''SELECT '10.0.0.0/8'::cidr AS "n!"''')  # error: [type-mismatch]
    query[Inet](t'''SELECT '10.0.0.1/24'::inet AS "n!"''')
    query[Cidr](t'''SELECT '10.0.0.0/8'::cidr AS "n!"''')
    query[Inet](t'''SELECT '08:00:2b:01:02:03'::macaddr AS "n!"''')  # error: [type-mismatch]
    query[int](t'''SELECT '08:00:2b:01:02:03:04:05'::macaddr8 AS "n!"''')  # error: [type-mismatch]
    query[str](t'''SELECT '08:00:2b:01:02:03'::macaddr AS "n!"''')
    query[list[Inet | None]](t'''SELECT ARRAY['10.0.0.1/24'::inet] AS "n!"''')
    query[list[Cidr]](t'''SELECT ARRAY['10.0.0.1/24'::inet] AS "n!"''')  # error: [type-mismatch]
