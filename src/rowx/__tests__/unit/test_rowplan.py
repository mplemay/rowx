import sys
import types

import pytest

from rowx.__tests__.conformance.row_types import PRELUDE, ROWS, NamedPlan, Plan
from rowx._rowplan import NO_ROWS, RowPlan, row_plan
from rowx.errors import InterfaceError


def evaluate(source: str) -> object:
    ns: dict[str, object] = {}
    exec(PRELUDE + source, ns)  # ruff: ignore[exec-builtin]
    return ns["T"]


@pytest.mark.parametrize(("source", "expected", "_verdict"), ROWS)
def test_conformance_row(source: str, expected: Plan | NamedPlan | str, _verdict: str) -> None:
    t = evaluate(source)
    if isinstance(expected, str):
        with pytest.raises(InterfaceError):
            row_plan(t)
        return
    if len(expected) == 4:
        _, form, matching, fields = expected
        plan = row_plan(t)
        assert plan.kind == "named"
        assert (plan.form, plan.matching) == (form, matching)
        assert tuple((f.key, f.names, f.nullable, f.required) for f in plan.fields) == fields
        assert plan.arity == len(fields)
        assert plan.nullable_mask == tuple(f[2] for f in fields)
        return
    assert len(expected) == 3
    kind, arity, mask = expected
    assert row_plan(t) == RowPlan(kind=kind, arity=arity, nullable_mask=mask)  # ty: ignore[invalid-argument-type]


def test_none_is_shared_no_rows_plan() -> None:
    assert row_plan(None) is NO_ROWS


def test_plan_is_cached_per_type() -> None:
    t = evaluate("T = tuple[int, str | None]")
    assert row_plan(t) is row_plan(t)


def test_unhashable_annotation_is_not_cached() -> None:
    t = evaluate("T = Annotated[int, []]")
    assert row_plan(t) == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))


def test_alias_depth_limit() -> None:
    ns: dict[str, object] = {}
    lines = ["type A0 = int"] + [f"type A{i} = A{i - 1}" for i in range(1, 20)]
    exec("\n".join(lines), ns)  # ruff: ignore[exec-builtin]
    with pytest.raises(InterfaceError):
        row_plan(ns["A19"])
    assert row_plan(ns["A10"]).kind == "scalar"


def test_network_pair_in_either_order_is_one_leaf() -> None:
    forward = evaluate("T = ipaddress.IPv4Network | ipaddress.IPv6Network | None")
    backward = evaluate("T = ipaddress.IPv6Network | ipaddress.IPv4Network")
    assert row_plan(forward) == RowPlan(kind="scalar", arity=1, nullable_mask=(True,))
    assert row_plan(backward) == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))


@pytest.mark.parametrize(
    "source",
    [
        "type V4 = ipaddress.IPv4Interface | None\ntype V6 = ipaddress.IPv6Interface\nT = V4 | V6",
        "type V4 = Annotated[ipaddress.IPv4Interface | None, 1]\nT = Annotated[V4 | ipaddress.IPv6Interface, 2]",
    ],
)
def test_network_pair_split_across_nested_unions_is_one_nullable_leaf(source: str) -> None:
    assert row_plan(evaluate(source)) == RowPlan(kind="scalar", arity=1, nullable_mask=(True,))


def test_cyclic_union_alias_is_rejected() -> None:
    with pytest.raises(InterfaceError):
        row_plan(evaluate("type A = A | int\nT = A"))


@pytest.mark.parametrize("subtype", ["int", "decimal.Decimal", "datetime.date", "datetime.datetime"])
def test_range_is_a_non_nullable_leaf_for_each_subtype(subtype: str) -> None:
    assert row_plan(evaluate(f"T = Range[{subtype}]")) == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))
    assert row_plan(evaluate(f"T = Range[{subtype}] | None")) == RowPlan(kind="scalar", arity=1, nullable_mask=(True,))


def test_range_subtype_is_looked_through_aliases() -> None:
    plan = row_plan(evaluate("type N = Annotated[int, 1]\nT = Range[N]"))
    assert plan == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))


def test_money_and_interval_are_non_nullable_leaves() -> None:
    assert row_plan(evaluate("T = Money")).nullable_mask == (False,)
    assert row_plan(evaluate("T = Interval | None")).nullable_mask == (True,)
    assert row_plan(evaluate("T = list[list[Money]]")).nullable_mask == (False,)


