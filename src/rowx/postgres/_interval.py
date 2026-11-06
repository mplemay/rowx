from dataclasses import dataclass
from typing import ClassVar, Final

from rowx._rowplan import ValueClass

# bit 1 of the driver mask: this leaf keeps months, days and microseconds
INTERVAL_FLAG: Final[int] = 0b10


# lossless: infinity keeps the server's sentinel fields (the maximum or minimum of each)
@dataclass(frozen=True, slots=True, kw_only=True)
class Interval(ValueClass):
    _rowx_flags: ClassVar[int] = INTERVAL_FLAG
    months: int = 0
    days: int = 0
    microseconds: int = 0
