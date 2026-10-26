import dataclasses
from decimal import Decimal, localcontext

import pytest

from rowx.postgres import Money


@pytest.mark.parametrize(
    ("minor_units", "scale", "expected"),
    [
        (1_234_567, 2, Decimal("12345.67")),
        (1_234_567, 0, Decimal(1_234_567)),
        (-5, 3, Decimal("-0.005")),
        (0, 2, Decimal("0.00")),
        (100, 4, Decimal("0.0100")),
        (2**63 - 1, 2, Decimal("92233720368547758.07")),
    ],
)
def test_to_decimal_places_the_point_at_the_callers_scale(minor_units: int, scale: int, expected: Decimal) -> None:
    got = Money(minor_units).to_decimal(scale)
    assert got == expected
    assert got.as_tuple().exponent == -scale


def test_to_decimal_ignores_the_active_decimal_precision() -> None:
    with localcontext() as ctx:
        ctx.prec = 6
        assert Money(123_456_789).to_decimal(2) == Decimal("1234567.89")
        assert Money(2**63 - 1).to_decimal(2).as_tuple().digits == tuple(int(c) for c in str(2**63 - 1))


def test_money_has_no_scale_of_its_own() -> None:
    assert [field.name for field in dataclasses.fields(Money)] == ["minor_units"]
    assert Money(100) == Money(100)
    assert Money(100) != Money(1)


def test_money_is_frozen_hashable_and_slotted() -> None:
    value = Money(5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.minor_units = 6  # ty: ignore[invalid-assignment]
    assert {value: 1}[Money(5)] == 1
    assert not hasattr(value, "__dict__")