@pytest.mark.parametrize(
    ("source", "spans"),
    [
        ("T = Interval", (True,)),
        ("T = Interval | None", (True,)),
        ("T = list[Interval | None]", (True,)),
        ("T = list[list[Interval]]", (True,)),
        ("T = list[Interval] | None", (True,)),
        ("T = list[list[Interval] | None]", (True,)),
        ("T = list[datetime.timedelta] | None", (False,)),
        ("T = datetime.timedelta", (False,)),
        ("T = Any", (False,)),
        ("T = Money", (False,)),
        ("T = tuple[Interval, datetime.timedelta | None, list[Interval], int]", (True, False, True, False)),
        ("type I = Interval\nT = tuple[I | None, str]", (True, False)),
    ],
)
def test_interval_leaf_is_marked_per_column(source: str, spans: tuple[bool, ...]) -> None:
    assert row_plan(evaluate(source)).interval_mask == spans


def test_flags_pack_nullability_and_the_interval_leaf() -> None:
    plan = row_plan(evaluate("T = tuple[Interval, datetime.timedelta | None, Interval | None, int]"))
    assert plan.flags == [0b10, 0b01, 0b11, 0b00]


def test_interval_and_money_are_rejected_in_unions() -> None:
    for source in ("T = Interval | Money", "T = Interval | datetime.timedelta"):
        with pytest.raises(InterfaceError):
            row_plan(evaluate(source))


def test_geometric_classes_are_non_nullable_leaves() -> None:
    for name in ("Point", "Line", "LineSegment", "Box", "Path", "Polygon", "Circle"):
        assert row_plan(evaluate(f"T = {name}")).nullable_mask == (False,), name
        assert row_plan(evaluate(f"T = {name} | None")).nullable_mask == (True,), name
        assert row_plan(evaluate(f"T = list[list[{name} | None]]")).nullable_mask == (False,), name
    plan = row_plan(evaluate("T = tuple[Point, Box | None, list[Circle]]"))
    assert plan.nullable_mask == (False, True, False)
    assert plan.interval_mask == (False, False, False)


def test_geometric_classes_are_rejected_in_unions_and_subscripts() -> None:
    for source in ("T = Point | Circle", "T = Point | int", "T = Path | Polygon"):
        with pytest.raises(InterfaceError):
            row_plan(evaluate(source))


def test_cube_and_the_hstore_dict_are_non_nullable_leaves() -> None:
    for leaf in ("Cube", "dict[str, str | None]"):
        assert row_plan(evaluate(f"T = {leaf}")).nullable_mask == (False,), leaf
        assert row_plan(evaluate(f"T = {leaf} | None")).nullable_mask == (True,), leaf
        assert row_plan(evaluate(f"T = list[list[{leaf} | None]]")).nullable_mask == (False,), leaf


def test_other_dicts_and_cube_unions_are_rejected() -> None:
    for source in (
        "T = dict",
        "T = dict[str, str]",
        "T = dict[str, int]",
        "T = dict[int, str | None]",
        "T = Cube | Point",
        "T = dict[str, str | None] | Cube",
    ):
        with pytest.raises(InterfaceError):
            row_plan(evaluate(source))


def keys(source: str) -> list[tuple[str, bool, bool]]:
    return [(f.key, f.nullable, f.required) for f in row_plan(evaluate(source)).fields]


def test_named_plan_carries_the_form_the_type_name_and_a_shape() -> None:
    t = evaluate("@dataclass\nclass T:\n    id: int\n    name: str | None")
    plan = row_plan(t)
    assert (plan.kind, plan.form, plan.matching, plan.type_name, plan.arity) == ("named", "keyword", "by_name", "T", 2)
    assert plan.nullable_mask == (False, True)
    assert plan.flags == [0, 1]
    assert (plan.shape.form, plan.shape.matching, plan.shape.type_name) == ("keyword", "by_name", "T")  # ty: ignore[unresolved-attribute]
    assert plan.shape.ctor is t  # ty: ignore[unresolved-attribute]


def test_named_plan_and_its_shape_are_cached_per_type() -> None:
    t = evaluate("@dataclass\nclass T:\n    id: int")
    assert row_plan(t) is row_plan(t)
    assert row_plan(t).shape is row_plan(t).shape


