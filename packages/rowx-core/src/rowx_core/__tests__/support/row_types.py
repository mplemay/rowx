from typing import Final

type FieldRow = tuple[str, tuple[str, ...] | None, bool, bool]
type Plan = tuple[str, int, tuple[bool, ...]]
# a named plan is `named`, the form, the matching, then one `(key, names, nullable, required)` per field
type NamedPlan = tuple[str, str, str, tuple[FieldRow, ...]]
type Row = tuple[str, Plan | NamedPlan | str, str]

PRELUDE: Final[str] = """\
import collections
import attrs
import datetime
import decimal
import ipaddress
import pydantic
import typing
import typing_extensions
import uuid
from dataclasses import KW_ONLY, InitVar, dataclass, field
from pydantic import AliasChoices, BaseModel, ConfigDict, AnyUrl, Field, PositiveInt, RootModel, SecretStr, model_validator
from rowx_core._rowplan import ValueClass
@dataclass(frozen=True)
class Money(ValueClass): ...
@dataclass(frozen=True)
class Interval(ValueClass):
    _rowx_flags = 2
@dataclass(frozen=True)
class Point(ValueClass): ...
@dataclass(frozen=True)
class Line(ValueClass): ...
@dataclass(frozen=True)
class LineSegment(ValueClass): ...
@dataclass(frozen=True)
class Box(ValueClass): ...
@dataclass(frozen=True)
class Path(ValueClass): ...
@dataclass(frozen=True)
class Polygon(ValueClass): ...
@dataclass(frozen=True)
class Circle(ValueClass): ...
@dataclass(frozen=True)
class Cube(ValueClass): ...
@dataclass(frozen=True)
class Range[T](ValueClass):
    _rowx_subscripted = True
from typing import Annotated, Any, ClassVar, Literal, NamedTuple, NotRequired, Optional, Protocol, ReadOnly, Required, TypeVar, TypedDict, Union, overload
"""

NONE: Final[Plan] = ("none", 0, ())

# (source_text, expected_plan_or_error, expected_checker_verdict); source text assigns `T`.
ATTRS_ROWS: Final[list[Row]] = [
    (
        "@attrs.define\nclass T:\n    value: int = attrs.field(factory=lambda: 1)",
        ("named", "validator", "by_name", (("value", ("value",), False, False),)),
        "ok",
    ),
    (
        "@attrs.define\nclass T:\n    visible: int = attrs.field()\n    hidden = attrs.field()",
        "unresolved",
        "invalid-row-type",
    ),
    (
        "field_alias = attrs.field\n@attrs.define\nclass T:\n    hidden = field_alias()",
        "unresolved",
        "invalid-row-type",
    ),
    (
        "@attrs.define\nclass T:\n    visible: int = attrs.field()\n    hidden = attrs.field(init=False, default=1)",
        ("named", "validator", "by_name", (("visible", ("visible",), False, True),)),
        "ok",
    ),
    *(
        (
            (
                "async def convert(value: int) -> str: return str(value)\n@attrs.define\n"
                f"class T:\n    value: str = attrs.field(converter={converter})"
            ),
            "unsupported converter",
            "invalid-row-type",
        )
        for converter in ["convert", "attrs.Converter(convert)"]
    ),
    (
        "@attrs.define\nclass T:\n    value: int = attrs.field(factory=None)",
        ("named", "validator", "by_name", (("value", ("value",), False, True),)),
        "ok",
    ),
    (
        "@attrs.define\nclass T:\n    a: int = attrs.field(default=1, factory=None)\n    b: int = attrs.field(factory=None, default=2)",
        ("named", "validator", "by_name", (("a", ("a",), False, False), ("b", ("b",), False, False))),
        "ok",
    ),
    *(
        (
            (
                f"def convert(value: int) -> {output}: return value\n"
                f"@attrs.define\nclass T:\n    value: {field} = attrs.field(converter=convert)"
            ),
            "unsupported converter",
            "invalid-row-type",
        )
        for output, field in [("Any", "object"), ("object", "Any"), ("list[Any]", "list[object]")]
    ),
    (
        (
            "Alias = list[Any]\ndef convert(value: int) -> Annotated[Alias, 'out']: return []\n"
            "@attrs.define\nclass T:\n    value: Annotated[list[Any], 'field'] = attrs.field(converter=convert)"
        ),
        ("named", "validator", "by_name", (("value", ("value",), False, True),)),
        "ok",
    ),
    (
        (
            "type Inner = Annotated[int, 'inner']\ntype Items = list[Inner | None]\n"
            "def convert(value: int) -> Items: return []\n"
            "@attrs.define\nclass T:\n    value: list[int | None] = attrs.field(converter=convert)"
        ),
        ("named", "validator", "by_name", (("value", ("value",), False, True),)),
        "ok",
    ),
    *(
        (
            (
                "type Alias[T] = list[T]\n"
                f"def convert(value: int) -> {output}: return []\n"
                f"@attrs.define\nclass T:\n    value: {field} = attrs.field(converter=convert)"
            ),
            ("named", "validator", "by_name", (("value", ("value",), False, True),)),
            "ok",
        )
        for output, field in [
            ("Alias[int]", "list[int]"),
            ("list[int]", "Alias[int]"),
            ("list[Alias[Annotated[int, 'x']]]", "list[list[int]]"),
            ("list[list[int]]", "list[Alias[Annotated[int, 'x']]]"),
        ]
    ),
    (
        "import attr\nfield_spec = attr.ib(default=1, alias='external')\n@attrs.define\nclass T:\n    value: int = field_spec",
        ("named", "validator", "by_name", (("value", ("external",), False, False),)),
        "ok",
    ),
    (
        "spec = attrs.field(init=False, default=1)\n@attrs.define\nclass T:\n    visible: int = attrs.field()\n    hidden = spec",
        ("named", "validator", "by_name", (("visible", ("visible",), False, True),)),
        "ok",
    ),
    (
        "@attrs.define\nclass T:\n    value: Annotated[int, 'metadata']",
        ("named", "validator", "by_name", (("value", ("value",), False, True),)),
        "ok",
    ),
    (
        (
            "def convert(value: int) -> str: return str(value)\n"
            "@attrs.define\nclass T:\n    value: str = attrs.field(converter=attrs.Converter(convert))"
        ),
        ("named", "validator", "by_name", (("value", ("value",), False, True),)),
        "ok",
    ),
    (
        (
            "type Recursive = list[Recursive]\ndef convert(value: Recursive) -> str: return ''\n"
            "@attrs.define\nclass T:\n    value: str = attrs.field(converter=convert)"
        ),
        "deeper than",
        "unresolved-row-type",
    ),
    (
        (
            "def convert(value: list[Any]) -> str: return str(value)\n"
            "@attrs.define\nclass T:\n    value: str = attrs.field(converter=convert)"
        ),
        "unsupported converter",
        "invalid-row-type",
    ),
    (
        (
            "def convert(value: int) -> str: return str(value)\n"
            "@attrs.define\nclass T:\n    value: str = attrs.field(converter=attrs.converters.pipe(convert))"
        ),
        "unsupported converter",
        "invalid-row-type",
    ),
    (
        (
            "@overload\ndef convert(value: int) -> str: ...\n"
            "@overload\ndef convert(value: str) -> str: ...\n"
            "def convert(value: int) -> str: return str(value)\n"
            "@attrs.define\nclass T:\n    value: str = attrs.field(converter=convert)"
        ),
        "unsupported converter",
        "invalid-row-type",
    ),
    (
        (
            "@attrs.define(slots=False)\nclass Base:\n    first: int\n    second: int\n"
            "@attrs.define\nclass Child(Base):\n    first: int\n    third: int\nT = Child"
        ),
        (
            "named",
            "validator",
            "by_name",
            (
                ("second", ("second",), False, True),
                ("first", ("first",), False, True),
                ("third", ("third",), False, True),
            ),
        ),
        "ok",
    ),
    (
        (
            "@attrs.define(slots=False)\nclass Left:\n    a: int\n"
            "@attrs.define(slots=False)\nclass Right:\n    b: int\n"
            "@attrs.define(slots=False)\nclass Child(Left, Right):\n    c: int\nT = Child"
        ),
        (
            "named",
            "validator",
            "by_name",
            (("b", ("b",), False, True), ("a", ("a",), False, True), ("c", ("c",), False, True)),
        ),
        "ok",
    ),
    (
        "@attrs.define\nclass T:\n    _id: int\n    label: str = attrs.field(alias='name', default='x')",
        ("named", "validator", "by_name", (("_id", ("id",), False, True), ("label", ("name",), False, False))),
        "ok",
    ),
    (
        (
            "def convert(value: int | None) -> str:\n    return str(value)\n"
            "@attrs.frozen\nclass T:\n    value: str = attrs.field(converter=convert)"
        ),
        ("named", "validator", "by_name", (("value", ("value",), True, True),)),
        "ok",
    ),
    (
        (
            "@attrs.define\nclass Base[T]:\n    value: T\n"
            "@attrs.define(kw_only=True)\nclass Child(Base[int]):\n"
            "    ignored: int = attrs.field(init=False, default=1)\n    tag: str = attrs.field(factory=str)\nT = Child"
        ),
        ("named", "validator", "by_name", (("value", ("value",), False, True), ("tag", ("tag",), False, False))),
        "ok",
    ),
    (
        "@attrs.define\nclass T:\n    x: int = attrs.NOTHING\n    y: int = attrs.field(default=attrs.NOTHING)",
        ("named", "validator", "by_name", (("x", ("x",), False, True), ("y", ("y",), False, True))),
        "ok",
    ),
    (
        "@attrs.define\nclass T:\n    _id: int = attrs.field(alias=None)",
        ("named", "validator", "by_name", (("_id", ("id",), False, True),)),
        "ok",
    ),
    (
        (
            "@attrs.define\nclass T:\n    x: int = attrs.Factory(int)\n    y: int = attrs.field()\n"
            "    @y.default\n    def make_y(self) -> int: return 1"
        ),
        ("named", "validator", "by_name", (("x", ("x",), False, False), ("y", ("y",), False, False))),
        "ok",
    ),
]

