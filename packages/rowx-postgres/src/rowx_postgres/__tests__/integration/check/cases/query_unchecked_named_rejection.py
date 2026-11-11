from dataclasses import dataclass

from rowx import query_unchecked


@dataclass
class WithInit:
    a: int

    def __init__(self, a: int) -> None:
        self.a = a


def run() -> None:
    query_unchecked[WithInit](t"SELECT 1")  # error: [invalid-row-type]