@pytest.mark.parametrize(
    ("source", "form", "matching"),
    [
        ("@dataclass\nclass T:\n    a: int", "keyword", "by_name"),
        ("class T(NamedTuple):\n    a: int", "positional", "positional"),
        ("T = NamedTuple('T', [('a', int)])", "positional", "positional"),
        ("class T(TypedDict):\n    a: int", "dict", "by_name"),
        ("T = TypedDict('T', {'a': int})", "dict", "by_name"),
        ("class T(typing_extensions.TypedDict):\n    a: int", "dict", "by_name"),
    ],
)
def test_each_form_has_its_form_and_matching(source: str, form: str, matching: str) -> None:
    plan = row_plan(evaluate(source))
    assert (plan.form, plan.matching) == (form, matching)
    assert plan.shape.form == form  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize(
    "decorator",
    [
        "@dataclass(slots=True)",
        "@dataclass(frozen=True)",
        "@dataclass(kw_only=True)",
        "@dataclass(slots=True, frozen=True, kw_only=True)",
    ],
)
def test_dataclass_options_do_not_change_the_fields(decorator: str) -> None:
    assert keys(f"{decorator}\nclass T:\n    a: int\n    b: str | None = None") == [
        ("a", False, True),
        ("b", True, False),
    ]


def test_dataclass_kw_only_markers_are_ordinary_fields() -> None:
    source = "@dataclass\nclass T:\n    a: int\n    _: KW_ONLY\n    b: int\n    c: int = field(kw_only=True, default=0)"
    assert keys(source) == [("a", False, True), ("b", False, True), ("c", False, False)]


def test_dataclass_default_factory_is_not_required() -> None:
    assert keys("@dataclass\nclass T:\n    a: list[int] = field(default_factory=list)") == [("a", False, False)]


def test_dataclass_fields_are_inherited_across_two_levels_and_a_redeclared_one_keeps_its_place() -> None:
    source = (
        "@dataclass\nclass A:\n    a: int\n    b: str\n"
        "@dataclass\nclass B(A):\n    c: int\n"
        "@dataclass\nclass T(B):\n    b: str | None\n    d: int = 0"
    )
    assert keys(source) == [("a", False, True), ("b", True, True), ("c", False, True), ("d", False, False)]


def test_dataclass_members_that_are_not_fields_are_excluded() -> None:
    source = "@dataclass\nclass T:\n    a: int\n    c: ClassVar[int] = 0\n    d: int = field(init=False, default=1)"
    assert keys(source) == [("a", False, True)]


def test_a_dataclass_with_a_hand_written_init_is_accepted_at_runtime() -> None:
    source = "@dataclass\nclass T:\n    a: int\n    def __init__(self, a: int) -> None:\n        self.a = a"
    assert keys(source) == [("a", False, True)]


def test_namedtuple_defaults_are_not_required_and_a_subclass_adds_no_fields() -> None:
    assert keys("class T(NamedTuple):\n    a: int\n    b: str = ''") == [("a", False, True), ("b", False, False)]
    source = "class Base(NamedTuple):\n    a: int\n    b: str | None\nclass T(Base):\n    extra: int"
    assert keys(source) == [("a", False, True), ("b", True, True)]
    assert row_plan(evaluate(source)).type_name == "T"


def test_functional_namedtuple_and_typeddict() -> None:
    assert keys("T = NamedTuple('T', [('a', int), ('b', str | None)])") == [("a", False, True), ("b", True, True)]
    assert keys("T = TypedDict('T', {'a': int, 'b': str | None}, total=False)") == [
        ("a", False, False),
        ("b", True, False),
    ]


def test_typeddict_qualifiers_decide_which_keys_are_required() -> None:
    source = "class T(TypedDict):\n    a: int\n    b: NotRequired[int]\n    c: ReadOnly[int]\n    d: ReadOnly[NotRequired[int | None]]"
    assert keys(source) == [("a", False, True), ("b", False, False), ("c", False, True), ("d", True, False)]
    source = "class T(TypedDict, total=False):\n    a: int\n    b: Required[int]\n    c: ReadOnly[Required[str]]"
    assert keys(source) == [("a", False, False), ("b", False, True), ("c", False, True)]


def test_typeddict_total_applies_to_the_declaring_class_only() -> None:
    source = (
        "class A(TypedDict):\n    a: int\n    b: str\n"
        "class B(A, total=False):\n    c: int\n"
        "class T(B):\n    b: str | None\n    d: int"
    )
    assert keys(source) == [("a", False, True), ("b", True, True), ("c", False, False), ("d", False, True)]