ROWS: Final[list[Row]] = [
    *ATTRS_ROWS,
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
    ("type Row[X] = tuple[X, str]\nT = Row[int]", ("tuple", 2, (False, False)), "ok"),
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

TYPE_CHECKING_ALIAS: Final[str] = "if typing.TYPE_CHECKING:\n    from fractions import Fraction\ntype D = Fraction\n"
# the checker accepts the row but can't know the column names, so the runtime's names are not compared; every field's
# names must be unknown
UNKNOWN_NAMES: Final[str] = "ok-names-unknown"
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
        "ok",
    ),
    (
        "@dataclass(slots=True, frozen=True, kw_only=True)\nclass T:\n    a: int\n    b: int = 0",
        keyword(f("a"), f("b", required=False)),
        "ok",
    ),
    (
        "@dataclass\nclass T:\n    a: int\n    _: KW_ONLY\n    b: str\n    c: str = field(kw_only=True, default='')",
        keyword(f("a"), f("b"), f("c", required=False)),
        "ok",
    ),
    (
        (
            "@dataclass\nclass Base:\n    a: int\n    b: str\n@dataclass\nclass Mid(Base):\n    c: int\n"
            "@dataclass\nclass T(Mid):\n    b: str | None\n    d: bool = False"
        ),
        keyword(f("a"), f("b", nullable=True), f("c"), f("d", required=False)),
        "ok",
    ),
    (
        "@dataclass\nclass T:\n    a: int\n    c: ClassVar[int] = 0\n    d: int = field(init=False, default=1)",
        keyword(f("a")),
        "ok",
    ),
    (
        "@dataclass\nclass T:\n    a: Annotated[int, 'm']\n    b: Any\n    c: None\n    d: Interval | None\n    e: Range[int]",
        keyword(f("a"), f("b", nullable=True), f("c", nullable=True), f("d", nullable=True), f("e")),
        "ok",
    ),
    (
        "@dataclass\nclass T:\n    m: Money\n    p: Point\n    r: Range[datetime.date] | None\n    h: dict[str, str | None]",
        keyword(f("m"), f("p"), f("r", nullable=True), f("h")),
        "ok",
    ),
    ("@dataclass\nclass U:\n    id: int\ntype A = U\nT = A", keyword(f("id")), "ok"),
    # a hand-written `__init__` is a checker-only rejection: the runtime accepts it
    (
        "@dataclass\nclass T:\n    id: int\n    def __init__(self, id: int) -> None:\n        self.id = id",
        keyword(f("id")),
        CHECKER_ONLY,
    ),
    # an unknown decorator, a metaclass and an unknown class keyword are checker-only rejections too
    (
        "def deco(cls):\n    return cls\n@deco\n@dataclass\nclass T:\n    id: int",
        keyword(f("id")),
        CHECKER_ONLY,
    ),
    (
        "class Meta(type): ...\n@dataclass\nclass T(metaclass=Meta):\n    id: int",
        keyword(f("id")),
        CHECKER_ONLY,
    ),
    (
        (
            "class Base:\n    def __init_subclass__(cls, flag=False, **kwargs):\n        super().__init_subclass__(**kwargs)\n"
            "@dataclass\nclass T(Base, flag=True):\n    id: int"
        ),
        keyword(f("id")),
        CHECKER_ONLY,
    ),
    # typing.NamedTuple
    (
        "class T(NamedTuple):\n    id: int\n    name: str | None\n    note: str = ''",
        positional(f("id"), f("name", nullable=True), f("note", required=False)),
        "ok",
    ),
    (
        "T = NamedTuple('T', [('id', int), ('name', str | None)])",
        positional(f("id"), f("name", nullable=True)),
        "ok",
    ),
    (
        "class Base(NamedTuple):\n    id: int\nclass T(Base):\n    extra: str",
        positional(f("id")),
        "ok",
    ),
    # TypedDict
    (
        "class T(TypedDict):\n    id: int\n    name: str | None\n    note: NotRequired[str]",
        dict_form(f("id"), f("name", nullable=True), f("note", required=False)),
        "ok",
    ),
    (
        "class T(TypedDict, total=False):\n    a: int\n    b: Required[str]\n    c: ReadOnly[int]",
        dict_form(f("a", required=False), f("b"), f("c", required=False)),
        "ok",
    ),
    (
        "T = TypedDict('T', {'id': int, 'name': str | None})",
        dict_form(f("id"), f("name", nullable=True)),
        "ok",
    ),
    (
        "class T(typing_extensions.TypedDict):\n    id: int\n    n: typing_extensions.NotRequired[str]",
        dict_form(f("id"), f("n", required=False)),
        "ok",
    ),
    (
        (
            "class Base(TypedDict):\n    a: int\n    b: str\nclass Mid(Base, total=False):\n    c: int\n"
            "class T(Mid):\n    b: str | None\n    d: int"
        ),
        dict_form(f("a"), f("b", nullable=True), f("c", required=False), f("d")),
        "ok",
    ),
    # quoted qualifiers are missed by `__required_keys__`; requiredness follows the resolved annotation
    (
        "class T(TypedDict):\n    id: int\n    n: 'NotRequired[str]'\n    m: NotRequired['str']",
        dict_form(f("id"), f("n", required=False), f("m", required=False)),
        "ok",
    ),
    (
        "class T(TypedDict, total=False):\n    id: int\n    n: 'Required[str]'\n    m: 'Required[ReadOnly[int]]'",
        dict_form(f("id", required=False), f("n"), f("m")),
        "ok",
    ),
    (
        "class T(typing_extensions.TypedDict):\n    n: 'typing_extensions.NotRequired[str]'\n    m: int",
        dict_form(f("n", required=False), f("m")),
        "ok",
    ),
    # pydantic models and dataclasses
    (
        "class T(BaseModel):\n    id: int\n    name: str | None\n    tags: list[str] = []",
        validator(f("id"), f("name", nullable=True), f("tags", required=False)),
        "ok",
    ),
    (
        "class T(BaseModel):\n    id: int = Field(alias='user_id')\n    n: str = Field(validation_alias='nm', alias='x')",
        validator(("id", ("user_id",), False, True), ("n", ("nm",), False, True)),
        "ok",
    ),
    (
        "class T(BaseModel, populate_by_name=True):\n    id: int = Field(alias='user_id')\n    n: str",
        validator(("id", ("user_id", "id"), False, True), f("n")),
        "ok",
    ),
    (
        (
            "class T(BaseModel):\n    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)\n"
            "    id: int = Field(alias='user_id')"
        ),
        validator(f("id")),
        "ok",
    ),
    (
        "class T(BaseModel):\n    id: int = Field(validation_alias=AliasChoices('a', 'b'))\n    n: str",
        validator(("id", None, False, True), f("n")),
        "ok",  # the checker adds `note: names not checked` for the field
    ),
    (
        (
            "class T(BaseModel):\n    id: int\n    n: str\n    @model_validator(mode='before')\n    @classmethod\n"
            "    def pre(cls, data):\n        return data"
        ),
        validator(("id", None, False, True), ("n", None, False, True)),
        "ok",  # the checker adds `note: names not checked` for the model
    ),
    (
        "class T(BaseModel):\n    model_config = ConfigDict(alias_generator=lambda name: name.upper())\n    id: int",
        validator(("id", ("ID",), False, True)),
        # the checker can't run the generator, so its names are unknown; the runtime sees the generated aliases
        UNKNOWN_NAMES,
    ),
    (
        "@pydantic.dataclasses.dataclass(config=ConfigDict(strict=True))\nclass T:\n    id: int\n    name: str | None",
        validator(f("id"), f("name", nullable=True)),
        "ok",
    ),
    (
        "class T(BaseModel):\n    id: int\n    count: int = Field(init=False)",
        validator(f("id"), f("count")),
        "ok",  # `init=False` excludes a field only on a pydantic dataclass
    ),
    (
        "class T(BaseModel, populate_by_name=True, validate_by_name=False):\n    id: int = Field(alias='user_id')",
        validator(("id", ("user_id",), False, True)),
        "ok",
    ),
    # config merges through a base, and the child wins; the decorator's `config=` is read too (`class Config` is deprecated, so it is unit-tested only)
    (
        "class Base(BaseModel, populate_by_name=True):\n    id: int = Field(alias='u')\nclass T(Base):\n    n: str",
        validator(("id", ("u", "id"), False, True), f("n")),
        "ok",
    ),
    (
        (
            "class Base(BaseModel):\n    model_config = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')\n"
            "class T(Base):\n    model_config = ConfigDict(validate_by_name=False)"
        ),
        validator(("id", ("u",), False, True)),
        "ok",
    ),
    (
        "class T(BaseModel):\n    model_config = ConfigDict(validate_by_alias=False)\n    id: int = Field(alias='u')",
        validator(f("id")),
        "ok",
    ),
    (
        (
            "class T(BaseModel):\n    model_config = ConfigDict(populate_by_name=True, validate_by_alias=False)\n"
            "    id: int = Field(alias='u')"
        ),
        validator(("id", ("u", "id"), False, True)),
        "ok",
    ),
    (
        "@pydantic.dataclasses.dataclass(config=ConfigDict(populate_by_name=True))\nclass T:\n    id: int = Field(alias='u')",
        validator(("id", ("u", "id"), False, True)),
        "ok",
    ),
    # a before-validator in a base, a plain mixin's annotation, and an after-validator, which doesn't hide the names
    (
        (
            "class Base(BaseModel):\n    @model_validator(mode='before')\n    @classmethod\n"
            "    def pre(cls, data):\n        return data\nclass T(Base):\n    id: int"
        ),
        validator(("id", None, False, True)),
        "ok",
    ),
    (
        "class T(BaseModel):\n    id: int\n    @model_validator(mode='after')\n    def check(self):\n        return self",
        validator(f("id")),
        "ok",
    ),
    ("class Mixin:\n    a: int\nclass T(Mixin, BaseModel):\n    b: str", validator(f("a"), f("b")), "ok"),
    # a literal `init=False` is the only decorator `init` pydantic takes, and excludes nothing
    ("@pydantic.dataclasses.dataclass(init=False)\nclass T:\n    id: int", validator(f("id")), "ok"),
    (
        "@pydantic.dataclasses.dataclass\nclass T:\n    id: int\n    count: int = Field(init=False, default=0)",
        validator(f("id")),
        "ok",
    ),
    (
        "class T(BaseModel):\n    id: int\n    count: Annotated[int, Field(default=1, alias='c')]",
        validator(f("id"), ("count", ("c",), False, False)),
        "ok",
    ),
    # the checker can't read a non-literal class keyword or a config that isn't `ConfigDict(...)`, and the runtime can
    (
        "FLAG = True\nclass T(BaseModel, populate_by_name=FLAG):\n    id: int = Field(alias='u')",
        validator(("id", ("u", "id"), False, True)),
        UNKNOWN_NAMES,
    ),
    (
        "class T(BaseModel):\n    model_config = {'populate_by_name': True}\n    id: int = Field(alias='u')",
        validator(("id", ("u", "id"), False, True)),
        UNKNOWN_NAMES,
    ),
    # pydantic marks only the decorated class, so the runtime plans an undecorated subclass as a plain dataclass
    (
        ("@pydantic.dataclasses.dataclass\nclass Base:\n    id: int\nclass T(Base): ..."),
        keyword(f("id")),
        CHECKER_ONLY,
    ),
    # pydantic merges the direct bases' configs from the first to the last, so the last base wins
    (
        (
            "class A(BaseModel, populate_by_name=True): ...\nclass B(BaseModel, populate_by_name=False): ...\n"
            "class T(A, B):\n    id: int = Field(alias='u')"
        ),
        validator(("id", ("u",), False, True)),
        "ok",
    ),
    (
        (
            "class A(BaseModel, populate_by_name=True): ...\nclass B(BaseModel, populate_by_name=False): ...\n"
            "class T(B, A):\n    id: int = Field(alias='u')"
        ),
        validator(("id", ("u", "id"), False, True)),
        "ok",
    ),
    (
        (
            "class A(BaseModel, populate_by_name=True): ...\nclass B(BaseModel, validate_by_alias=True): ...\n"
            "class T(A, B, populate_by_name=False):\n    id: int = Field(alias='u')"
        ),
        validator(("id", ("u",), False, True)),
        "ok",
    ),
    # `Field(...)` metadata is read through an alias (not through a `type` statement, whose attributes pydantic drops with a warning, so that is unit-tested only)
    (
        "Id = Annotated[int, Field(alias='user_id')]\nclass T(BaseModel):\n    id: Id",
        validator(("id", ("user_id",), False, True)),
        "ok",
    ),
    (
        "from typing import TypeAlias\nId: TypeAlias = Annotated[int, Field(alias='user_id')]\nclass T(BaseModel):\n    id: Id",
        validator(("id", ("user_id",), False, True)),
        "ok",
    ),
    (
        "Id = Annotated[int, Field(alias='user_id')]\nclass T(BaseModel):\n    id: 'Id'",
        validator(("id", ("user_id",), False, True)),
        "ok",
    ),
    (
        "Id = Annotated[int, Field(alias='user_id')]\nclass T(BaseModel):\n    id: Annotated[Id, Field(gt=0)]",
        validator(("id", ("user_id",), False, True)),
        "ok",
    ),
    (
        "Id = Annotated[int, Field(default=5)]\nclass T(BaseModel):\n    id: Id",
        validator(("id", ("id",), False, False)),
        "ok",
    ),
    (
        "Id = Annotated[int, Field(alias='user_id')]\n@pydantic.dataclasses.dataclass\nclass T:\n    id: Id",
        validator(("id", ("user_id",), False, True)),
        "ok",
    ),
    # a `ClassVar` or `KW_ONLY` behind an alias or `Annotated` is no field of a pydantic class
    ("CV = ClassVar[int]\nclass T(BaseModel):\n    a: int\n    b: CV", validator(f("a")), "ok"),
    (
        "from typing import TypeAlias\nCV: TypeAlias = ClassVar[int]\nclass T(BaseModel):\n    a: int\n    b: CV",
        validator(f("a")),
        "ok",
    ),
    ("class T(BaseModel):\n    a: int\n    b: Annotated[ClassVar[int], 'm']", validator(f("a")), "ok"),
    (
        "CV = ClassVar[int]\n@pydantic.dataclasses.dataclass\nclass T:\n    a: int\n    b: CV",
        validator(f("a")),
        "ok",
    ),
    (
        "@pydantic.dataclasses.dataclass\nclass T:\n    a: int\n    b: Annotated[ClassVar[int], 'm']",
        validator(f("a")),
        "ok",
    ),
    (
        "K = KW_ONLY\n@pydantic.dataclasses.dataclass\nclass T:\n    a: int\n    _: K\n    b: int",
        validator(f("a"), f("b")),
        "ok",
    ),
    # pydantic follows an alias of `Annotated` to find a `ClassVar`
    (
        "A = Annotated\nclass T(BaseModel):\n    a: int\n    b: A[ClassVar[int], 'm']",
        validator(f("a")),
        "ok",
    ),
    ("CV = ClassVar[int]\n@dataclass\nclass T:\n    a: int\n    b: CV = 1", keyword(f("a")), "ok"),
    # pydantic registers a before-validator of a plain mixin, and an assigned one, and runs a conditional block
    (
        (
            "class Mixin:\n    @model_validator(mode='before')\n    @classmethod\n    def pre(cls, data):\n"
            "        return data\nclass T(BaseModel, Mixin):\n    id: int"
        ),
        validator(("id", None, False, True)),
        UNKNOWN_NAMES,
    ),
    (
        (
            "class Mixin:\n    @model_validator(mode='before')\n    @classmethod\n    def pre(cls, data):\n"
            "        return data\nclass T(Mixin, BaseModel):\n    id: int"
        ),
        validator(("id", None, False, True)),
        UNKNOWN_NAMES,
    ),
    (
        (
            "def g(cls, data):\n    return data\nclass T(BaseModel):\n"
            "    f = model_validator(mode='before')(classmethod(g))\n    id: int"
        ),
        validator(("id", None, False, True)),
        UNKNOWN_NAMES,
    ),
    (
        (
            "def g(cls, data, handler):\n    return data\nclass T(BaseModel):\n"
            "    f = model_validator(mode='wrap')(classmethod(g))\n    id: int"
        ),
        validator(("id", None, False, True)),
        UNKNOWN_NAMES,
    ),
    (
        ("def g(self):\n    return self\nclass T(BaseModel):\n    f = model_validator(mode='after')(g)\n    id: int"),
        validator(f("id")),
        "ok",
    ),
    (
        (
            "class T(BaseModel):\n    if typing.TYPE_CHECKING or True:\n"
            "        model_config = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')"
        ),
        validator(("id", ("u", "id"), False, True)),
        UNKNOWN_NAMES,
    ),
    (
        (
            "class T(BaseModel):\n    try:\n        @model_validator(mode='before')\n        @classmethod\n"
            "        def pre(cls, data):\n            return data\n    except Exception:\n        pass\n    id: int"
        ),
        validator(("id", None, False, True)),
        UNKNOWN_NAMES,
    ),
    (
        (
            "class T(BaseModel):\n    if typing.TYPE_CHECKING:\n        pass\n    else:\n"
            "        model_config = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')"
        ),
        validator(("id", ("u", "id"), False, True)),
        "ok",
    ),
    # an `= ...` value clears an `Annotated` default on a model, but not a `default_factory` or a dataclass's default
    ("class T(BaseModel):\n    a: Annotated[int, Field(default=3)] = ...", validator(f("a")), "ok"),
    (
        "class T(BaseModel):\n    a: Annotated[int, Field(default_factory=int)] = ...",
        validator(f("a", required=False)),
        "ok",
    ),
    (
        "@pydantic.dataclasses.dataclass\nclass T:\n    a: Annotated[int, Field(default=3)] = ...",
        validator(f("a", required=False)),
        "ok",
    ),
    # a redeclaration without a value takes the parent's class attribute: a `Field(...)` without a default is kept (its
    # alias with it), and one with a default is that plain default
    (
        (
            "@pydantic.dataclasses.dataclass\nclass P:\n    b: int = Field(alias='B')\n"
            "@pydantic.dataclasses.dataclass\nclass T(P):\n    b: int"
        ),
        validator(("b", ("B",), False, True)),
        "ok",
    ),
    (
        (
            "@pydantic.dataclasses.dataclass\nclass P:\n    b: int = Field(alias='B')\n"
            "@pydantic.dataclasses.dataclass\nclass T(P):\n    b: Annotated[int, Field(alias='C')]"
        ),
        validator(("b", ("B",), False, True)),
        "ok",
    ),
    (
        (
            "@pydantic.dataclasses.dataclass\nclass P:\n    b: int = Field(alias='B', default_factory=int)\n"
            "@pydantic.dataclasses.dataclass\nclass T(P):\n    b: int"
        ),
        validator(("b", ("B",), False, False)),
        "ok",
    ),
    (
        (
            "@pydantic.dataclasses.dataclass\nclass P:\n    b: int = Field(alias='B', default=1)\n"
            "@pydantic.dataclasses.dataclass\nclass T(P):\n    b: int"
        ),
        validator(("b", ("b",), False, False)),
        "ok",
    ),
    (
        (
            "@pydantic.dataclasses.dataclass\nclass P:\n    b: int = Field(alias='B')\n"
            "@pydantic.dataclasses.dataclass\nclass Q(P):\n    b: int\n"
            "@pydantic.dataclasses.dataclass\nclass T(Q):\n    b: int"
        ),
        validator(("b", ("B",), False, True)),
        "ok",
    ),
    # the last of two `model_config` assignments replaces the first
    (
        (
            "class T(BaseModel):\n    model_config = ConfigDict(populate_by_name=True)\n"
            "    model_config = ConfigDict(validate_by_alias=True)\n    id: int = Field(alias='u')"
        ),
        validator(("id", ("u",), False, True)),
        "ok",
    ),
    # the runtime accepts these mixed forms, and the checker rejects them for good (a design follow-up)
    (
        "@dataclass\nclass D:\n    x: int = 1\nclass T(BaseModel, D):\n    id: int",
        validator(f("x", required=False), f("id")),
        CHECKER_ONLY,
    ),
    (
        "@pydantic.dataclasses.dataclass\nclass P:\n    x: int\n@dataclass\nclass T(P):\n    id: int",
        keyword(f("x"), f("id")),
        CHECKER_ONLY,
    ),
    (
        "@dataclass\nclass S:\n    x: int\n@pydantic.dataclasses.dataclass\nclass T(S):\n    id: int",
        validator(f("x"), f("id")),
        CHECKER_ONLY,
    ),
    ("class T(BaseModel):\n    id: PositiveInt", validator(f("id")), CHECKER_ONLY),
    ("class T(BaseModel):\n    j: pydantic.Json[list[int]]", validator(f("j")), CHECKER_ONLY),
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
    # a generic class that is never subscripted has an unbound parameter
    ("@dataclass\nclass T[X]:\n    a: int", "InterfaceError", REJECTED),
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
    # a name imported only under `TYPE_CHECKING` stays unavailable through a `type` alias, which reads its value lazily
    *[
        (f"{TYPE_CHECKING_ALIAS}{body}", "InterfaceError", REJECTED)
        for body in (
            "@dataclass\nclass T:\n    a: D",
            "@dataclass\nclass T:\n    a: list[D]",
            "@dataclass\nclass T:\n    a: D | None",
            "@dataclass\nclass T:\n    a: Optional[D]",
            "@dataclass\nclass T:\n    a: Annotated[D, 'm']",
            "type E = D\n@dataclass\nclass T:\n    a: E",
            "type E = list[D]\n@dataclass\nclass T:\n    a: E",
            "class T(NamedTuple):\n    a: D",
            "class T(NamedTuple):\n    a: list[D]",
            "class T(TypedDict):\n    a: D",
            "class T(TypedDict):\n    a: NotRequired[D]",
        )
    ],
    # a row-level `type` alias of a name imported only under `TYPE_CHECKING` is unavailable at runtime too
    (
        "if typing.TYPE_CHECKING:\n    from decimal import Decimal as D0\ntype T = D0",
        "InterfaceError",
        REJECTED,
    ),
    # the body of `if TYPE_CHECKING:` in a class never runs, and its `else:` body always does
    ("@dataclass\nclass T:\n    a: int\n    if typing.TYPE_CHECKING:\n        b: int", keyword(f("a")), "ok"),
    (
        "@dataclass\nclass T:\n    a: int\n    if typing.TYPE_CHECKING:\n        ...\n    else:\n        b: int = 1",
        keyword(f("a"), f("b", required=False)),
        "ok",
    ),
    # a redeclared field takes its default from the class attribute of that name found along the MRO
    *[
        (source, keyword(f("x", required=False)), "ok")
        for source in (
            "@dataclass\nclass Base:\n    x: int = 1\n@dataclass\nclass T(Base):\n    x: int",
            "@dataclass\nclass Base:\n    x: int = field(default=1)\n@dataclass\nclass T(Base):\n    x: int",
            "@dataclass\nclass Base:\n    x: int = 1\nclass Mid(Base):\n    x: int\n@dataclass\nclass T(Mid):\n    x: int",
            "class Mixin:\n    x = 9\n@dataclass\nclass T(Mixin):\n    x: int",
            "@dataclass\nclass A:\n    x: int = 1\n@dataclass\nclass B:\n    x: int\n@dataclass\nclass T(A, B):\n    x: int",
            "@dataclass\nclass T:\n    x: int\n    x = 5",
            "@dataclass\nclass A:\n    x: int\n@dataclass\nclass B:\n    x: int = 1\n@dataclass\nclass T(A, B):\n    x: int",
            "class Mixin:\n    def x(self): ...\n@dataclass\nclass T(Mixin):\n    x: int",
        )
    ],
    *[
        (source, keyword(f("x")), "ok")
        for source in (
            "@dataclass\nclass Base:\n    x: int = field(default_factory=int)\n@dataclass\nclass T(Base):\n    x: int",
            "@dataclass\nclass Base:\n    x: int = field(kw_only=True)\n@dataclass\nclass T(Base):\n    x: int",
        )
    ],
    # the fields of an undecorated subclass are the first dataclass's in the MRO, not a union of the bases
    (
        "@dataclass\nclass A:\n    a: int\n@dataclass\nclass B:\n    b: int\nclass T(A, B): ...",
        keyword(f("a")),
        "ok",
    ),
    (
        "@dataclass\nclass A:\n    a: int\nclass Mid(A): ...\n@dataclass\nclass B:\n    b: int\nclass T(Mid, B): ...",
        keyword(f("a")),
        "ok",
    ),
    # the fields are collected along the real C3 linearization, not the bases in the order written
    (
        (
            "@dataclass\nclass C0:\n    a: int\n    e: int\n@dataclass\nclass C1:\n    d: int\n    b: int\n    c: int\n"
            "@dataclass\nclass C2:\n    e: int\n@dataclass\nclass C3(C2, C1, C0):\n    a: int\n"
            "@dataclass\nclass C4(C3): ...\n@dataclass\nclass T(C3, C2): ..."
        ),
        keyword(f("a"), f("e"), f("d"), f("b"), f("c")),
        "ok",
    ),
    (
        (
            "@dataclass\nclass A:\n    a: int\n@dataclass\nclass B(A):\n    b: int\n@dataclass\nclass C(A):\n    c: int\n"
            "@dataclass\nclass T(B, C):\n    d: int"
        ),
        keyword(f("a"), f("c"), f("b"), f("d")),
        "ok",
    ),
    # the runtime accepts a base from outside the project, and the checker rejects it: it only follows project classes
    ("import abc\n@dataclass\nclass T(abc.ABC):\n    id: int", keyword(f("id")), CHECKER_ONLY),
    ("@dataclass\nclass T(Exception):\n    id: int", keyword(f("id")), CHECKER_ONLY),
    # a field declared in a conditional block is a field at runtime, and the checker can't tell whether it is
    ("@dataclass\nclass T:\n    a: int\n    if True:\n        b: int", keyword(f("a"), f("b")), CHECKER_ONLY),
    (
        "class T(TypedDict):\n    a: int\n    try:\n        b: int\n    except ImportError:\n        pass",
        dict_form(f("a"), f("b")),
        CHECKER_ONLY,
    ),
    # a private name is stored under its mangled name, which is what the row's field is called
    ("@dataclass\nclass T:\n    __x: int\n    _y: int", keyword(f("_T__x"), f("_y")), "ok"),
    ("class T(TypedDict):\n    __x: int", dict_form(f("_T__x")), "ok"),
]


