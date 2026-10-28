import asyncio
import math
from typing import TYPE_CHECKING, Any, Final

import pytest

from rowx import query
from rowx.errors import DatabaseError, EncodeError
from rowx.postgres import Box, Circle, Connection, Line, LineSegment, Path, Point, Polygon, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

INVALID_BINARY_REPRESENTATION: Final[str] = "22P03"

ORIGIN: Final[Point] = Point(0.0, 0.0)
UNIT: Final[Point] = Point(1.0, 1.0)

# key -> a value of that type, for the round-trip and array tests
VALUES: Final[dict[str, Any]] = {
    "point": Point(1.5, -2.0),
    "line": Line(1.0, -2.0, 3.0),
    "lseg": LineSegment(ORIGIN, Point(1.0, 2.0)),
    "box": Box(Point(2.0, 3.0), Point(-1.0, 0.0)),
    "path": Path((ORIGIN, UNIT, Point(2.0, 0.0)), closed=False),
    "polygon": Polygon((ORIGIN, Point(1.0, 0.0), UNIT)),
    "circle": Circle(1.0, 2.0, 3.0),
}


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def read(dsn: str, sql: str, params: list[object] | None = None) -> Any:  # ruff: ignore[any-type]
    async def run(conn: Connection) -> Any:  # ruff: ignore[any-type]
        return (await conn._impl._fetch(sql, params or [], None, scalar=True, limit_one=True, arity=1))[0]

    return run_with_conn(dsn, run)


@pytest.mark.parametrize("key", sorted(VALUES))
def test_geometric_value_round_trips(schema: str, key: str) -> None:
    assert read(schema, f"SELECT $1::{key}", [VALUES[key]]) == VALUES[key]


@pytest.mark.parametrize("key", sorted(VALUES))
def test_geometric_array_decodes_and_encodes_with_null_elements_and_dimensions(schema: str, key: str) -> None:
    value = VALUES[key]
    assert read(schema, f"SELECT $1::{key}[]", [[value, None, value]]) == [value, None, value]
    matrix = [[value, None], [value, value]]
    assert read(schema, f"SELECT $1::{key}[]", [matrix]) == matrix
    assert read(schema, f"SELECT '{{}}'::{key}[]") == []
    assert read(schema, f"SELECT ARRAY[NULL]::{key}[]") == [None]


def test_geometric_array_decodes_from_server_literals(schema: str) -> None:
    assert read(schema, "SELECT '{\"(1,2)\",NULL}'::point[]") == [Point(1.0, 2.0), None]
    assert read(schema, "SELECT '{\"<(0,0),1>\"}'::circle[]") == [Circle(0.0, 0.0, 1.0)]
    assert read(schema, "SELECT '{\"((0,0),(1,1))\"}'::polygon[]") == [Polygon((ORIGIN, UNIT))]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT '(1,2)'::point", Point(1.0, 2.0)),
        ("SELECT '1,2'::point", Point(1.0, 2.0)),
        ("SELECT '{1,2,3}'::line", Line(1.0, 2.0, 3.0)),
        ("SELECT '[(0,0),(1,1)]'::line", Line(1.0, -1.0, 0.0)),
        ("SELECT '((1,2),(3,4))'::lseg", LineSegment(Point(1.0, 2.0), Point(3.0, 4.0))),
        ("SELECT '<(1,2),3>'::circle", Circle(1.0, 2.0, 3.0)),
        ("SELECT '((0,0),(1,1))'::path", Path((ORIGIN, UNIT), closed=True)),
        ("SELECT '[(0,0),(1,1)]'::path", Path((ORIGIN, UNIT), closed=False)),
        ("SELECT '(0,0),(1,1),(2,0)'::polygon", Polygon((ORIGIN, UNIT, Point(2.0, 0.0)))),
    ],
)
def test_geometric_text_forms_decode(schema: str, sql: str, expected: object) -> None:
    assert read(schema, sql) == expected


# the server stores the upper right corner first, whatever order the corners were given in
@pytest.mark.parametrize("literal", ["(0,0),(2,3)", "(2,3),(0,0)", "(0,3),(2,0)", "(2,0),(0,3)"])
def test_box_is_canonicalised_by_the_server(schema: str, literal: str) -> None:
    assert read(schema, f"SELECT '{literal}'::box") == Box(Point(2.0, 3.0), ORIGIN)


def test_box_given_in_any_corner_order_is_read_back_canonicalised(schema: str) -> None:
    assert read(schema, "SELECT $1::box", [Box(ORIGIN, Point(2.0, 3.0))]) == Box(Point(2.0, 3.0), ORIGIN)


def test_non_finite_coordinates_round_trip(schema: str) -> None:
    got = read(schema, "SELECT $1::point", [Point(math.inf, -math.inf)])
    assert got == Point(math.inf, -math.inf)
    nan = read(schema, "SELECT $1::point", [Point(math.nan, 1.0)])
    assert isinstance(nan, Point)
    assert math.isnan(nan.x)
    assert nan.y == pytest.approx(1.0)
    circle = read(schema, "SELECT $1::circle", [Circle(0.0, 0.0, math.inf)])
    assert circle == Circle(0.0, 0.0, math.inf)


