import datetime as dt
import ipaddress
import uuid
from decimal import Decimal

from rowx import query
from rowx.postgres import Box, Circle, Cube, Interval, Line, LineSegment, Money, Path, Point, Polygon, Range

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
    query[Range[int]](t'''SELECT '[1,5)'::int4range AS "r!"''')
    query[Range[int]](t'''SELECT '[1,5)'::int8range AS "r!"''')
    query[Range[int] | None](t'''SELECT '[1,5)'::int4range AS "r?"''')
    query[Range[Decimal]](t'''SELECT '[1,5)'::numrange AS "r!"''')
    query[Range[dt.date]](t'''SELECT '[2024-01-01,2024-02-01)'::daterange AS "r!"''')
    query[Range[dt.datetime]](t'''SELECT '[2024-01-01,2024-02-01)'::tsrange AS "r!"''')
    query[Range[dt.datetime]](t'''SELECT '[2024-01-01,2024-02-01)'::tstzrange AS "r!"''')
    query[Range[Decimal]](t'''SELECT '[1,5)'::int4range AS "r!"''')  # error: [type-mismatch]
    query[Range[int]](t'''SELECT '[1,5)'::numrange AS "r!"''')  # error: [type-mismatch]
    query[Range[dt.datetime]](t'''SELECT '[2024-01-01,2024-02-01)'::daterange AS "r!"''')  # error: [type-mismatch]
    query[Range[dt.date]](t'''SELECT '[2024-01-01,2024-02-01)'::tsrange AS "r!"''')  # error: [type-mismatch]
    query[tuple[int, int]](t'''SELECT '[1,5)'::int4range AS "r!", 1 AS "n!"''')  # error: [type-mismatch]
    query[str](t'''SELECT '[1,5)'::int4range AS "r!"''')  # error: [type-mismatch]
    query[Range[int]](t'''SELECT 1 AS "n!"''')  # error: [type-mismatch]
    query[list[Range[int] | None]](t'''SELECT ARRAY['[1,5)'::int4range] AS "r!"''')
    query[list[Range[Decimal]]](t'''SELECT ARRAY['[1,5)'::int4range] AS "r!"''')  # error: [type-mismatch]
    query[Range[int]](t'''SELECT '[1,5]'::rowx_intrange AS "r!"''')
    query[Range[Decimal]](t'''SELECT '[1,5]'::rowx_intrange AS "r!"''')  # error: [type-mismatch]


def run_money_bit_interval() -> None:
    query[Money](t'''SELECT 1::money AS "m!"''')
    query[Money | None](t'''SELECT 1::money AS "m?"''')
    query[int](t'''SELECT 1::money AS "m!"''')  # error: [type-mismatch]
    query[Decimal](t'''SELECT 1::money AS "m!"''')  # error: [type-mismatch]
    query[str](t'''SELECT 1::money AS "m!"''')  # error: [type-mismatch]
    query[Money](t'''SELECT 1 AS "n!"''')  # error: [type-mismatch]
    query[Money](t'''SELECT '1 day'::interval AS "i!"''')  # error: [type-mismatch]
    query[list[Money | None]](t'''SELECT ARRAY[1::money] AS "m!"''')
    query[list[Decimal]](t'''SELECT ARRAY[1::money] AS "m!"''')  # error: [type-mismatch]
    query[str](t'''SELECT B'101'::bit(3) AS "b!"''')
    query[str](t'''SELECT B'101'::varbit AS "b!"''')
    query[int](t'''SELECT B'101'::bit(3) AS "b!"''')  # error: [type-mismatch]
    query[bytes](t'''SELECT B'101'::varbit AS "b!"''')  # error: [type-mismatch]
    query[Money](t'''SELECT B'101'::varbit AS "b!"''')  # error: [type-mismatch]
    query[list[str | None]](t'''SELECT ARRAY[B'101'::bit(3)] AS "b!"''')
    query[list[int]](t'''SELECT ARRAY[B'101'::varbit] AS "b!"''')  # error: [type-mismatch]
    query[dt.timedelta](t'''SELECT '1 day'::interval AS "i!"''')
    query[Interval](t'''SELECT '1 mon'::interval AS "i!"''')
    query[Interval | None](t'''SELECT '1 mon'::interval AS "i?"''')
    query[tuple[Interval, dt.timedelta | None]](t'''SELECT '1 mon'::interval AS "i!", NULL::interval AS "d?"''')
    query[list[Interval | None]](t'''SELECT ARRAY['1 mon'::interval] AS "i!"''')
    query[list[dt.timedelta]](t'''SELECT ARRAY['1 day'::interval] AS "i!"''')
    query[Interval](t'''SELECT now() AS "n!"''')  # error: [type-mismatch]
    query[Interval](t'''SELECT 1 AS "n!"''')  # error: [type-mismatch]
    query[Interval](t'''SELECT 1::money AS "m!"''')  # error: [type-mismatch]
    query[Interval](t'''SELECT now()::time AS "t!"''')  # error: [type-mismatch]
    query[dt.timedelta](t'''SELECT now()::time AS "t!"''')  # error: [type-mismatch]
    query[list[Interval]](t'''SELECT ARRAY[1] AS "n!"''')  # error: [type-mismatch]


