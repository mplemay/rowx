import dataclasses

import pytest

from rowx.postgres import Range


def test_defaults_are_a_closed_open_range() -> None:
    value = Range(1, 5)
    assert (value.lower, value.upper, value.lower_inc, value.upper_inc, value.empty) == (1, 5, True, False, False)
    assert value.bounds == "[)"


def test_an_infinite_side_is_never_inclusive() -> None:
    assert Range(None, 5) == Range(None, 5, lower_inc=False)
    assert Range(1, None, upper_inc=True) == Range(1, None)
    unbounded = Range(None, None, lower_inc=True, upper_inc=True)
    assert (unbounded.lower_inf, unbounded.upper_inf, unbounded.bounds) == (True, True, "()")


def test_every_empty_range_is_equal() -> None:
    assert Range(empty=True) == Range(1, 5, empty=True)
    empty = Range(3, 4, lower_inc=False, empty=True)
    assert (empty.lower, empty.upper, empty.lower_inc, empty.upper_inc) == (None, None, False, False)
    assert (empty.lower_inf, empty.upper_inf, empty.bounds) == (False, False, "empty")


def test_inclusivity_distinguishes_ranges() -> None:
    assert Range(1, 5) != Range(1, 5, upper_inc=True)
    assert Range(1, 5, lower_inc=False).bounds == "()"
    assert Range(1, 5, lower_inc=False, upper_inc=True).bounds == "(]"


def test_a_range_is_frozen_hashable_and_slotted() -> None:
    value = Range(1, 5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.lower = 2  # ty: ignore[invalid-assignment]
    assert {value: 1}[Range(1, 5)] == 1
    assert not hasattr(value, "__dict__")


def test_flags_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        Range(1, 5, True)  # ty: ignore[too-many-positional-arguments]  # ruff: ignore[boolean-positional-value-in-call]
