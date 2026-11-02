import importlib
import sys
import types
from typing import TYPE_CHECKING, Final

import pytest

from rowx.__tests__.conformance.row_types import PRELUDE, ROWS, NamedPlan, Plan
from rowx._rowplan import NO_ROWS, RowPlan, row_plan
from rowx.errors import InterfaceError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


type Evaluate = Callable[[str], object]
type Keys = Callable[[str], list[tuple[str, bool, bool]]]


@pytest.fixture
def evaluate(monkeypatch: pytest.MonkeyPatch) -> Evaluate:
    def run(source: str) -> object:
        # a class resolves its annotations in its module, so the source runs in a registered one that goes with the test
        module = types.ModuleType("conformance_row")
        monkeypatch.setitem(sys.modules, "conformance_row", module)
        exec(PRELUDE + source, module.__dict__)  # ruff: ignore[exec-builtin]
        return module.T

    return run


@pytest.mark.parametrize(("source", "expected", "_verdict"), ROWS)
def test_conformance_row(source: str, expected: Plan | NamedPlan | str, _verdict: str, evaluate: Evaluate) -> None:
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


def test_plan_is_cached_per_type(evaluate: Evaluate) -> None:
    t = evaluate("T = tuple[int, str | None]")
    assert row_plan(t) is row_plan(t)


def test_unhashable_annotation_is_not_cached(evaluate: Evaluate) -> None:
    t = evaluate("T = Annotated[int, []]")
    assert row_plan(t) == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))


def test_alias_depth_limit() -> None:
    ns: dict[str, object] = {}
    lines = ["type A0 = int"] + [f"type A{i} = A{i - 1}" for i in range(1, 20)]
    exec("\n".join(lines), ns)  # ruff: ignore[exec-builtin]
    with pytest.raises(InterfaceError):
        row_plan(ns["A19"])
    assert row_plan(ns["A10"]).kind == "scalar"


def test_network_pair_in_either_order_is_one_leaf(evaluate: Evaluate) -> None:
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
def test_network_pair_split_across_nested_unions_is_one_nullable_leaf(source: str, evaluate: Evaluate) -> None:
    assert row_plan(evaluate(source)) == RowPlan(kind="scalar", arity=1, nullable_mask=(True,))


def test_cyclic_union_alias_is_rejected(evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError):
        row_plan(evaluate("type A = A | int\nT = A"))


@pytest.mark.parametrize("subtype", ["int", "decimal.Decimal", "datetime.date", "datetime.datetime"])
def test_range_is_a_non_nullable_leaf_for_each_subtype(subtype: str, evaluate: Evaluate) -> None:
    assert row_plan(evaluate(f"T = Range[{subtype}]")) == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))
    assert row_plan(evaluate(f"T = Range[{subtype}] | None")) == RowPlan(kind="scalar", arity=1, nullable_mask=(True,))


def test_range_subtype_is_looked_through_aliases(evaluate: Evaluate) -> None:
    plan = row_plan(evaluate("type N = Annotated[int, 1]\nT = Range[N]"))
    assert plan == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))


def test_money_and_interval_are_non_nullable_leaves(evaluate: Evaluate) -> None:
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
def test_interval_leaf_is_marked_per_column(source: str, spans: tuple[bool, ...], evaluate: Evaluate) -> None:
    assert row_plan(evaluate(source)).interval_mask == spans


def test_flags_pack_nullability_and_the_interval_leaf(evaluate: Evaluate) -> None:
    plan = row_plan(evaluate("T = tuple[Interval, datetime.timedelta | None, Interval | None, int]"))
    assert plan.flags == [0b10, 0b01, 0b11, 0b00]


def test_interval_and_money_are_rejected_in_unions(evaluate: Evaluate) -> None:
    for source in ("T = Interval | Money", "T = Interval | datetime.timedelta"):
        with pytest.raises(InterfaceError):
            row_plan(evaluate(source))


