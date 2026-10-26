from typing import Final

type Plan = tuple[str, int, tuple[bool, ...]]
type Row = tuple[str, Plan | str, str]

PRELUDE: Final[str] = """\
import datetime
import decimal
import ipaddress
import uuid
from typing import Annotated, Any, Literal, Optional, TypeVar, Union
"""

NONE: Final[Plan] = ("none", 0, ())

# (source_text, expected_plan_or_error, expected_checker_verdict); source text assigns `T`.
ROWS: Final[list[Row]] = [
    ("T = None", NONE, "ok"),
    ("T = int", ("scalar", 1, (False,)), "ok"),
    ("T = int | None", ("scalar", 1, (True,)), "ok"),
    ("T = Optional[str]", ("scalar", 1, (True,)), "ok"),
    ("T = Union[str, None]", ("scalar", 1, (True,)), "ok"),
    ("T = Any", ("scalar", 1, (True,)), "ok"),
    ("T = object", ("scalar", 1, (True,)), "ok"),
    ("T = uuid.UUID", ("scalar", 1, (False,)), "ok"),
    ("T = datetime.datetime", ("scalar", 1, (False,)), "ok"),
    ("T = ipaddress.IPv4Interface | ipaddress.IPv6Interface", ("scalar", 1, (False,)), "ok"),
    ("T = ipaddress.IPv4Network | ipaddress.IPv6Network", ("scalar", 1, (False,)), "ok"),
    ("T = ipaddress.IPv4Interface | ipaddress.IPv6Interface | None", ("scalar", 1, (True,)), "ok"),
    ("T = Optional[ipaddress.IPv4Network | ipaddress.IPv6Network]", ("scalar", 1, (True,)), "ok"),
    ("T = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]", ("scalar", 1, (False,)), "ok"),
    ("T = list[ipaddress.IPv4Interface | ipaddress.IPv6Interface | None]", ("scalar", 1, (False,)), "ok"),
    (
        "type A = ipaddress.IPv4Interface | ipaddress.IPv6Interface\nT = tuple[A, A | None, str]",
        ("tuple", 3, (False, True, False)),
        "ok",
    ),
    ("T = tuple[int, str]", ("tuple", 2, (False, False)), "ok"),
    ("T = tuple[int]", ("tuple", 1, (False,)), "ok"),
    ("T = tuple[None]", ("tuple", 1, (True,)), "ok"),
    ("T = tuple[int, None]", ("tuple", 2, (False, True)), "ok"),
    ("type A = tuple[int, str | None]\nT = A", ("tuple", 2, (False, True)), "ok"),
    ("type A = int | None\ntype B = A\nT = B", ("scalar", 1, (True,)), "ok"),
    ("type A = None\nT = A", NONE, "ok"),
    ("T = Annotated[int, 'meta']", ("scalar", 1, (False,)), "ok"),
    ("T = Annotated[tuple[int, Any], 1]", ("tuple", 2, (False, True)), "ok"),
    ("type A = Annotated[int | None, 1]\nT = tuple[A, str]", ("tuple", 2, (True, False)), "ok"),
    ("T = list[int | None]", ("scalar", 1, (False,)), "ok"),
    ("T = list[list[int]]", ("scalar", 1, (False,)), "ok"),
    ("T = tuple[list[str] | None, decimal.Decimal]", ("tuple", 2, (True, False)), "ok"),
    ("T = tuple[int, ...]", "InterfaceError", "invalid-row-type"),
    ("T = tuple", "InterfaceError", "invalid-row-type"),
    ("T = tuple[()]", "InterfaceError", "invalid-row-type"),
    ("T = int | str", "InterfaceError", "invalid-row-type"),
    ("T = ipaddress.IPv4Interface", "InterfaceError", "invalid-row-type"),
    ("T = ipaddress.IPv6Network", "InterfaceError", "invalid-row-type"),
    ("T = ipaddress.IPv4Interface | ipaddress.IPv6Network", "InterfaceError", "invalid-row-type"),
    ("T = ipaddress.IPv4Address | ipaddress.IPv6Address", "InterfaceError", "invalid-row-type"),
    ("T = ipaddress.IPv4Interface | ipaddress.IPv6Interface | int", "InterfaceError", "invalid-row-type"),
    ("T = list[ipaddress.IPv4Interface]", "InterfaceError", "invalid-row-type"),
    ("T = int | str | None", "InterfaceError", "invalid-row-type"),
    ("T = 'int'", "InterfaceError", "invalid-row-type"),
    ("T = tuple['int', str]", "InterfaceError", "invalid-row-type"),
    ("T = TypeVar('T')", "InterfaceError", "invalid-row-type"),
    ("type Row[X] = tuple[X, str]\nT = Row[int]", "InterfaceError", "invalid-row-type"),
    ("T = Literal[1]", "InterfaceError", "invalid-row-type"),
    ("T = dict[str, int]", "InterfaceError", "invalid-row-type"),
    ("T = list", "InterfaceError", "invalid-row-type"),
    ("T = list[dict]", "InterfaceError", "invalid-row-type"),
    ("T = tuple[tuple[int, int], str]", "InterfaceError", "invalid-row-type"),
    ("class C: ...\nT = C", "InterfaceError", "invalid-row-type"),
]
