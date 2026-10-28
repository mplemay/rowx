import dataclasses

import pytest

from rowx.postgres import Cube


def test_fields_are_the_two_corners() -> None:
    assert [field.name for field in dataclasses.fields(Cube)] == ["lower_left", "upper_right"]


def test_value_is_frozen_hashable_and_slotted() -> None:
    cube = Cube((0.0, 1.0), (2.0, 3.0))
    assert hash(cube) == hash(Cube((0.0, 1.0), (2.0, 3.0)))
    assert {cube: 1}[Cube((0.0, 1.0), (2.0, 3.0))] == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        cube.lower_left = ()  # ty: ignore[invalid-assignment]
    assert not hasattr(cube, "__dict__")


def test_a_point_and_a_box_are_never_equal() -> None:
    assert Cube((1.0,), (1.0,)) != Cube((1.0,), (2.0,))
    assert Cube((1.0, 2.0), (1.0, 2.0)) != Cube((1.0,), (1.0,))
