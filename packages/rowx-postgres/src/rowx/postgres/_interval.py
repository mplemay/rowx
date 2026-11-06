from dataclasses import dataclass


# lossless: infinity keeps the server's sentinel fields (the maximum or minimum of each)
@dataclass(frozen=True, slots=True, kw_only=True)
class Interval:
    months: int = 0
    days: int = 0
    microseconds: int = 0