def test_geometric_classes_are_non_nullable_leaves(evaluate: Evaluate) -> None:
    for name in ("Point", "Line", "LineSegment", "Box", "Path", "Polygon", "Circle"):
        assert row_plan(evaluate(f"T = {name}")).nullable_mask == (False,), name
        assert row_plan(evaluate(f"T = {name} | None")).nullable_mask == (True,), name
        assert row_plan(evaluate(f"T = list[list[{name} | None]]")).nullable_mask == (False,), name
    plan = row_plan(evaluate("T = tuple[Point, Box | None, list[Circle]]"))
    assert plan.nullable_mask == (False, True, False)
    assert plan.interval_mask == (False, False, False)


def test_geometric_classes_are_rejected_in_unions_and_subscripts(evaluate: Evaluate) -> None:
    for source in ("T = Point | Circle", "T = Point | int", "T = Path | Polygon"):
        with pytest.raises(InterfaceError):
            row_plan(evaluate(source))


def test_cube_and_the_hstore_dict_are_non_nullable_leaves(evaluate: Evaluate) -> None:
    for leaf in ("Cube", "dict[str, str | None]"):
        assert row_plan(evaluate(f"T = {leaf}")).nullable_mask == (False,), leaf
        assert row_plan(evaluate(f"T = {leaf} | None")).nullable_mask == (True,), leaf
        assert row_plan(evaluate(f"T = list[list[{leaf} | None]]")).nullable_mask == (False,), leaf


def test_other_dicts_and_cube_unions_are_rejected(evaluate: Evaluate) -> None:
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


@pytest.fixture
def keys(evaluate: Evaluate) -> Keys:
    def run(source: str) -> list[tuple[str, bool, bool]]:
        return [(f.key, f.nullable, f.required) for f in row_plan(evaluate(source)).fields]

    return run


def test_named_plan_carries_the_form_the_type_name_and_a_shape(evaluate: Evaluate) -> None:
    t = evaluate("@dataclass\nclass T:\n    id: int\n    name: str | None")
    plan = row_plan(t)
    assert (plan.kind, plan.form, plan.matching, plan.type_name, plan.arity) == ("named", "keyword", "by_name", "T", 2)
    assert plan.nullable_mask == (False, True)
    assert plan.flags == [0, 1]
    assert (plan.shape.form, plan.shape.matching, plan.shape.type_name) == ("keyword", "by_name", "T")  # ty: ignore[unresolved-attribute]
    assert plan.shape.ctor is t  # ty: ignore[unresolved-attribute]


def test_named_plan_and_its_shape_are_cached_per_type(evaluate: Evaluate) -> None:
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
def test_each_form_has_its_form_and_matching(source: str, form: str, matching: str, evaluate: Evaluate) -> None:
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
def test_dataclass_options_do_not_change_the_fields(decorator: str, keys: Keys) -> None:
    assert keys(f"{decorator}\nclass T:\n    a: int\n    b: str | None = None") == [
        ("a", False, True),
        ("b", True, False),
    ]


def test_dataclass_kw_only_markers_are_ordinary_fields(keys: Keys) -> None:
    source = "@dataclass\nclass T:\n    a: int\n    _: KW_ONLY\n    b: int\n    c: int = field(kw_only=True, default=0)"
    assert keys(source) == [("a", False, True), ("b", False, True), ("c", False, False)]


def test_dataclass_default_factory_is_not_required(keys: Keys) -> None:
    assert keys("@dataclass\nclass T:\n    a: list[int] = field(default_factory=list)") == [("a", False, False)]


def test_dataclass_fields_are_inherited_across_two_levels_and_a_redeclared_one_keeps_its_place(keys: Keys) -> None:
    source = (
        "@dataclass\nclass A:\n    a: int\n    b: str\n"
        "@dataclass\nclass B(A):\n    c: int\n"
        "@dataclass\nclass T(B):\n    b: str | None\n    d: int = 0"
    )
    assert keys(source) == [("a", False, True), ("b", True, True), ("c", False, True), ("d", False, False)]


