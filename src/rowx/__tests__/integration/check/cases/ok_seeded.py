from datetime import date, datetime, timedelta
from decimal import Decimal
from ipaddress import IPv4Interface, IPv4Network, IPv6Interface, IPv6Network

from rowx import query
from rowx.postgres import Box, Circle, Cube, Interval, Line, LineSegment, Money, Path, Point, Polygon, Range


def run() -> None:
    query[tuple[int, str, str | None]](t"SELECT id, name, email FROM users")
    query[tuple[int, str]](t"SELECT id, name FROM users")
    query[tuple[int, str | None]](t"SELECT u.id, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id")
    query[tuple[str, str | None]](t"SELECT u.name, o.note FROM users u JOIN orders o ON o.user_id = u.id")
    query[tuple[str, int]](t'SELECT name, count(*) AS "n!" FROM users GROUP BY name')
    query[tuple[int, int | None, bool | None]](t"SELECT id, c_int4, c_bool FROM all_types")
    query[tuple[int, str | None, list[int | None] | None]](t"SELECT id, c_text, c_int4_array FROM all_types")
    query[tuple[int | None, str | None]](t"SELECT user_id, title FROM user_posts")
    query[tuple[int, int | None]](t"SELECT id, x FROM not_valid")
    query[tuple[str, int]](t'SELECT coalesce(email, \'none\') AS "e!", 1 AS "n!" FROM users')
    query[str](t'SELECT c_text AS "t!" FROM all_types WHERE c_text IS NOT NULL')
    query(t"SELECT pg_sleep(0)")
    query[tuple[IPv4Interface | IPv6Interface | None, IPv4Network | IPv6Network | None, str | None, str | None]](
        t"SELECT c_inet, c_cidr, c_macaddr, c_macaddr8 FROM all_types"
    )
    query[tuple[Range[int] | None, Range[int] | None, Range[Decimal] | None, Range[date] | None]](
        t"SELECT c_int4range, c_int8range, c_numrange, c_daterange FROM all_types"
    )
    query[tuple[Range[datetime] | None, Range[datetime] | None]](t"SELECT c_tsrange, c_tstzrange FROM all_types")
    query[
        tuple[list[Range[int] | None] | None, list[Range[Decimal] | None] | None, list[Range[datetime] | None] | None]
    ](t"SELECT c_int4range_array, c_numrange_array, c_tstzrange_array FROM all_types")
    query[tuple[Range[int] | None, list[Range[int] | None] | None]](
        t"SELECT c_custom_range, c_custom_range_array FROM all_types"
    )
    query[tuple[Money | None, str | None, str | None]](t"SELECT c_money, c_bit, c_varbit FROM all_types")
    query[tuple[list[Money | None] | None, list[str | None] | None, list[str | None] | None]](
        t"SELECT c_money_array, c_bit_array, c_varbit_array FROM all_types"
    )
    query[tuple[timedelta | None, Interval | None]](t"SELECT c_interval, c_interval FROM all_types")
    query[tuple[list[timedelta | None] | None, list[Interval | None] | None]](
        t"SELECT c_interval_array, c_interval_array FROM all_types"
    )
    query[tuple[Point | None, Line | None, LineSegment | None, Box | None]](
        t"SELECT c_point, c_line, c_lseg, c_box FROM all_types"
    )
    query[tuple[Path | None, Polygon | None, Circle | None]](t"SELECT c_path, c_polygon, c_circle FROM all_types")
    query[tuple[list[Point | None] | None, list[Box | None] | None, list[Polygon | None] | None]](
        t"SELECT c_point_array, c_box_array, c_polygon_array FROM all_types"
    )
    query[tuple[dict[str, str | None] | None, str | None, str | None, Cube | None]](
        t"SELECT c_hstore, c_ltree, c_lquery, c_cube FROM all_types"
    )
    query[
        tuple[
            list[str | None] | None,
            list[dict[str, str | None] | None] | None,
            list[str | None] | None,
            list[str | None] | None,
            list[Cube | None] | None,
        ]
    ](t"SELECT c_citext_array, c_hstore_array, c_ltree_array, c_lquery_array, c_cube_array FROM all_types")
