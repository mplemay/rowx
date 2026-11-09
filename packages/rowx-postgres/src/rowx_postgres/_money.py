from dataclasses import dataclass
from decimal import Decimal

from rowx_core import ValueClass


@dataclass(frozen=True, slots=True)
class Money(ValueClass):
    # the int8 the server sends; the scale comes from lc_monetary, which can change mid-session, so none is held
    minor_units: int

    def to_decimal(self, scale: int) -> Decimal:
        # built from text so the active decimal context never rounds the digits
        return Decimal(f"{self.minor_units}E{-scale}")