def alias_chain(length: int, body: str, final: str) -> str:
    # `length` generic aliases, each defined by the one before it, the first by `body`
    lines = [f"type A0[X] = {body}"]
    lines += [f"type A{index}[X] = A{index - 1}[X]" for index in range(1, length)]
    return "\n".join(lines) + f"\nT = A{length - 1}[{final}]"


CRATE: Final[str] = "@dataclass\nclass Crate[X]:\n    a: X\n    b: str\n"
GENERIC_ROWS: Final[list[Row]] = [
    # generic aliases: scalar, tuple, nested, and the order the arguments are used in
    ("type Opt[X] = X | None\nT = Opt[int]", ("scalar", 1, (True,)), "ok"),
    ("type Swap[A, B] = tuple[B, A]\nT = Swap[int, str | None]", ("tuple", 2, (True, False)), "ok"),
    ("type Pair[A, B] = tuple[A, B]\nT = Pair[int, str | None]", ("tuple", 2, (False, True)), "ok"),
    ("type Nested[X] = tuple[list[X], X | None]\nT = Nested[int]", ("tuple", 2, (False, True)), "ok"),
    (
        "type Inner[X] = tuple[X, str]\ntype Outer[Y] = Inner[Y | None]\nT = Outer[int]",
        ("tuple", 2, (True, False)),
        "ok",
    ),
    ("type Row[A, B = str | None] = tuple[A, B]\nT = Row[int]", ("tuple", 2, (False, True)), "ok"),
    ("type Row[X] = tuple[X, str]\nT = Row", "InterfaceError", REJECTED),
    ("type Row[X] = tuple[X, str]\nT = Row[int, str]", "InterfaceError", REJECTED),
    ("type Row[X] = tuple[X, str]\nT = Row[()]", "InterfaceError", REJECTED),
    ("type Two[A, B] = tuple[A, B]\nT = Two[int]", "InterfaceError", REJECTED),
    ("type Two[A, B = int] = tuple[A, B]\nT = Two[int, str, bytes]", "InterfaceError", REJECTED),
    ("type Row[*Ts] = tuple[int, str]\nT = Row[int]", "InterfaceError", REJECTED),
    ("type Row[**P] = tuple[int, str]\nT = Row[[int]]", "InterfaceError", REJECTED),
    ("type Row[X] = tuple[X, str]\nT = Row[Row[int]]", "InterfaceError", REJECTED),
    # one hop is one alias (or class) substitution along a chain: 16 are followed, and 17 are not
    (alias_chain(16, "tuple[X, str]", "int"), ("tuple", 2, (False, False)), "ok"),
    (alias_chain(17, "tuple[X, str]", "int"), "InterfaceError", REJECTED),
    (alias_chain(15, "Crate[X]", "int") + "\n" + CRATE, keyword(f("a"), f("b")), "ok"),
    (CRATE + alias_chain(16, "Crate[X]", "int"), "InterfaceError", REJECTED),
]
NAMED_ROWS.extend([
    # generic dataclasses with one and with two parameters, in PEP 695 and `Generic[X]` form
    (CRATE + "T = Crate[int | None]", keyword(f("a", nullable=True), f("b")), "ok"),
    (
        "@dataclass\nclass Crate[A, B]:\n    a: A\n    b: list[B]\nT = Crate[int, str | None]",
        keyword(f("a"), f("b")),
        "ok",
    ),
    # a parameter that is named like the row alias still reads as the parameter inside the class
    ("@dataclass\nclass Crate[T]:\n    a: T\nT = Crate[int]", keyword(f("a")), "ok"),
    (
        "X = TypeVar('X')\n@dataclass\nclass Crate(typing.Generic[X]):\n    a: X\nT = Crate[int | None]",
        keyword(f("a", nullable=True)),
        "ok",
    ),
    (
        (
            "A = TypeVar('A')\nB = TypeVar('B')\n@dataclass\nclass Crate(typing.Generic[A, B]):\n    a: B\n    b: A\n"
            "T = Crate[int, str | None]"
        ),
        keyword(f("a", nullable=True), f("b")),
        "ok",
    ),
    ("@dataclass\nclass Crate[X]:\n    a: 'X | None'\nT = Crate[int]", keyword(f("a", nullable=True)), "ok"),
    (
        "type Opt[Y] = Y | None\n@dataclass\nclass Crate[X]:\n    a: Opt[X]\nT = Crate[int]",
        keyword(f("a", nullable=True)),
        "ok",
    ),
    ("@dataclass\nclass Crate[X]:\n    a: Annotated[X, 'm']\nT = Crate[int]", keyword(f("a")), "ok"),
    # a generic NamedTuple and TypedDict
    ("class Crate[X](NamedTuple):\n    a: X\n    b: str\nT = Crate[int]", positional(f("a"), f("b")), "ok"),
    (
        "class Crate[A, B](NamedTuple):\n    a: A\n    b: B = None\nT = Crate[int, str | None]",
        positional(f("a"), f("b", nullable=True, required=False)),
        "ok",
    ),
    (
        "class Crate[X](TypedDict):\n    a: X\n    b: NotRequired[str]\nT = Crate[int | None]",
        dict_form(f("a", nullable=True), f("b", required=False)),
        "ok",
    ),
    (
        "class Crate[A, B](TypedDict):\n    a: A\n    b: B\nT = Crate[int, str | None]",
        dict_form(f("a"), f("b", nullable=True)),
        "ok",
    ),
    # generic pydantic models and pydantic dataclasses; a subscripted model is already a concrete subclass
    ("class Page[X](BaseModel):\n    a: X\n    b: str\nT = Page[int]", validator(f("a"), f("b")), "ok"),
    (
        "class Page[A, B](BaseModel):\n    a: A\n    b: B\nT = Page[int, str | None]",
        validator(f("a"), f("b", nullable=True)),
        "ok",
    ),
    (
        "X = TypeVar('X')\nclass Page(BaseModel, typing.Generic[X]):\n    a: X\nT = Page[int | None]",
        validator(f("a", nullable=True)),
        "ok",
    ),
    (
        "@pydantic.dataclasses.dataclass\nclass Crate[X]:\n    a: X\n    b: str\nT = Crate[int]",
        validator(f("a"), f("b")),
        "ok",
    ),
    (
        "@pydantic.dataclasses.dataclass\nclass Crate[A, B]:\n    a: A\n    b: B\nT = Crate[int, str | None]",
        validator(f("a"), f("b", nullable=True)),
        "ok",
    ),
    # inherited parameters are rebound through the base's arguments
    (
        "@dataclass\nclass Base[X]:\n    a: X\n@dataclass\nclass Child[U](Base[U]):\n    b: U | None\nT = Child[int]",
        keyword(f("a"), f("b", nullable=True)),
        "ok",
    ),
    (
        (
            "@dataclass\nclass Base[A, B]:\n    a: A\n    b: B\n@dataclass\nclass Child[U](Base[U | None, str]):\n    c: U\n"
            "T = Child[int]"
        ),
        keyword(f("a", nullable=True), f("b"), f("c")),
        "ok",
    ),
    (
        (
            "@dataclass\nclass Base[X]:\n    a: X\n@dataclass\nclass Mid[Y](Base[Y | None]):\n    b: Y\n"
            "@dataclass\nclass Child[U](Mid[U]):\n    c: U\nT = Child[int]"
        ),
        keyword(f("a", nullable=True), f("b"), f("c")),
        "ok",
    ),
    (
        (
            "A = TypeVar('A')\n@dataclass\nclass Base(typing.Generic[A]):\n    a: A\n@dataclass\nclass Child(Base[int | None]):\n    b: str\n"
            "T = Child"
        ),
        keyword(f("a", nullable=True), f("b")),
        "ok",
    ),
    (
        "class Base[X](TypedDict):\n    a: X\nclass Child[U](Base[U | None], TypedDict):\n    b: U\nT = Child[int]",
        dict_form(f("a", nullable=True), f("b")),
        "ok",
    ),
    (
        "class Base[X](BaseModel):\n    a: X\nclass Child[U](Base[U | None]):\n    b: U\nT = Child[int]",
        validator(f("a", nullable=True), f("b")),
        "ok",
    ),
    (
        (
            "@pydantic.dataclasses.dataclass\nclass Base[X]:\n    a: X\n@pydantic.dataclasses.dataclass\nclass Child[U](Base[U | None]):\n"
            "    b: U\nT = Child[int]"
        ),
        validator(f("a", nullable=True), f("b")),
        "ok",
    ),
    # PEP 696 defaults make a bare generic class usable (a bare alias is left to the unit tests: the type checkers
    # report it unsolved)
    ("@dataclass\nclass Crate[X = int]:\n    a: X\nT = Crate", keyword(f("a")), "ok"),
    ("@dataclass\nclass Crate[X = int | None]:\n    a: X\nT = Crate", keyword(f("a", nullable=True)), "ok"),
    (
        "@dataclass\nclass Crate[A, B = str | None]:\n    a: A\n    b: B\nT = Crate[int]",
        keyword(f("a"), f("b", nullable=True)),
        "ok",
    ),
    ("@dataclass\nclass Crate[A = int, B = list[A]]:\n    a: A\n    b: B\nT = Crate", keyword(f("a"), f("b")), "ok"),
    (
        "X = TypeVar('X', default=int)\n@dataclass\nclass Crate(typing.Generic[X]):\n    a: X\nT = Crate",
        keyword(f("a")),
        "ok",
    ),
    ("class Page[X = int](BaseModel):\n    a: X\nT = Page", validator(f("a")), "ok"),
    ("class Crate[X = int](NamedTuple):\n    a: X\nT = Crate", positional(f("a")), "ok"),
    ("class Crate[X = int](TypedDict):\n    a: X\nT = Crate", dict_form(f("a")), "ok"),
    ("@pydantic.dataclasses.dataclass\nclass Crate[X = int]:\n    a: X\nT = Crate", validator(f("a")), "ok"),
    # a generic alias of a generic class, and a generic class used through an alias
    (CRATE + "type B[Y] = Crate[Y]\nT = B[int | None]", keyword(f("a", nullable=True), f("b")), "ok"),
    (CRATE + "type B = Crate[int]\nT = B", keyword(f("a"), f("b")), "ok"),
    (CRATE + "type B[Y] = Crate[Y]\nT = B", "InterfaceError", REJECTED),
    (CRATE + "Alias = Crate\nT = Alias[int]", keyword(f("a"), f("b")), "ok"),
    # the rejections: an unbound parameter, a TypeVarTuple and a ParamSpec (Python rejects a wrong argument count to a
    # class itself, so only the checker sees one; the alias rows above cover it on both sides)
    (CRATE + "T = Crate", "InterfaceError", REJECTED),
    ("@dataclass\nclass Crate[*Ts]:\n    a: int\nT = Crate[int]", "InterfaceError", REJECTED),
    ("@dataclass\nclass Crate[**P]:\n    a: int\nT = Crate[[int]]", "InterfaceError", REJECTED),
    ("X = TypeVar('X')\n@dataclass\nclass Crate:\n    a: X\nT = Crate", "InterfaceError", REJECTED),
    ("class Page[X](BaseModel):\n    a: X\nT = Page", "InterfaceError", REJECTED),
    ("@pydantic.dataclasses.dataclass\nclass Crate[X]:\n    a: X\nT = Crate", "InterfaceError", REJECTED),
    ("class Crate[X](NamedTuple):\n    a: X\nT = Crate", "InterfaceError", REJECTED),
    ("class Crate[X](TypedDict):\n    a: X\nT = Crate", "InterfaceError", REJECTED),
    (
        "@dataclass\nclass Base[X]:\n    a: X\n@dataclass\nclass Child(Base):\n    b: int\nT = Child",
        "InterfaceError",
        REJECTED,
    ),
    # a substituted field is checked like any other: a named type or a bad leaf can't be an argument
    (CRATE + "@dataclass\nclass U:\n    id: int\nT = Crate[U]", "InterfaceError", REJECTED),
    (CRATE + "T = Crate[dict[str, int]]", "InterfaceError", REJECTED),
    (CRATE + "T = Crate[int | str]", "InterfaceError", REJECTED),
    (CRATE + "T = Crate[None]", keyword(f("a", nullable=True), f("b")), "ok"),
])
ROWS.extend(GENERIC_ROWS)
ROWS.extend(NAMED_ROWS)


