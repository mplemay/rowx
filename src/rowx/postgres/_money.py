from dataclasses import dataclass
from decimal import Decimal

from rowx._rowplan import register_value_class


@dataclass(frozen=True, slots=True)
class Money:
    # the int8 the server sends; the scale comes from lc_monetary, which can change mid-session, so none is held
    minor_units: int

    def to_decimal(self, scale: int) -> Decimal:
        # built from text so the active decimal context never rounds the digits
        return Decimal(f"{self.minor_units}E{-scale}")


register_value_class(Money, flags=0, label="a subclass of rowx.postgres.Money is not a row type")