def test_dataclass_members_that_are_not_fields_are_excluded(keys: Keys) -> None:
    source = "@dataclass\nclass T:\n    a: int\n    c: ClassVar[int] = 0\n    d: int = field(init=False, default=1)"
    assert keys(source) == [("a", False, True)]


def test_a_dataclass_with_a_hand_written_init_is_accepted_at_runtime(keys: Keys) -> None:
    source = "@dataclass\nclass T:\n    a: int\n    def __init__(self, a: int) -> None:\n        self.a = a"
    assert keys(source) == [("a", False, True)]


def test_namedtuple_defaults_are_not_required_and_a_subclass_adds_no_fields(evaluate: Evaluate, keys: Keys) -> None:
    assert keys("class T(NamedTuple):\n    a: int\n    b: str = ''") == [("a", False, True), ("b", False, False)]
    source = "class Base(NamedTuple):\n    a: int\n    b: str | None\nclass T(Base):\n    extra: int"
    assert keys(source) == [("a", False, True), ("b", True, True)]
    assert row_plan(evaluate(source)).type_name == "T"


def test_functional_namedtuple_and_typeddict(keys: Keys) -> None:
    assert keys("T = NamedTuple('T', [('a', int), ('b', str | None)])") == [("a", False, True), ("b", True, True)]
    assert keys("T = TypedDict('T', {'a': int, 'b': str | None}, total=False)") == [
        ("a", False, False),
        ("b", True, False),
    ]


def test_typeddict_qualifiers_decide_which_keys_are_required(keys: Keys) -> None:
    source = "class T(TypedDict):\n    a: int\n    b: NotRequired[int]\n    c: ReadOnly[int]\n    d: ReadOnly[NotRequired[int | None]]"
    assert keys(source) == [("a", False, True), ("b", False, False), ("c", False, True), ("d", True, False)]
    source = "class T(TypedDict, total=False):\n    a: int\n    b: Required[int]\n    c: ReadOnly[Required[str]]"
    assert keys(source) == [("a", False, False), ("b", False, True), ("c", False, True)]


def test_typeddict_total_applies_to_the_declaring_class_only(keys: Keys) -> None:
    source = (
        "class A(TypedDict):\n    a: int\n    b: str\n"
        "class B(A, total=False):\n    c: int\n"
        "class T(B):\n    b: str | None\n    d: int"
    )
    assert keys(source) == [("a", False, True), ("b", True, True), ("c", False, False), ("d", False, True)]


def test_typing_extensions_typeddict_qualifiers(keys: Keys) -> None:
    source = "class T(typing_extensions.TypedDict):\n    a: typing_extensions.ReadOnly[int]\n    b: typing_extensions.NotRequired[str]"
    assert keys(source) == [("a", False, True), ("b", False, False)]


def test_annotated_fields_and_a_type_alias_naming_the_class(evaluate: Evaluate, keys: Keys) -> None:
    source = (
        "@dataclass\nclass U:\n    a: Annotated[int, 'x']\n    b: Annotated[str | None, 1]\ntype Alias = U\nT = Alias"
    )
    assert keys(source) == [("a", False, True), ("b", True, True)]
    assert row_plan(evaluate(source)).type_name == "U"


def test_leaf_typed_fields_mark_intervals_and_ranges(evaluate: Evaluate) -> None:
    plan = row_plan(
        evaluate(
            "@dataclass\nclass T:\n    i: Interval\n    j: Interval | None\n    t: datetime.timedelta\n    r: Range[int]"
        )
    )
    assert [f.interval for f in plan.fields] == [True, True, False, False]
    assert plan.flags == [0b10, 0b11, 0b00, 0b00]


