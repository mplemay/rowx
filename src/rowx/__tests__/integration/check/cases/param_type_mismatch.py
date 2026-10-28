import datetime as dt
import ipaddress
from decimal import Decimal

from rowx import query
from rowx.postgres import Box, Circle, Cube, Interval, Line, LineSegment, Money, Path, Point, Polygon, Range

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


def run_money_bit_interval() -> None:
    query[Money](t'SELECT {Money(100)}::money AS "m!"')
    query[Money](t'SELECT {1}::money AS "m!"')  # error: [param-type-mismatch]
    query[Money](t'SELECT {"1.00"}::money AS "m!"')  # error: [param-type-mismatch]
    query[Money](t'SELECT {Decimal("1.00")}::money AS "m!"')  # error: [param-type-mismatch]
    query[Money](t'SELECT {1.5}::money AS "m!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {Money(100)}::int AS "n!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {Money(100)}::text AS "n!"')  # error: [param-type-mismatch]
    query[Interval](t'SELECT {Money(100)}::interval AS "i!"')  # error: [param-type-mismatch]
    query[list[Money]](t'SELECT {[Money(1)]}::money[] AS "m!"')
    query[list[Money]](t'SELECT {[1]}::money[] AS "m!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {"101"}::varbit AS "b!"')
    query[str](t'SELECT {"101"}::bit(3) AS "b!"')
    query[str](t'SELECT {101}::varbit AS "b!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {b"101"}::bit(3) AS "b!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {Money(1)}::varbit AS "b!"')  # error: [param-type-mismatch]
    query[list[str]](t'SELECT {["101"]}::varbit[] AS "b!"')
    query[list[str]](t'SELECT {[101]}::varbit[] AS "b!"')  # error: [param-type-mismatch]
    query[Interval](t'SELECT {Interval(months=1)}::interval AS "i!"')
    query[dt.timedelta](t'SELECT {dt.timedelta(days=1)}::interval AS "i!"')
    query[Interval](t'SELECT {1}::interval AS "i!"')  # error: [param-type-mismatch]
    query[Interval](t'SELECT {"1 mon"}::interval AS "i!"')  # error: [param-type-mismatch]
    query[Interval](t'SELECT {dt.date(2024, 1, 1)}::interval AS "i!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {Interval(months=1)}::int AS "n!"')  # error: [param-type-mismatch]
    query[list[Interval]](t'SELECT {[Interval(days=1)]}::interval[] AS "i!"')
    query[list[Interval]](t'SELECT {[1]}::interval[] AS "i!"')  # error: [param-type-mismatch]


def run_geometric() -> None:
    query[Point](t'SELECT {Point(1.0, 2.0)}::point AS "p!"')
    query[Line](t'SELECT {Line(1.0, 2.0, 3.0)}::line AS "l!"')
    query[LineSegment](t'SELECT {LineSegment(Point(0.0, 0.0), Point(1.0, 1.0))}::lseg AS "l!"')
    query[Box](t'SELECT {Box(Point(1.0, 1.0), Point(0.0, 0.0))}::box AS "b!"')
    query[Path](t'SELECT {Path((Point(0.0, 0.0),), closed=False)}::path AS "p!"')
    query[Polygon](t'SELECT {Polygon((Point(0.0, 0.0),))}::polygon AS "p!"')
    query[Circle](t'SELECT {Circle(0.0, 0.0, 1.0)}::circle AS "c!"')
    query[Point](t'SELECT {(1.0, 2.0)}::point AS "p!"')  # error: [param-type-mismatch]
    query[Point](t'SELECT {"(1,2)"}::point AS "p!"')  # error: [param-type-mismatch]
    query[Point](t'SELECT {1.5}::point AS "p!"')  # error: [param-type-mismatch]
    query[Point](t'SELECT {Circle(0.0, 0.0, 1.0)}::point AS "p!"')  # error: [param-type-mismatch]
    query[Polygon](t'SELECT {Path((Point(0.0, 0.0),), closed=True)}::polygon AS "p!"')  # error: [param-type-mismatch]
    query[Box](t'SELECT {LineSegment(Point(0.0, 0.0), Point(1.0, 1.0))}::box AS "b!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {Point(1.0, 2.0)}::text AS "p!"')  # error: [param-type-mismatch]
    query[Money](t'SELECT {Point(1.0, 2.0)}::money AS "p!"')  # error: [param-type-mismatch]
    query[list[Point]](t'SELECT {[Point(1.0, 2.0)]}::point[] AS "p!"')
    query[list[Point]](t'SELECT {[1.0]}::point[] AS "p!"')  # error: [param-type-mismatch]


def run_extensions(tags: dict[str, str | None]) -> None:
    query[dict[str, str | None]](t'SELECT {tags}::public.hstore AS "h!"')
    query[Cube](t'SELECT {Cube((1.0,), (2.0,))}::public.cube AS "c!"')
    query[str](t'SELECT {"a.b"}::public.ltree AS "l!"')
    query[str](t'SELECT {"a.*"}::public.lquery AS "l!"')
    query[list[str | None]](t'SELECT {["a", "b"]}::public.citext[] AS "c!"')
    query[dict[str, str | None]](t'SELECT {"a=>1"}::public.hstore AS "h!"')  # error: [param-type-mismatch]
    query[dict[str, str | None]](t'SELECT {[1]}::public.hstore AS "h!"')  # error: [param-type-mismatch]
    query[Cube](t'SELECT {(1.0, 2.0)}::public.cube AS "c!"')  # error: [param-type-mismatch]
    query[Cube](t'SELECT {"(1,2)"}::public.cube AS "c!"')  # error: [param-type-mismatch]
    query[Cube](t'SELECT {Point(1.0, 2.0)}::public.cube AS "c!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {Cube((1.0,), (2.0,))}::public.ltree AS "l!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {tags}::public.ltree AS "l!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {1}::public.lquery AS "l!"')  # error: [param-type-mismatch]
    query[list[Cube | None]](t'SELECT {[Cube((1.0,), (2.0,))]}::public.cube[] AS "c!"')
    query[list[Cube | None]](t'SELECT {[1.0]}::public.cube[] AS "c!"')  # error: [param-type-mismatch]
