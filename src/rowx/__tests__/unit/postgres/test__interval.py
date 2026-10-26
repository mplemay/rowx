import dataclasses

import pytest

from rowx.postgres import Interval


def test_every_field_defaults_to_zero() -> None:
    assert Interval() == Interval(months=0, days=0, microseconds=0)
    value = Interval(months=1, days=2, microseconds=3)
    assert (value.months, value.days, value.microseconds) == (1, 2, 3)


def test_months_and_days_are_distinct_from_each_other() -> None:
    assert Interval(months=1) != Interval(days=30)


def test_fields_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        Interval(1, 2, 3)  # ty: ignore[too-many-positional-arguments]


def test_an_interval_is_frozen_hashable_and_slotted() -> None:
    value = Interval(months=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.months = 2  # ty: ignore[invalid-assignment]
    assert {value: 1}[Interval(months=1)] == 1
    assert not hasattr(value, "__dict__")