def class_chain(length: int, *, fields_everywhere: bool) -> str:
    # `length` generic dataclasses, each subscripting the one before it, so the row `T = K<length>[int]` is `length`
    # subscripted bases deep; the fields are on the row's own class only, or on every class
    lines = ["@dataclass\nclass K0[X]:\n    a0: X"]
    for index in range(1, length + 1):
        body = f"    a{index}: X" if fields_everywhere or index == length else "    pass"
        lines.append(f"@dataclass\nclass K{index}[X](K{index - 1}[X]):\n{body}")
    return "\n".join(lines) + f"\nT = K{length}[int]"


def legacy_chain(length: int, *, restate: bool) -> str:
    # the same chain written with `typing.Generic`: `Generic[X]` is a base that adds no hop, restated or not
    lines = ["X = TypeVar('X')\n@dataclass\nclass K0(typing.Generic[X]):\n    a0: X"]
    for index in range(1, length + 1):
        bases = f"K{index - 1}[X], typing.Generic[X]" if restate else f"K{index - 1}[X]"
        lines.append(f"@dataclass\nclass K{index}({bases}):\n    a{index}: X")
    return "\n".join(lines) + f"\nT = K{length}[int]"


def chain_fields(length: int, *, fields_everywhere: bool) -> NamedPlan:
    keys = [f"a{index}" for index in range(length + 1)] if fields_everywhere else ["a0", f"a{length}"]
    return keyword(*(f(key) for key in keys))