def test_a_field_typed_none_is_nullable(keys: Keys) -> None:
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
def test_value_classes_stay_leaves_though_they_are_dataclasses(source: str, kind: str, evaluate: Evaluate) -> None:
    assert row_plan(evaluate(source)).kind == kind


def test_the_bare_range_class_is_not_a_named_row(evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError):
        row_plan(evaluate("T = Range"))


def test_an_annotation_imported_only_under_type_checking_names_the_field_and_the_name(evaluate: Evaluate) -> None:
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
def test_named_rejections_have_their_messages(source: str, message: str, evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError, match=message):
        row_plan(evaluate(source))


def test_a_pydantic_class_is_rejected_for_now(monkeypatch: pytest.MonkeyPatch) -> None:
    # detection goes through `sys.modules` and the class attributes, so a stand-in is enough
    monkeypatch.setitem(sys.modules, "pydantic", types.ModuleType("pydantic"))
    ns: dict[str, object] = {}
    exec("from dataclasses import dataclass\n@dataclass\nclass T:\n    id: int\n    __pydantic_validator__ = 1", ns)  # ruff: ignore[exec-builtin]
    with pytest.raises(InterfaceError, match="pydantic"):
        row_plan(ns["T"])


FUTURE: Final[list[str]] = ["", "from __future__ import annotations\n"]


def load_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, source: str) -> types.ModuleType:
    (tmp_path / f"{name}.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module(name)
    monkeypatch.setitem(sys.modules, name, module)
    return module


MISSING_NAME_HEADER: Final[str] = (
    "from dataclasses import dataclass, field\n"
    "from typing import TYPE_CHECKING, ClassVar, NamedTuple, TypedDict, Optional\n"
    "import typing_extensions\n"
    "if TYPE_CHECKING:\n    from fractions import Fraction as Bar\n"
)


@pytest.mark.parametrize("future", FUTURE)
def test_unselected_members_naming_a_type_checking_only_name_do_not_fail_a_dataclass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future: str
) -> None:
    source = (
        future
        + MISSING_NAME_HEADER
        + "@dataclass\nclass T:\n    a: int\n    cv: ClassVar[Bar] = None\n    b: Bar = field(init=False, default=None)\n"
    )
    module = load_module(tmp_path, monkeypatch, "unselected_dc", source)
    assert [f.key for f in row_plan(module.T).fields] == ["a"]


@pytest.mark.parametrize("future", FUTURE)
def test_an_extra_annotation_on_a_namedtuple_subclass_naming_a_missing_name_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future: str
) -> None:
    source = future + MISSING_NAME_HEADER + "class Base(NamedTuple):\n    a: int\nclass T(Base):\n    extra: Bar\n"
    module = load_module(tmp_path, monkeypatch, "unselected_nt", source)
    assert [f.key for f in row_plan(module.T).fields] == ["a"]


SELECTED_FORMS: Final[list[str]] = [
    "@dataclass\nclass T:\n    a: {ann}",
    "class T(NamedTuple):\n    a: {ann}",
    "class T(TypedDict):\n    a: {ann}",
    "class T(typing_extensions.TypedDict):\n    a: {ann}",
]
SELECTED_ANNOTATIONS: Final[list[str]] = ["Bar", "'Bar'", "list['Bar']", "list[Bar]", "Optional['Bar']"]


@pytest.mark.parametrize("future", FUTURE)
@pytest.mark.parametrize("form", SELECTED_FORMS)
@pytest.mark.parametrize("annotation", SELECTED_ANNOTATIONS)
def test_a_selected_field_naming_a_type_checking_only_name_is_named_in_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future: str, form: str, annotation: str
) -> None:
    source = future + MISSING_NAME_HEADER + form.format(ann=annotation) + "\n"
    module = load_module(tmp_path, monkeypatch, "selected_missing", source)
    with pytest.raises(InterfaceError, match=r"T: field 'a' names 'Bar'"):
        row_plan(module.T)


