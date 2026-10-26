from dataclasses import KW_ONLY, dataclass


@dataclass(frozen=True, slots=True)
class Range[T]:
    lower: T | None = None
    upper: T | None = None
    _: KW_ONLY
    lower_inc: bool = True
    upper_inc: bool = False
    empty: bool = False

    def __post_init__(self) -> None:
        # the server's canonical form: an infinite side is never inclusive, and an empty range has no bounds,
        # so equal ranges compare equal however they were built
        if self.empty:
            object.__setattr__(self, "lower", None)
            object.__setattr__(self, "upper", None)
            object.__setattr__(self, "lower_inc", False)
            object.__setattr__(self, "upper_inc", False)
            return
        if self.lower is None:
            object.__setattr__(self, "lower_inc", False)
        if self.upper is None:
            object.__setattr__(self, "upper_inc", False)

    @property
    def lower_inf(self) -> bool:
        return not self.empty and self.lower is None

    @property
    def upper_inf(self) -> bool:
        return not self.empty and self.upper is None

    @property
    def bounds(self) -> str:
        if self.empty:
            return "empty"
        return ("[" if self.lower_inc else "(") + ("]" if self.upper_inc else ")")