# the type parameters of a legacy generic class are its bases' type variables in the order `typing` collects them, which
# is depth first by first appearance; each row binds `int | None` to the first and `str` to the second, so a field
# typed by the first is the one that is nullable
LEGACY_BASE: Final[str] = (
    "A = TypeVar('A')\nB = TypeVar('B')\nC = TypeVar('C')\n"
    "@dataclass\nclass Base(typing.Generic[A, B]):\n    a: A\n    b: B\n"
)
TYPE_VARIABLE_ROWS: Final[list[Row]] = [
    (
        LEGACY_BASE + "@dataclass\nclass Child(Base[list[A], B]):\n    c: A\n    d: B\nT = Child[int | None, str]",
        keyword(f("a"), f("b"), f("c", nullable=True), f("d")),
        "ok",
    ),
    (
        LEGACY_BASE + "@dataclass\nclass Child(Base[B | None, A]):\n    c: A\n    d: B\nT = Child[int | None, str]",
        keyword(f("a", nullable=True), f("b"), f("c"), f("d", nullable=True)),
        "ok",
    ),
    (
        LEGACY_BASE
        + "@dataclass\nclass Child(Base[list[list[B]], A]):\n    c: A\n    d: B\nT = Child[int | None, str]",
        keyword(f("a"), f("b"), f("c"), f("d", nullable=True)),
        "ok",
    ),
    (
        LEGACY_BASE
        + "@dataclass\nclass Child(Base[Annotated[list[B], A], C]):\n    c: B\n    d: C\nT = Child[int | None, str]",
        keyword(f("a"), f("b"), f("c", nullable=True), f("d")),
        "ok",
    ),
    (
        LEGACY_BASE
        + "@dataclass\nclass Child(Base[A, B], typing.Generic[B, A]):\n    c: A\n    d: B\nT = Child[int | None, str]",
        keyword(f("a"), f("b", nullable=True), f("c"), f("d", nullable=True)),
        "ok",
    ),
    (
        (
            "A = TypeVar('A')\nB = TypeVar('B')\n@dataclass\nclass One(typing.Generic[A]):\n    a: A\n"
            "@dataclass\nclass Two(typing.Generic[B]):\n    b: B\n"
            "@dataclass\nclass Child(One[B], Two[A]):\n    c: A\nT = Child[int | None, str]"
        ),
        keyword(f("b"), f("a", nullable=True), f("c")),
        "ok",
    ),
    (
        (
            "@dataclass\nclass Base[X, Y]:\n    a: X\n    b: Y\n"
            "@dataclass\nclass Child[A, B](Base[B | None, A]):\n    c: A\n    d: B\nT = Child[int | None, str]"
        ),
        keyword(f("a", nullable=True), f("b", nullable=True), f("c", nullable=True), f("d")),
        "ok",
    ),
    # a default that names an earlier parameter is substituted when the class is subscripted with fewer arguments
    (
        "@dataclass\nclass Crate[A, B = list[A | None]]:\n    a: A\n    b: B\nT = Crate[int]",
        keyword(f("a"), f("b")),
        "ok",
    ),
    (
        "@dataclass\nclass Crate[A, B = A | None]:\n    a: A\n    b: B\nT = Crate[int]",
        keyword(f("a"), f("b", nullable=True)),
        "ok",
    ),
    (
        (
            "A = TypeVar('A')\nB = TypeVar('B', default=A | None)\n"
            "@dataclass\nclass Crate(typing.Generic[A, B]):\n    a: A\n    b: B\nT = Crate[int]"
        ),
        keyword(f("a"), f("b", nullable=True)),
        "ok",
    ),
    (
        "class Crate[A, B = A | None](NamedTuple):\n    a: A\n    b: B\nT = Crate[int]",
        positional(f("a"), f("b", nullable=True)),
        "ok",
    ),
    (
        "class Crate[A, B = A | None](TypedDict):\n    a: A\n    b: B\nT = Crate[int]",
        dict_form(f("a"), f("b", nullable=True)),
        "ok",
    ),
    (
        "@pydantic.dataclasses.dataclass\nclass Crate[A, B = A | None]:\n    a: A\n    b: B\nT = Crate[int]",
        validator(f("a"), f("b", nullable=True)),
        "ok",
    ),
    (
        "class Page[A, B = A | None](BaseModel):\n    a: A\n    b: B\nT = Page[int]",
        validator(f("a"), f("b", nullable=True)),
        "ok",
    ),
    ("type Pair[A, B = A | None] = tuple[A, B]\nT = Pair[int]", ("tuple", 2, (False, True)), "ok"),
    # a generic alias written as a plain assignment of type variables, which Python builds the same way
    ("A = TypeVar('A')\nPair = tuple[A, str]\nT = Pair[int | None]", ("tuple", 2, (True, False)), "ok"),
    (
        "A = TypeVar('A')\nB = TypeVar('B')\nSwap = tuple[B, A]\nT = Swap[int | None, str]",
        ("tuple", 2, (True, False)),
        "ok",
    ),
    ("A = TypeVar('A')\nPair = tuple[A, str]\nT = Pair", "InterfaceError", REJECTED),
    ("A = TypeVar('A', default=str)\nPair = tuple[A, int | None]\nT = Pair", "InterfaceError", REJECTED),
    ("A = TypeVar('A', default=str)\nPair = tuple[A, int | None]\nT = Pair[int]", ("tuple", 2, (False, True)), "ok"),
    ("A = TypeVar('A')\nOpt = Optional[A]\nT = Opt[int]", ("scalar", 1, (True,)), "ok"),
    (
        "A = TypeVar('A')\nB = TypeVar('B')\nInner = tuple[A, B]\nOuter = Inner[list[B], A]\nT = Outer[int | None, str]",
        ("tuple", 2, (False, False)),
        "ok",
    ),
    (
        "A = TypeVar('A')\n"
        + CRATE.replace("Crate[X]", "Crate(typing.Generic[A])").replace("X", "A")
        + "Alias = Crate[A]\nT = Alias[int | None]",
        keyword(f("a", nullable=True), f("b")),
        "ok",
    ),
    (
        "A = TypeVar('A')\n@dataclass\nclass Crate(typing.Generic[A]):\n    a: A\nRow = Crate[A]\nT = Row[int]",
        keyword(f("a")),
        "ok",
    ),
    # a type variable list the type checkers don't accept as a row's own parameters
    (
        "P = typing.ParamSpec('P')\n@dataclass\nclass Crate(typing.Generic[P]):\n    a: int\nT = Crate[[int]]",
        "InterfaceError",
        REJECTED,
    ),
    (
        "Ts = typing.TypeVarTuple('Ts')\n@dataclass\nclass Crate(typing.Generic[typing.Unpack[Ts]]):\n    a: int\nT = Crate[int]",
        "InterfaceError",
        REJECTED,
    ),
    # one hop is one subscripted generic base, along the whole chain, wherever the fields are
    (class_chain(16, fields_everywhere=True), chain_fields(16, fields_everywhere=True), "ok"),
    (class_chain(17, fields_everywhere=True), "InterfaceError", REJECTED),
    (class_chain(16, fields_everywhere=False), chain_fields(16, fields_everywhere=False), "ok"),
    (class_chain(17, fields_everywhere=False), "InterfaceError", REJECTED),
    (class_chain(20, fields_everywhere=False), "InterfaceError", REJECTED),
    (legacy_chain(16, restate=False), chain_fields(16, fields_everywhere=True), "ok"),
    (legacy_chain(17, restate=False), "InterfaceError", REJECTED),
    (legacy_chain(16, restate=True), chain_fields(16, fields_everywhere=True), "ok"),
    (legacy_chain(17, restate=True), "InterfaceError", REJECTED),
]
ROWS.extend(TYPE_VARIABLE_ROWS)