@pytest.mark.parametrize("future", FUTURE)
def test_a_missing_name_that_is_a_substring_of_another_annotation_blames_the_right_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future: str
) -> None:
    source = future + MISSING_NAME_HEADER + "@dataclass\nclass T:\n    a: list[int]\n    b: Bar\n    c: int\n"
    module = load_module(tmp_path, monkeypatch, "substring_dc", source)
    with pytest.raises(InterfaceError, match=r"field 'b' names 'Bar'"):
        row_plan(module.T)
    source = future + MISSING_NAME_HEADER + "@dataclass\nclass T:\n    a: Bar\n    b: list[Barn]\n"
    source += "class Barn: ...\n"
    module = load_module(tmp_path, monkeypatch, "substring_dc2", source)
    with pytest.raises(InterfaceError, match=r"field 'a' names 'Bar'"):
        row_plan(module.T)


@pytest.mark.parametrize("future", FUTURE)
def test_a_typeddict_key_inherited_from_a_base_in_another_module_resolves_in_the_base_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future: str
) -> None:
    base = (
        future
        + "from decimal import Decimal as Dec\nfrom typing import TypedDict\n"
        + "class Base(TypedDict):\n    a: Dec\n    b: list['Dec']\n"
    )
    load_module(tmp_path, monkeypatch, "td_base_mod", base)
    child = future + "from typing import TypedDict\nfrom td_base_mod import Base\nclass T(Base):\n    c: int\n"
    module = load_module(tmp_path, monkeypatch, "td_child_mod", child)
    assert [(f.key, f.required) for f in row_plan(module.T).fields] == [("a", True), ("b", True), ("c", True)]


@pytest.mark.parametrize("future", FUTURE)
def test_a_dataclass_field_inherited_from_a_base_in_another_module_resolves_in_the_base_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future: str
) -> None:
    base = (
        future
        + "from dataclasses import dataclass\nfrom decimal import Decimal as Dec\n@dataclass\nclass Base:\n    a: Dec\n"
    )
    load_module(tmp_path, monkeypatch, "dc_base_mod", base)
    child = (
        future
        + "from dataclasses import dataclass\nfrom dc_base_mod import Base\n@dataclass\nclass T(Base):\n    c: int\n"
    )
    module = load_module(tmp_path, monkeypatch, "dc_child_mod", child)
    assert [f.key for f in row_plan(module.T).fields] == ["a", "c"]


TD_MODULES: Final[list[str]] = ["typing", "typing_extensions"]

# (class header, body, the expected `(key, required)` pairs)
TD_REQUIREDNESS: Final[list[tuple[str, str, list[tuple[str, bool]]]]] = [
    (
        "",
        "a: int\n    b: NotRequired[str]\n    c: NotRequired['str']\n    d: 'NotRequired[str]'",
        [("a", True), ("b", False), ("c", False), ("d", False)],
    ),
    (
        "total=False",
        "a: int\n    b: Required[str]\n    c: 'Required[str]'\n    d: Required['str']",
        [("a", False), ("b", True), ("c", True), ("d", True)],
    ),
    (
        "",
        "a: Required[ReadOnly[int]]\n    b: ReadOnly[Required[int]]\n    c: NotRequired[ReadOnly[int]]\n    d: ReadOnly[NotRequired[int]]",
        [("a", True), ("b", True), ("c", False), ("d", False)],
    ),
    (
        "total=False",
        "a: Required[ReadOnly[int]]\n    b: ReadOnly[Required[int]]\n    c: ReadOnly[int]",
        [("a", True), ("b", True), ("c", False)],
    ),
    (
        "",
        "a: Annotated[NotRequired[int], 'm']\n    b: NotRequired[Annotated[int, 'm']]\n    c: Annotated[int, 'm']",
        [("a", False), ("b", False), ("c", True)],
    ),
]


