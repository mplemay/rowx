from dataclasses import dataclass
from typing import Final

from rowx._rowplan import register_value_class

# bit 1 of the driver mask: this leaf keeps months, days and microseconds
INTERVAL_FLAG: Final[int] = 0b10


# lossless: infinity keeps the server's sentinel fields (the maximum or minimum of each)
@dataclass(frozen=True, slots=True, kw_only=True)
class Interval:
    months: int = 0
    days: int = 0
    microseconds: int = 0


register_value_class(
    Interval,
    flags=INTERVAL_FLAG,
    label="a subclass of rowx.postgres.Interval is not a row type",
)