def test_nan_coordinates_are_not_equal_to_each_other() -> None:
    # a dataclass compares its fields, and NaN != NaN
    assert Point(math.nan, 0.0) != Point(float("nan"), 0.0)
    assert Point(0.0, 0.0) == Point(-0.0, 0.0)


def test_equality_is_exact_not_epsilon_based(schema: str) -> None:
    near = read(schema, "SELECT '(1.0000001,1)'::point")
    assert near != Point(1.0, 1.0)
    # the server's `~=` calls them the same point, within its epsilon
    assert read(schema, "SELECT '(1.0000001,1)'::point ~= '(1,1)'::point") is True


def test_a_one_point_path_and_polygon_round_trip(schema: str) -> None:
    assert read(schema, "SELECT '[(1,1)]'::path") == Path((UNIT,), closed=False)
    assert read(schema, "SELECT $1::path", [Path((UNIT,), closed=True)]) == Path((UNIT,), closed=True)
    assert read(schema, "SELECT $1::polygon", [Polygon((UNIT,))]) == Polygon((UNIT,))


@pytest.mark.parametrize("value", [Path((), closed=False), Path((), closed=True), Polygon(())])
def test_a_path_or_polygon_without_points_is_an_encode_error(schema: str, value: object) -> None:
    key = "polygon" if isinstance(value, Polygon) else "path"
    with pytest.raises(EncodeError, match="at least one point"):
        read(schema, f"SELECT $1::{key}", [value])


def test_a_degenerate_line_is_rejected_by_the_server(schema: str) -> None:
    with pytest.raises(DatabaseError) as info:
        read(schema, "SELECT $1::line", [Line(0.0, 0.0, 1.0)])
    assert info.value.sqlstate == INVALID_BINARY_REPRESENTATION


def test_a_negative_radius_is_rejected_by_the_server(schema: str) -> None:
    with pytest.raises(DatabaseError) as info:
        read(schema, "SELECT $1::circle", [Circle(0.0, 0.0, -1.0)])
    assert info.value.sqlstate == INVALID_BINARY_REPRESENTATION


def test_ints_are_accepted_as_coordinates(schema: str) -> None:
    assert read(schema, "SELECT $1::point", [Point(1, 2)]) == Point(1.0, 2.0)


def test_geometric_values_are_hashable_frozen_and_slotted() -> None:
    value = Path((ORIGIN, UNIT), closed=True)
    assert {value: 1}[Path((ORIGIN, UNIT), closed=True)] == 1
    with pytest.raises(AttributeError):
        value.closed = False  # ty: ignore[invalid-assignment]
    assert not hasattr(value, "__dict__")


@pytest.mark.parametrize("key", sorted(VALUES))
def test_geometric_param_takes_only_its_own_class(schema: str, key: str) -> None:
    others = [value for other, value in VALUES.items() if other != key]
    for value in [*others, 5, "(1,2)", (1.0, 2.0), 1.5]:
        with pytest.raises(EncodeError, match=key):
            read(schema, f"SELECT $1::{key}", [value])


def test_geometric_param_needs_point_fields_of_the_point_class(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"expected a rowx\.postgres\.Point"):
        read(schema, "SELECT $1::lseg", [LineSegment((0.0, 0.0), UNIT)])  # ty: ignore[invalid-argument-type]
    with pytest.raises(EncodeError, match="is not a number"):
        read(schema, "SELECT $1::point", [Point("a", 1.0)])  # ty: ignore[invalid-argument-type]


def test_geometric_value_is_not_accepted_for_a_text_or_json_parameter(schema: str) -> None:
    with pytest.raises(EncodeError, match="not accepted for text"):
        read(schema, "SELECT $1::text", [ORIGIN])
    with pytest.raises(EncodeError, match="Point"):
        read(schema, "SELECT $1::json", [ORIGIN])


def test_geometric_values_are_read_through_a_query(schema: str) -> None:
    async def body(conn: Connection) -> tuple[Point, Circle | None, list[Polygon | None], tuple[Box, Path]]:
        one = await query[Point](t"SELECT {Point(1.0, 2.0)}::point").one(conn)
        none = await query[Circle | None](t"SELECT NULL::circle").one(conn)
        many = await query[list[Polygon | None]](t"SELECT ARRAY[{Polygon((ORIGIN,))}, NULL]::polygon[]").one(conn)
        pair = await query[tuple[Box, Path]](
            t"SELECT '(1,1),(0,0)'::box, '((0,0),(1,1))'::path",
        ).one(conn)
        return one, none, many, pair

    assert run_with_conn(schema, body) == (
        Point(1.0, 2.0),
        None,
        [Polygon((ORIGIN,)), None],
        (Box(UNIT, ORIGIN), Path((ORIGIN, UNIT), closed=True)),
    )