@pytest.mark.parametrize("future", FUTURE)
@pytest.mark.parametrize("module_name", TD_MODULES)
@pytest.mark.parametrize("case", TD_REQUIREDNESS)
def test_typeddict_requiredness_follows_the_resolved_qualifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future: str,
    module_name: str,
    case: tuple[str, str, list[tuple[str, bool]]],
) -> None:
    header, body, expected = case
    source = (
        future
        + f"from {module_name} import Annotated, NotRequired, ReadOnly, Required, TypedDict\n"
        + f"class T(TypedDict{', ' + header if header else ''}):\n    {body}\n"
    )
    module = load_module(tmp_path, monkeypatch, "td_required", source)
    assert [(f.key, f.required) for f in row_plan(module.T).fields] == expected


@pytest.mark.parametrize("future", FUTURE)
@pytest.mark.parametrize("module_name", TD_MODULES)
def test_typeddict_requiredness_uses_each_declaring_class_total_across_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future: str, module_name: str
) -> None:
    imports = f"from {module_name} import NotRequired, Required, TypedDict\n"
    base = future + imports + "class Base(TypedDict):\n    a: int\n    b: NotRequired[int]\n"
    load_module(tmp_path, monkeypatch, "td_total_base", base)
    child = (
        future
        + imports
        + "from td_total_base import Base\n"
        + "class T(Base, total=False):\n    c: int\n    d: Required[int]\n"
    )
    module = load_module(tmp_path, monkeypatch, "td_total_child", child)
    assert [(f.key, f.required) for f in row_plan(module.T).fields] == [
        ("a", True),
        ("b", False),
        ("c", False),
        ("d", True),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "class P2(Point): ...\nT = P2",
        "@dataclass(frozen=True)\nclass P2(Point):\n    z: int\nT = P2",
        "class S(Range[int]): ...\nT = S",
        "class I2(Interval): ...\nT = I2",
        "class M2(Money): ...\nT = M2",
        "class C2(Cube): ...\nT = C2",
        "class P2(Polygon): ...\nT = tuple[P2, int]",
        "class P2(Point): ...\nT = P2 | None",
    ],
)
def test_a_subclass_of_a_value_class_is_not_a_row_type(source: str, evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError, match=r"a subclass of rowx\.postgres\.\w+ is not a row type"):
        row_plan(evaluate(source))


@pytest.mark.parametrize(
    "source",
    [
        "@dataclass\nclass T[X]:\n    a: int",
        "G = TypeVar('G')\n@dataclass\nclass Box(typing.Generic[G]):\n    a: int\nT = Box",
        "class T[X](NamedTuple):\n    a: int",
        "class T[X](TypedDict):\n    a: int",
        "@dataclass\nclass Box[X]:\n    a: int\nT = Box[int]",
        "S = TypeVar('S')\nclass T(NamedTuple, typing.Generic[S]):\n    a: S",
    ],
)
def test_generic_row_types_are_rejected_until_189(source: str, evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError, match="generic row types are not supported yet"):
        row_plan(evaluate(source))


def test_a_quoted_initvar_alias_naming_a_missing_name_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (
        "from dataclasses import dataclass, InitVar as IVA\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from decimal import Decimal\n"
        "@dataclass\nclass T:\n    a: int\n    b: 'IVA[Decimal]'\n"
    )
    module = load_module(tmp_path, monkeypatch, "initvar_alias", source)
    with pytest.raises(InterfaceError, match="InitVar field 'b' is not supported"):
        row_plan(module.T)


def test_a_quoted_classvar_naming_a_missing_name_still_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = (
        "from dataclasses import dataclass\n"
        "from typing import TYPE_CHECKING, ClassVar\n"
        "if TYPE_CHECKING:\n    from decimal import Decimal\n"
        "@dataclass\nclass T:\n    a: int\n    b: 'ClassVar[Decimal]' = None\n"
    )
    module = load_module(tmp_path, monkeypatch, "classvar_quoted", source)
    assert [f.key for f in row_plan(module.T).fields] == ["a"]
