import datetime as dt
import ipaddress

from rowx import query
from rowx.postgres import Circle, Interval, Money, Path, Point, Polygon, Range


def run() -> None:
    query[dict](t"SELECT 1")  # error: [invalid-row-type]  # ty: ignore[missing-type-argument]
    query[tuple[()]](t"SELECT 1")  # error: [invalid-row-type]
    query[ipaddress.IPv4Interface](t"SELECT 1")  # error: [invalid-row-type]
    query[ipaddress.IPv4Interface | ipaddress.IPv6Network](t"SELECT 1")  # error: [invalid-row-type]
    query[ipaddress.IPv4Interface | ipaddress.IPv6Interface | int](t"SELECT 1")  # error: [invalid-row-type]
    query[Range](t"SELECT 1")  # error: [invalid-row-type]  # ty: ignore[missing-type-argument]
    query[Range[str]](t"SELECT 1")  # error: [invalid-row-type]
    query[Range[float]](t"SELECT 1")  # error: [invalid-row-type]
    query[Range[int | None]](t"SELECT 1")  # error: [invalid-row-type]
    query[Interval | Money](t"SELECT 1")  # error: [invalid-row-type]
    query[Interval | dt.timedelta](t"SELECT 1")  # error: [invalid-row-type]
    query[tuple[Interval | Money, int]](t"SELECT 1, 2")  # error: [invalid-row-type]
    query[Point | Circle](t"SELECT 1")  # error: [invalid-row-type]
    query[Point | dt.date](t"SELECT 1")  # error: [invalid-row-type]
    query[tuple[Path | Polygon, int]](t"SELECT 1, 2")  # error: [invalid-row-type]
