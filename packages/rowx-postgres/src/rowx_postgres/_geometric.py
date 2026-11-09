from dataclasses import dataclass

from rowx_core import ValueClass


# Equality compares the fields exactly (a dataclass `==`), not with Postgres' epsilon-based `~=`. A NaN coordinate is
# never equal to anything, itself included.
@dataclass(frozen=True, slots=True)
class Point(ValueClass):
    x: float
    y: float


# the line `a*x + b*y + c = 0`; the server rejects `a == b == 0`
@dataclass(frozen=True, slots=True)
class Line(ValueClass):
    a: float
    b: float
    c: float


@dataclass(frozen=True, slots=True)
class LineSegment(ValueClass):
    start: Point
    end: Point


# the server canonicalises a box, so a decoded box always has its upper right corner first, whatever was stored
@dataclass(frozen=True, slots=True)
class Box(ValueClass):
    upper_right: Point
    lower_left: Point


# at least one point; `closed` tells `((1,1),(2,2))` from `[(1,1),(2,2)]`
@dataclass(frozen=True, slots=True)
class Path(ValueClass):
    points: tuple[Point, ...]
    closed: bool


@dataclass(frozen=True, slots=True)
class Polygon(ValueClass):
    points: tuple[Point, ...]


@dataclass(frozen=True, slots=True)
class Circle(ValueClass):
    x: float
    y: float
    radius: float
