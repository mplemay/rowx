from dataclasses import dataclass

from rowx._rowplan import register_value_class


# both corners keep the dimension, and a point has equal corners, so a point and a box never get confused;
# the codec writes the point flag when the corners are equal
@dataclass(frozen=True, slots=True)
class Cube:
    lower_left: tuple[float, ...]
    upper_right: tuple[float, ...]


register_value_class(Cube, flags=0, label="a subclass of rowx.postgres.Cube is not a row type")
