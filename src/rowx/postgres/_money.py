from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Money:
    # the int8 the server sends; the scale comes from lc_monetary, which can change mid-session, so none is held
    minor_units: int

    def to_decimal(self, scale: int) -> Decimal:
        return Decimal(self.minor_units).scaleb(-scale)