# the `type_name` of a generic row, which both sides show the same way: `Optional[X]` and `Union[X, None]` as `X | None`,
# `Annotated` as its type, a plain alias as its target, a type alias by its name and a type variable by its name
TYPE_NAME_ROWS: Final[list[tuple[str, str]]] = [
    (CRATE + "T = Crate[int]", "Crate[int]"),
    (CRATE + "T = Crate[Optional[int]]", "Crate[int | None]"),
    (CRATE + "T = Crate[Union[int, None]]", "Crate[int | None]"),
    (CRATE + "T = Crate[Union[None, int]]", "Crate[None | int]"),
    (CRATE + "T = Crate[Optional[int | None]]", "Crate[int | None]"),
    (CRATE + "T = Crate[int | None]", "Crate[int | None]"),
    (CRATE + "T = Crate[Annotated[int, 'x']]", "Crate[int]"),
    (CRATE + "T = Crate[Annotated[Optional[int], 'x']]", "Crate[int | None]"),
    (CRATE + "T = Crate[list[Annotated[Optional[int], 'x']]]", "Crate[list[int | None]]"),
    (CRATE + "I = int\nT = Crate[I]", "Crate[int]"),
    (CRATE + "I = Optional[int]\nT = Crate[I]", "Crate[int | None]"),
    (CRATE + "I = Annotated[int, 'x']\nT = Crate[I]", "Crate[int]"),
    (CRATE + "type I = int | None\nT = Crate[I]", "Crate[I]"),
    (CRATE + "T = Crate[datetime.datetime]", "Crate[datetime]"),
    (CRATE + "T = Crate[list[Optional[str]]]", "Crate[list[str | None]]"),
    (CRATE + "T = Crate[None]", "Crate[None]"),
    ("A = TypeVar('A')\nOpt = Optional[A]\n" + CRATE + "T = Crate[Opt[int]]", "Crate[int | None]"),
    ("A = TypeVar('A')\nMap = dict[str, A]\n" + CRATE + "T = Crate[Map[str | None]]", "Crate[dict[str, str | None]]"),
    (
        "@dataclass\nclass Crate[A, B = list[A | None]]:\n    a: A\n    b: B\nT = Crate[Optional[int]]",
        "Crate[int | None, list[int | None]]",
    ),
    (
        "A = TypeVar('A')\n@dataclass\nclass Crate(typing.Generic[A]):\n    a: A\nT = Crate[Optional[int]]",
        "Crate[int | None]",
    ),
    (
        "@dataclass\nclass Base[X]:\n    a: X\n@dataclass\nclass Child[U](Base[U]):\n    b: U\nT = Child[Optional[int]]",
        "Child[int | None]",
    ),
    (
        "@pydantic.dataclasses.dataclass\nclass Crate[X]:\n    a: X\nT = Crate[Optional[int]]",
        "Crate[int | None]",
    ),
]