def test_typing_extensions_typeddict_qualifiers() -> None:
    source = "class T(typing_extensions.TypedDict):\n    a: typing_extensions.ReadOnly[int]\n    b: typing_extensions.NotRequired[str]"
    assert keys(source) == [("a", False, True), ("b", False, False)]


def test_annotated_fields_and_a_type_alias_naming_the_class() -> None:
    source = (
        "@dataclass\nclass U:\n    a: Annotated[int, 'x']\n    b: Annotated[str | None, 1]\ntype Alias = U\nT = Alias"
    )
    assert keys(source) == [("a", False, True), ("b", True, True)]
    assert row_plan(evaluate(source)).type_name == "U"


def test_leaf_typed_fields_mark_intervals_and_ranges() -> None:
    plan = row_plan(
        evaluate(
            "@dataclass\nclass T:\n    i: Interval\n    j: Interval | None\n    t: datetime.timedelta\n    r: Range[int]"
        )
    )
    assert [f.interval for f in plan.fields] == [True, True, False, False]
    assert plan.flags == [0b10, 0b11, 0b00, 0b00]


def test_a_field_typed_none_is_nullable() -> None:
    assert keys("class T(TypedDict):\n    v: None") == [("v", True, True)]


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("T = Money", "scalar"),
        ("T = Point", "scalar"),
        ("T = Range[int]", "scalar"),
        ("T = tuple[Money, Point]", "tuple"),
    ],
)
def test_value_classes_stay_leaves_though_they_are_dataclasses(source: str, kind: str) -> None:
    assert row_plan(evaluate(source)).kind == kind


def test_the_bare_range_class_is_not_a_named_row() -> None:
    with pytest.raises(InterfaceError):
        row_plan(evaluate("T = Range"))


def test_an_annotation_imported_only_under_type_checking_names_the_field_and_the_name() -> None:
    source = (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from fractions import Fraction\n"
        "@dataclass\nclass T:\n    id: int\n    ratio: list[Fraction]"
    )
    with pytest.raises(InterfaceError, match=r"field 'ratio' names 'Fraction'"):
        row_plan(evaluate(source))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("class T(Protocol):\n    id: int", "Protocol"),
        ("T = collections.namedtuple('T', ['id', 'name'])", "no type annotation"),
        ("@dataclass(init=False)\nclass T:\n    id: int", "init=False"),
        ("@dataclass\nclass T:\n    id: int\n    scale: InitVar[int]", "InitVar field 'scale'"),
        ("@dataclass\nclass T: ...", "has no fields"),
        ("class T(NamedTuple): ...", "has no fields"),
        ("class T(TypedDict): ...", "has no fields"),
        ("@dataclass\nclass U:\n    id: int\nT = U | None", r"use \.optional\(\)"),
        ("@dataclass\nclass U:\n    id: int\nT = Optional[U]", r"use \.optional\(\)"),
        ("@dataclass\nclass U:\n    id: int\ntype A = U | None\nT = A", r"use \.optional\(\)"),
        ("@dataclass\nclass U:\n    id: int\nT = tuple[U, int]", "only allowed as the row"),
        ("@dataclass\nclass U:\n    id: int\nT = list[U]", "only allowed as the row"),
        ("@dataclass\nclass U:\n    id: int\nT = U | int", "exactly one non-None member"),
        (
            "@dataclass\nclass U:\n    id: int\n@dataclass\nclass T:\n    u: U",
            r"only allowed as the row.*field 'u' of T",
        ),
        ("class T(TypedDict):\n    a: int | str", "field 'a' of T"),
    ],
)
def test_named_rejections_have_their_messages(source: str, message: str) -> None:
    with pytest.raises(InterfaceError, match=message):
        row_plan(evaluate(source))


def test_a_pydantic_class_is_rejected_for_now(monkeypatch: pytest.MonkeyPatch) -> None:
    # detection goes through `sys.modules` and the class attributes, so a stand-in is enough
    monkeypatch.setitem(sys.modules, "pydantic", types.ModuleType("pydantic"))
    ns: dict[str, object] = {}
    exec("from dataclasses import dataclass\n@dataclass\nclass T:\n    id: int\n    __pydantic_validator__ = 1", ns)  # ruff: ignore[exec-builtin]
    with pytest.raises(InterfaceError, match="pydantic"):
        row_plan(ns["T"])
