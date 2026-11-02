from typing import Final

type FieldRow = tuple[str, tuple[str, ...] | None, bool, bool]
type Plan = tuple[str, int, tuple[bool, ...]]
# a named plan is `named`, the form, the matching, then one `(key, names, nullable, required)` per field
type NamedPlan = tuple[str, str, str, tuple[FieldRow, ...]]
type Row = tuple[str, Plan | NamedPlan | str, str]

PRELUDE: Final[str] = """\
import collections
import datetime
import decimal
import ipaddress
import pydantic
import typing
import typing_extensions
import uuid
from dataclasses import KW_ONLY, InitVar, dataclass, field
from pydantic import AliasChoices, BaseModel, ConfigDict, AnyUrl, Field, PositiveInt, RootModel, SecretStr, model_validator
from rowx.postgres import Box, Circle, Cube, Interval, Line, LineSegment, Money, Path, Point, Polygon, Range
from typing import Annotated, Any, ClassVar, Literal, NamedTuple, NotRequired, Optional, Protocol, ReadOnly, Required, TypeVar, TypedDict, Union
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
    ("T = Range[int]", ("scalar", 1, (False,)), "ok"),
    ("T = Range[decimal.Decimal] | None", ("scalar", 1, (True,)), "ok"),
    ("T = Optional[Range[datetime.date]]", ("scalar", 1, (True,)), "ok"),
    ("T = tuple[Range[datetime.date], Range[datetime.datetime] | None, str]", ("tuple", 3, (False, True, False)), "ok"),
    ("T = list[Range[int] | None]", ("scalar", 1, (False,)), "ok"),
    ("type R = Range[int]\nT = tuple[R, R | None]", ("tuple", 2, (False, True)), "ok"),
    ("T = Money", ("scalar", 1, (False,)), "ok"),
    ("T = Money | None", ("scalar", 1, (True,)), "ok"),
    ("T = Interval", ("scalar", 1, (False,)), "ok"),
    ("T = Optional[Interval]", ("scalar", 1, (True,)), "ok"),
    ("T = tuple[Interval, datetime.timedelta | None, Money]", ("tuple", 3, (False, True, False)), "ok"),
    ("T = list[Interval | None]", ("scalar", 1, (False,)), "ok"),
    ("T = list[list[Money]]", ("scalar", 1, (False,)), "ok"),
    ("T = Point", ("scalar", 1, (False,)), "ok"),
    ("T = Circle | None", ("scalar", 1, (True,)), "ok"),
    (
        "T = tuple[Line, LineSegment | None, Box, Path, Polygon | None]",
        ("tuple", 5, (False, True, False, False, True)),
        "ok",
    ),
    ("T = list[Point | None]", ("scalar", 1, (False,)), "ok"),
    ("T = list[list[Polygon]]", ("scalar", 1, (False,)), "ok"),
    ("T = Cube", ("scalar", 1, (False,)), "ok"),
    ("T = Cube | None", ("scalar", 1, (True,)), "ok"),
    ("T = list[Cube | None]", ("scalar", 1, (False,)), "ok"),
    ("T = dict[str, str | None]", ("scalar", 1, (False,)), "ok"),
    ("T = dict[str, str | None] | None", ("scalar", 1, (True,)), "ok"),
    ("T = Optional[dict[str, Optional[str]]]", ("scalar", 1, (True,)), "ok"),
    ("T = tuple[dict[str, str | None], str]", ("tuple", 2, (False, False)), "ok"),
    ("T = list[dict[str, str | None] | None]", ("scalar", 1, (False,)), "ok"),
    ("type H = dict[str, str | None]\nT = H", ("scalar", 1, (False,)), "ok"),
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
    ("T = Range", "InterfaceError", "invalid-row-type"),
    ("T = Range[str]", "InterfaceError", "invalid-row-type"),
    ("T = Range[float]", "InterfaceError", "invalid-row-type"),
    ("T = Range[bool]", "InterfaceError", "invalid-row-type"),
    ("T = Range[int | None]", "InterfaceError", "invalid-row-type"),
    ("T = Range[list[int]]", "InterfaceError", "invalid-row-type"),
    ("T = Range[Range[int]]", "InterfaceError", "invalid-row-type"),
    ("T = list[Range]", "InterfaceError", "invalid-row-type"),
    ("T = Interval | Money", "InterfaceError", "invalid-row-type"),
    ("T = Point | Circle", "InterfaceError", "invalid-row-type"),
    ("T = Point | datetime.date", "InterfaceError", "invalid-row-type"),
    ("T = Interval | datetime.timedelta", "InterfaceError", "invalid-row-type"),
    ("T = int | str | None", "InterfaceError", "invalid-row-type"),
    ("T = 'int'", "InterfaceError", "invalid-row-type"),
    ("T = tuple['int', str]", "InterfaceError", "invalid-row-type"),
    ("T = TypeVar('T')", "InterfaceError", "invalid-row-type"),
    ("type Row[X] = tuple[X, str]\nT = Row[int]", "InterfaceError", "invalid-row-type"),
    ("T = Literal[1]", "InterfaceError", "invalid-row-type"),
    ("T = dict[str, int]", "InterfaceError", "invalid-row-type"),
    ("T = dict[str, str]", "InterfaceError", "invalid-row-type"),
    ("T = dict[int, str | None]", "InterfaceError", "invalid-row-type"),
    ("T = dict", "InterfaceError", "invalid-row-type"),
    ("T = Cube | Point", "InterfaceError", "invalid-row-type"),
    ("T = list", "InterfaceError", "invalid-row-type"),
    ("T = list[dict]", "InterfaceError", "invalid-row-type"),
    ("T = tuple[tuple[int, int], str]", "InterfaceError", "invalid-row-type"),
    ("class C: ...\nT = C", "InterfaceError", "invalid-row-type"),
]

# The checker resolves a class binding to `invalid-row-type` until #187 adds the named forms. A row whose verdict is
# `PENDING_187` flips to `ok` there; the other `invalid-row-type` rows are rejected on both sides for good.
PENDING_187: Final[str] = "invalid-row-type"
# `PENDING_188` is the same for the pydantic rows the checker accepts from #188 on
PENDING_188: Final[str] = "invalid-row-type"
# the runtime accepts these and the checker rejects them for good (a checker-only rejection)
CHECKER_ONLY: Final[str] = "invalid-row-type"
REJECTED: Final[str] = "invalid-row-type"


def keyword(*fields: FieldRow) -> NamedPlan:
    return ("named", "keyword", "by_name", fields)


def positional(*fields: FieldRow) -> NamedPlan:
    return ("named", "positional", "positional", fields)


def dict_form(*fields: FieldRow) -> NamedPlan:
    return ("named", "dict", "by_name", fields)


def validator(*fields: FieldRow) -> NamedPlan:
    return ("named", "validator", "by_name", fields)


def f(key: str, *, nullable: bool = False, required: bool = True) -> FieldRow:
    return (key, (key,), nullable, required)


NAMED_ROWS: Final[list[Row]] = [
    # stdlib dataclasses
    (
        "@dataclass\nclass T:\n    id: int\n    name: str | None\n    tags: list[str] = field(default_factory=list)",
        keyword(f("id"), f("name", nullable=True), f("tags", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "@dataclass(slots=True, frozen=True, kw_only=True)\nclass T:\n    a: int\n    b: int = 0",
        keyword(f("a"), f("b", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "@dataclass\nclass T:\n    a: int\n    _: KW_ONLY\n    b: str\n    c: str = field(kw_only=True, default='')",
        keyword(f("a"), f("b"), f("c", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        (
            "@dataclass\nclass Base:\n    a: int\n    b: str\n@dataclass\nclass Mid(Base):\n    c: int\n"
            "@dataclass\nclass T(Mid):\n    b: str | None\n    d: bool = False"
        ),
        keyword(f("a"), f("b", nullable=True), f("c"), f("d", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "@dataclass\nclass T:\n    a: int\n    c: ClassVar[int] = 0\n    d: int = field(init=False, default=1)",
        keyword(f("a")),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "@dataclass\nclass T:\n    a: Annotated[int, 'm']\n    b: Any\n    c: None\n    d: Interval | None\n    e: Range[int]",
        keyword(f("a"), f("b", nullable=True), f("c", nullable=True), f("d", nullable=True), f("e")),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "@dataclass\nclass T:\n    m: Money\n    p: Point\n    r: Range[datetime.date] | None\n    h: dict[str, str | None]",
        keyword(f("m"), f("p"), f("r", nullable=True), f("h")),
        PENDING_187,  # #187 flips to "ok"
    ),
    ("@dataclass\nclass U:\n    id: int\ntype A = U\nT = A", keyword(f("id")), PENDING_187),  # #187 flips to "ok"
    # a hand-written `__init__` is a checker-only rejection: the runtime accepts it
    (
        "@dataclass\nclass T:\n    id: int\n    def __init__(self, id: int) -> None:\n        self.id = id",
        keyword(f("id")),
        REJECTED,
    ),
    # typing.NamedTuple
    (
        "class T(NamedTuple):\n    id: int\n    name: str | None\n    note: str = ''",
        positional(f("id"), f("name", nullable=True), f("note", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "T = NamedTuple('T', [('id', int), ('name', str | None)])",
        positional(f("id"), f("name", nullable=True)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "class Base(NamedTuple):\n    id: int\nclass T(Base):\n    extra: str",
        positional(f("id")),
        PENDING_187,  # #187 flips to "ok"
    ),
    # TypedDict
    (
        "class T(TypedDict):\n    id: int\n    name: str | None\n    note: NotRequired[str]",
        dict_form(f("id"), f("name", nullable=True), f("note", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "class T(TypedDict, total=False):\n    a: int\n    b: Required[str]\n    c: ReadOnly[int]",
        dict_form(f("a", required=False), f("b"), f("c", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "T = TypedDict('T', {'id': int, 'name': str | None})",
        dict_form(f("id"), f("name", nullable=True)),
        PENDING_187,
    ),  # #187 flips to "ok"
    (
        "class T(typing_extensions.TypedDict):\n    id: int\n    n: typing_extensions.NotRequired[str]",
        dict_form(f("id"), f("n", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        (
            "class Base(TypedDict):\n    a: int\n    b: str\nclass Mid(Base, total=False):\n    c: int\n"
            "class T(Mid):\n    b: str | None\n    d: int"
        ),
        dict_form(f("a"), f("b", nullable=True), f("c", required=False), f("d")),
        PENDING_187,  # #187 flips to "ok"
    ),
    # quoted qualifiers are missed by `__required_keys__`; requiredness follows the resolved annotation
    (
        "class T(TypedDict):\n    id: int\n    n: 'NotRequired[str]'\n    m: NotRequired['str']",
        dict_form(f("id"), f("n", required=False), f("m", required=False)),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "class T(TypedDict, total=False):\n    id: int\n    n: 'Required[str]'\n    m: 'Required[ReadOnly[int]]'",
        dict_form(f("id", required=False), f("n"), f("m")),
        PENDING_187,  # #187 flips to "ok"
    ),
    (
        "class T(typing_extensions.TypedDict):\n    n: 'typing_extensions.NotRequired[str]'\n    m: int",
        dict_form(f("n", required=False), f("m")),
        PENDING_187,  # #187 flips to "ok"
    ),
    # pydantic models and dataclasses
    (
        "class T(BaseModel):\n    id: int\n    name: str | None\n    tags: list[str] = []",
        validator(f("id"), f("name", nullable=True), f("tags", required=False)),
        PENDING_188,  # #188 flips to "ok"
    ),
    (
        "class T(BaseModel):\n    id: int = Field(alias='user_id')\n    n: str = Field(validation_alias='nm', alias='x')",
        validator(("id", ("user_id",), False, True), ("n", ("nm",), False, True)),
        PENDING_188,  # #188 flips to "ok"
    ),
    (
        "class T(BaseModel, populate_by_name=True):\n    id: int = Field(alias='user_id')\n    n: str",
        validator(("id", ("user_id", "id"), False, True), f("n")),
        PENDING_188,  # #188 flips to "ok"
    ),
    (
        (
            "class T(BaseModel):\n    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)\n"
            "    id: int = Field(alias='user_id')"
        ),
        validator(f("id")),
        PENDING_188,  # #188 flips to "ok"
    ),
    (
        "class T(BaseModel):\n    id: int = Field(validation_alias=AliasChoices('a', 'b'))\n    n: str",
        validator(("id", None, False, True), f("n")),
        PENDING_188,  # #188 flips to "ok", with `note: names not checked` for the field
    ),
    (
        (
            "class T(BaseModel):\n    id: int\n    n: str\n    @model_validator(mode='before')\n    @classmethod\n"
            "    def pre(cls, data):\n        return data"
        ),
        validator(("id", None, False, True), ("n", None, False, True)),
        PENDING_188,  # #188 flips to "ok", with `note: names not checked` for the model
    ),
    (
        "class T(BaseModel):\n    model_config = ConfigDict(alias_generator=lambda name: name.upper())\n    id: int",
        validator(("id", ("ID",), False, True)),
        # #188 gives the checker its unknown-names verdict; the runtime sees the generated aliases
        PENDING_188,
    ),
    (
        "@pydantic.dataclasses.dataclass(config=ConfigDict(strict=True))\nclass T:\n    id: int\n    name: str | None",
        validator(f("id"), f("name", nullable=True)),
        PENDING_188,  # #188 flips to "ok"
    ),
    ("class T(BaseModel):\n    id: PositiveInt", validator(f("id")), CHECKER_ONLY),
    ("class T(BaseModel):\n    id: pydantic.constr(min_length=1)", validator(f("id")), CHECKER_ONLY),
    ("class T(RootModel[int]): ...", "InterfaceError", REJECTED),
    # `EmailStr` needs the optional email-validator package to even define the class; `AnyUrl` and `SecretStr` are
    # the same kind of pydantic-only class
    ("class T(BaseModel):\n    url: AnyUrl", "InterfaceError", REJECTED),
    ("class T(BaseModel):\n    secret: SecretStr", "InterfaceError", REJECTED),
    ("class T(BaseModel): ...", "InterfaceError", REJECTED),
    ("@pydantic.dataclasses.dataclass\nclass T:\n    id: int\n    scale: InitVar[int]", "InterfaceError", REJECTED),
    # rejections on both sides
    ("class P2(Point): ...\nT = P2", "InterfaceError", REJECTED),
    ("@dataclass(frozen=True)\nclass P2(Point):\n    z: int\nT = P2", "InterfaceError", REJECTED),
    ("class S(Range[int]): ...\nT = S", "InterfaceError", REJECTED),
    ("class I2(Interval): ...\nT = I2", "InterfaceError", REJECTED),
    ("class M2(Money): ...\nT = M2", "InterfaceError", REJECTED),
    ("class C2(Cube): ...\nT = C2", "InterfaceError", REJECTED),
    # generic row types are rejected by the runtime until #189; the checker has no class binding for them yet
    ("@dataclass\nclass T[X]:\n    a: int", "InterfaceError", REJECTED),  # #189 lifts this
    ("class T(Protocol):\n    id: int", "InterfaceError", REJECTED),
    ("T = collections.namedtuple('T', ['id', 'name'])", "InterfaceError", REJECTED),
    ("@dataclass(init=False)\nclass T:\n    id: int", "InterfaceError", REJECTED),
    ("@dataclass\nclass T:\n    id: int\n    scale: InitVar[int]", "InterfaceError", REJECTED),
    ("@dataclass\nclass T: ...", "InterfaceError", REJECTED),
    ("class T(NamedTuple): ...", "InterfaceError", REJECTED),
    ("class T(TypedDict): ...", "InterfaceError", REJECTED),
    ("@dataclass\nclass U:\n    id: int\nT = U | None", "InterfaceError", REJECTED),
    ("@dataclass\nclass U:\n    id: int\nT = Optional[U]", "InterfaceError", REJECTED),
    ("@dataclass\nclass U:\n    id: int\nT = tuple[U, int]", "InterfaceError", REJECTED),
    ("@dataclass\nclass U:\n    id: int\nT = list[U]", "InterfaceError", REJECTED),
    ("@dataclass\nclass U:\n    id: int\nT = U | int", "InterfaceError", REJECTED),
    ("@dataclass\nclass U:\n    id: int\n@dataclass\nclass T:\n    u: U", "InterfaceError", REJECTED),
    ("class T(NamedTuple):\n    a: tuple[int, int]", "InterfaceError", REJECTED),
    ("class T(TypedDict):\n    a: int | str", "InterfaceError", REJECTED),
]

ROWS.extend(NAMED_ROWS)