def run_geometric() -> None:
    query[Point](t'''SELECT '(1,2)'::point AS "p!"''')
    query[Point | None](t'''SELECT '(1,2)'::point AS "p?"''')
    query[Line](t'''SELECT '[(0,0),(1,1)]'::line AS "l!"''')
    query[LineSegment](t'''SELECT '[(0,0),(1,1)]'::lseg AS "l!"''')
    query[Box](t'''SELECT '(0,0),(1,1)'::box AS "b!"''')
    query[Path](t'''SELECT '[(0,0),(1,1)]'::path AS "p!"''')
    query[Polygon](t'''SELECT '((0,0),(1,1))'::polygon AS "p!"''')
    query[Circle](t'''SELECT '<(0,0),1>'::circle AS "c!"''')
    query[Box](t'''SELECT '(1,2)'::point AS "p!"''')  # error: [type-mismatch]
    query[Point](t'''SELECT '<(0,0),1>'::circle AS "c!"''')  # error: [type-mismatch]
    query[Polygon](t'''SELECT '[(0,0),(1,1)]'::path AS "p!"''')  # error: [type-mismatch]
    query[Path](t'''SELECT '((0,0),(1,1))'::polygon AS "p!"''')  # error: [type-mismatch]
    query[Line](t'''SELECT '[(0,0),(1,1)]'::lseg AS "l!"''')  # error: [type-mismatch]
    query[str](t'''SELECT '(1,2)'::point AS "p!"''')  # error: [type-mismatch]
    query[float](t'''SELECT '(1,2)'::point AS "p!"''')  # error: [type-mismatch]
    query[Point](t'''SELECT 1 AS "n!"''')  # error: [type-mismatch]
    query[Point](t'''SELECT 1::money AS "m!"''')  # error: [type-mismatch]
    query[Money](t'''SELECT '(1,2)'::point AS "p!"''')  # error: [type-mismatch]
    query[list[Point | None]](t'''SELECT ARRAY['(1,2)'::point] AS "p!"''')
    query[list[Box]](t'''SELECT ARRAY['(1,2)'::point] AS "p!"''')  # error: [type-mismatch]


def run_extensions() -> None:
    query[dict[str, str | None]](t'''SELECT 'a=>1'::public.hstore AS "h!"''')
    query[dict[str, str | None] | None](t'''SELECT 'a=>1'::public.hstore AS "h?"''')
    query[str](t'''SELECT 'a.b'::public.ltree AS "l!"''')
    query[str](t'''SELECT 'a.*'::public.lquery AS "l!"''')
    query[Cube](t'''SELECT '(1,2)'::public.cube AS "c!"''')
    query[str](t'''SELECT 'MiXed'::public.citext AS "c!"''')
    query[list[str | None]](t'''SELECT ARRAY['a']::public.citext[] AS "c!"''')
    query[list[Cube | None]](t'''SELECT ARRAY['(1)']::public.cube[] AS "c!"''')
    query[str](t'''SELECT 'a=>1'::public.hstore AS "h!"''')  # error: [type-mismatch]
    query[dict[str, str]](t'''SELECT 'a=>1'::public.hstore AS "h!"''')  # error: [invalid-row-type]
    query[dict[str, int]](t'''SELECT 'a=>1'::public.hstore AS "h!"''')  # error: [invalid-row-type]
    query[Cube](t'''SELECT 'a.b'::public.ltree AS "l!"''')  # error: [type-mismatch]
    query[Cube](t'''SELECT '(1,2)'::point AS "p!"''')  # error: [type-mismatch]
    query[Point](t'''SELECT '(1,2)'::public.cube AS "c!"''')  # error: [type-mismatch]
    query[list[str]](t'''SELECT 'a=>1'::public.hstore AS "h!"''')  # error: [type-mismatch]
    query[dict[str, str | None]](t'''SELECT 'a.b'::public.ltree AS "l!"''')  # error: [type-mismatch]
    query[int](t'''SELECT 'a.*'::public.lquery AS "l!"''')  # error: [type-mismatch]
