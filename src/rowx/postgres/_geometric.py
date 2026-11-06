from dataclasses import dataclass

from rowx._rowplan import register_value_class


# Equality compares the fields exactly (a dataclass `==`), not with Postgres' epsilon-based `~=`. A NaN coordinate is
# never equal to anything, itself included.
@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float


# the line `a*x + b*y + c = 0`; the server rejects `a == b == 0`
@dataclass(frozen=True, slots=True)
class Line:
    a: float
    b: float
    c: float


@dataclass(frozen=True, slots=True)
class LineSegment:
    start: Point
    end: Point


# the server canonicalises a box, so a decoded box always has its upper right corner first, whatever was stored
@dataclass(frozen=True, slots=True)
class Box:
    upper_right: Point
    lower_left: Point


# at least one point; `closed` tells `((1,1),(2,2))` from `[(1,1),(2,2)]`
@dataclass(frozen=True, slots=True)
class Path:
    points: tuple[Point, ...]
    closed: bool


@dataclass(frozen=True, slots=True)
class Polygon:
    points: tuple[Point, ...]


@dataclass(frozen=True, slots=True)
class Circle:
    x: float
    y: float
    radius: float


register_value_class(Point, flags=0, label="a subclass of rowx.postgres.Point is not a row type")
register_value_class(Line, flags=0, label="a subclass of rowx.postgres.Line is not a row type")
register_value_class(LineSegment, flags=0, label="a subclass of rowx.postgres.LineSegment is not a row type")
register_value_class(Box, flags=0, label="a subclass of rowx.postgres.Box is not a row type")
register_value_class(Path, flags=0, label="a subclass of rowx.postgres.Path is not a row type")
register_value_class(Polygon, flags=0, label="a subclass of rowx.postgres.Polygon is not a row type")
register_value_class(Circle, flags=0, label="a subclass of rowx.postgres.Circle is not a row type")