type ModuleCase = tuple[str, dict[str, str], NamedPlan | str]

IMPORT_HEADER: Final[str] = "from dataclasses import dataclass\nfrom typing import Generic\n"
SHARED: Final[str] = (
    "from typing import ParamSpec, TypeVar, TypeVarTuple\n"
    "X = TypeVar('X')\nY = TypeVar('Y')\nD = TypeVar('D', default=int)\nE = TypeVar('E', default=X | None)\n"
    "P = ParamSpec('P')\nTs = TypeVarTuple('Ts')\n"
)
CRATE_OF: Final[str] = "@dataclass\nclass Crate(Generic[{param}]):\n    a: {param}\n    b: str\n"
# a row type in `main.py` that uses type variables defined in other modules, which both sides must read as the same
# variables however they were imported; a case is its name, its modules and the plan, or REJECTED
IMPORTED_TYPE_VARIABLE_CASES: Final[list[ModuleCase]] = [
    (
        "from_import",
        {
            "shared.py": SHARED,
            "main.py": IMPORT_HEADER + "from shared import X\n" + CRATE_OF.format(param="X") + "T = Crate[int | None]",
        },
        keyword(f("a", nullable=True), f("b")),
    ),
    (
        "renamed",
        {
            "shared.py": SHARED,
            "main.py": IMPORT_HEADER
            + "from shared import X as V\n"
            + CRATE_OF.format(param="V")
            + "T = Crate[int | None]",
        },
        keyword(f("a", nullable=True), f("b")),
    ),
    (
        "reexported",
        {
            "shared.py": SHARED,
            "mid.py": "from shared import X\n",
            "main.py": IMPORT_HEADER + "from mid import X\n" + CRATE_OF.format(param="X") + "T = Crate[int | None]",
        },
        keyword(f("a", nullable=True), f("b")),
    ),
    (
        "attribute",
        {
            "shared.py": SHARED,
            "main.py": IMPORT_HEADER + "import shared as s\n" + CRATE_OF.format(param="s.X") + "T = Crate[int | None]",
        },
        keyword(f("a", nullable=True), f("b")),
    ),
    (
        "inferred_from_a_base",
        {
            "shared.py": SHARED,
            "main.py": (
                IMPORT_HEADER
                + "from shared import X, Y\n@dataclass\nclass Base(Generic[X, Y]):\n    a: X\n    b: Y\n"
                + "@dataclass\nclass Child(Base[list[Y], X]):\n    c: Y\n    d: X\nT = Child[int | None, str]"
            ),
        },
        keyword(f("a"), f("b"), f("c", nullable=True), f("d")),
    ),
    (
        "default_bare",
        {
            "shared.py": SHARED,
            "main.py": IMPORT_HEADER + "from shared import D\n" + CRATE_OF.format(param="D") + "T = Crate",
        },
        keyword(f("a"), f("b")),
    ),
    (
        "default_subscripted",
        {
            "shared.py": SHARED,
            "main.py": IMPORT_HEADER + "from shared import D\n" + CRATE_OF.format(param="D") + "T = Crate[str | None]",
        },
        keyword(f("a", nullable=True), f("b")),
    ),
    (
        "default_names_another_shared_variable",
        {
            "shared.py": SHARED,
            "main.py": (
                IMPORT_HEADER
                + "from shared import X, E\n@dataclass\nclass Crate(Generic[X, E]):\n    a: X\n    b: E\nT = Crate[int]"
            ),
        },
        keyword(f("a"), f("b", nullable=True)),
    ),
    (
        "imported_paramspec",
        {
            "shared.py": SHARED,
            "main.py": IMPORT_HEADER
            + "from shared import P\n@dataclass\nclass Crate(Generic[P]):\n    a: int\nT = Crate[[int]]",
        },
        REJECTED,
    ),
    (
        "imported_typevartuple",
        {
            "shared.py": SHARED,
            "main.py": (
                IMPORT_HEADER
                + "from typing import Unpack\nfrom shared import Ts\n"
                + "@dataclass\nclass Crate(Generic[Unpack[Ts]]):\n    a: int\nT = Crate[int]"
            ),
        },
        REJECTED,
    ),
    (
        "imported_variable_is_not_a_row",
        {"shared.py": SHARED, "main.py": "from shared import X\nT = X"},
        REJECTED,
    ),
]
