from dataclasses import dataclass


# both corners keep the dimension, and a point has equal corners, so a point and a box never get confused;
# the codec writes the point flag when the corners are equal
@dataclass(frozen=True, slots=True)
class Cube:
    lower_left: tuple[float, ...]
    upper_right: tuple[float, ...]
