import dataclasses
import math
from typing import Final

import pytest

from rowx.postgres import Box, Circle, Line, LineSegment, Path, Point, Polygon

ORIGIN = Point(0.0, 0.0)
UNIT = Point(1.0, 1.0)

type Shape = Point | Line | LineSegment | Box | Path | Polygon | Circle

FIELDS: Final[dict[str, list[str]]] = {
    "Point": ["x", "y"],
    "Line": ["a", "b", "c"],
    "LineSegment": ["start", "end"],
    "Box": ["upper_right", "lower_left"],
    "Path": ["points", "closed"],
    "Polygon": ["points"],
    "Circle": ["center", "radius"],
}

VALUES: Final[list[Shape]] = [
    Point(1.0, 2.0),
    Line(1.0, 2.0, 3.0),
    LineSegment(ORIGIN, UNIT),
    Box(UNIT, ORIGIN),
    Path((ORIGIN, UNIT), closed=True),
    Polygon((ORIGIN, UNIT)),
    Circle(ORIGIN, 1.0),
]


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_fields_are_in_the_order_the_server_sends_them(value: Shape) -> None:
    assert [field.name for field in dataclasses.fields(value)] == FIELDS[type(value).__name__]


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_value_is_frozen_hashable_and_slotted(value: Shape) -> None:
    assert hash(value) == hash(dataclasses.replace(value))
    assert {value: 1}[dataclasses.replace(value)] == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, FIELDS[type(value).__name__][0], 0)
    assert not hasattr(value, "__dict__")


def test_equality_compares_the_fields_exactly() -> None:
    assert Point(1.0, 1.0) == Point(1.0, 1.0)
    assert Point(1.0, 1.0) != Point(1.0 + 1e-9, 1.0)
    assert Path((ORIGIN,), closed=True) != Path((ORIGIN,), closed=False)
    assert Path((ORIGIN, UNIT), closed=True) != Path((UNIT, ORIGIN), closed=True)
    assert Box(UNIT, ORIGIN) != Box(ORIGIN, UNIT)


def test_a_nan_coordinate_is_never_equal() -> None:
    nan = math.nan
    assert Point(nan, 0.0) != Point(nan, 0.0)
    assert Point(math.nan, 0.0) != Point(float("nan"), 0.0)
    assert Polygon((Point(nan, 0.0),)) != Polygon((Point(nan, 0.0),))


def test_classes_of_different_types_are_never_equal() -> None:
    assert Polygon((ORIGIN,)) != Path((ORIGIN,), closed=True)
    assert Point(0.0, 0.0) != (0.0, 0.0)
