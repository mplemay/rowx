import importlib
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import types
from typing import TYPE_CHECKING, Final, TypeVar

import pytest

from rowx.__tests__.conformance.row_types import PRELUDE, ROWS, NamedPlan, Plan
from rowx._rowplan import MAX_ALIAS_DEPTH, NO_ROWS, RowPlan, _type_text, row_plan
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


def test_a_type_checking_only_name_behind_an_alias_names_the_field_and_the_name(evaluate: Evaluate) -> None:
    source = (
        "if typing.TYPE_CHECKING:\n    from fractions import Fraction\ntype D = Fraction\ntype E = list[D]\n"
        "@dataclass\nclass T:\n    id: int\n    ratio: E"
    )
    with pytest.raises(InterfaceError, match=r"T: field 'ratio' names 'Fraction', which is not available at runtime"):
        row_plan(evaluate(source))


def test_a_type_checking_only_name_behind_a_row_level_alias_is_an_interface_error(evaluate: Evaluate) -> None:
    source = "if typing.TYPE_CHECKING:\n    from fractions import Fraction\ntype T = Fraction"
    with pytest.raises(InterfaceError, match=r"it names 'Fraction', which is not available at runtime"):
        row_plan(evaluate(source))


def test_a_type_checking_only_name_behind_an_alias_from_another_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load_module(
        tmp_path,
        monkeypatch,
        "alias_rows",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from fractions import Fraction\ntype D = Fraction\n",
    )
    module = load_module(
        tmp_path,
        monkeypatch,
        "alias_user",
        "from dataclasses import dataclass\nfrom alias_rows import D\n@dataclass\nclass T:\n    a: D | None\n",
    )
    with pytest.raises(InterfaceError, match=r"field 'a' names 'Fraction'"):
        row_plan(module.T)


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


type Names = Callable[[str], dict[str, tuple[str, ...] | None]]


@pytest.fixture
def names(evaluate: Evaluate) -> Names:
    def run(source: str) -> dict[str, tuple[str, ...] | None]:
        return {f.key: f.names for f in row_plan(evaluate(source)).fields}

    return run


def test_a_pydantic_model_is_a_validator_plan_over_its_validator(evaluate: Evaluate) -> None:
    t = evaluate("class T(BaseModel):\n    id: int\n    name: str | None")
    plan = row_plan(t)
    assert (plan.kind, plan.form, plan.matching, plan.type_name) == ("named", "validator", "by_name", "T")
    assert plan.shape.ctor is t.__pydantic_validator__  # ty: ignore[unresolved-attribute]
    assert plan.nullable_mask == (False, True)


def test_a_pydantic_dataclass_is_called_with_keywords(evaluate: Evaluate) -> None:
    t = evaluate("@pydantic.dataclasses.dataclass\nclass T:\n    id: int\n    name: str = 'a'")
    plan = row_plan(t)
    assert (plan.form, plan.matching) == ("validator", "by_name")
    assert plan.shape.ctor is t  # ty: ignore[unresolved-attribute]
    assert [(f.key, f.required) for f in plan.fields] == [("id", True), ("name", False)]


def test_an_inherited_model_keeps_the_base_fields_first(names: Names) -> None:
    source = "class Base(BaseModel):\n    id: int\nclass T(Base):\n    name: str"
    assert names(source) == {"id": ("id",), "name": ("name",)}


def test_a_pydantic_dataclass_that_inherits_a_stdlib_dataclass_is_a_pydantic_one(evaluate: Evaluate) -> None:
    source = "@dataclass\nclass Base:\n    id: int\n@pydantic.dataclasses.dataclass\nclass T(Base):\n    name: str"
    plan = row_plan(evaluate(source))
    assert plan.form == "validator"
    assert [f.key for f in plan.fields] == ["id", "name"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("class T(BaseModel):\n    id: int = Field(alias='a')", ("a",)),
        ("class T(BaseModel):\n    id: int = Field(validation_alias='v')", ("v",)),
        ("class T(BaseModel):\n    id: int = Field(alias='a', validation_alias='v')", ("v",)),
        ("class T(BaseModel):\n    id: Annotated[int, Field(alias='a')]", ("a",)),
        ("class T(BaseModel):\n    id: Annotated[int | None, Field(alias='a')] = None", ("a",)),
        ("class T(BaseModel, populate_by_name=True):\n    id: int = Field(alias='a')", ("a", "id")),
        ("class T(BaseModel, validate_by_name=True):\n    id: int = Field(alias='a')", ("a", "id")),
        (
            "class T(BaseModel):\n    model_config = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='a')",
            ("a", "id"),
        ),
        ("class T(BaseModel, validate_by_alias=False):\n    id: int = Field(alias='a')", ("id",)),
        ("class T(BaseModel, populate_by_name=True):\n    id: int", ("id",)),
        ("class T(BaseModel):\n    id: int = Field(alias='id')", ("id",)),
        (
            "class T(BaseModel):\n    model_config = ConfigDict(alias_generator=lambda name: name.upper())\n    id: int",
            ("ID",),
        ),
        (
            (
                "class T(BaseModel):\n    model_config = ConfigDict(alias_generator=lambda name: 'x_' + name,"
                " populate_by_name=True)\n    id: int"
            ),
            ("x_id", "id"),
        ),
        (
            (
                "@pydantic.dataclasses.dataclass(config=ConfigDict(populate_by_name=True))\nclass T:\n"
                "    id: int = Field(alias='a')"
            ),
            ("a", "id"),
        ),
        ("@pydantic.dataclasses.dataclass\nclass T:\n    id: int = Field(alias='a')", ("a",)),
    ],
)
def test_a_pydantic_field_accepts_its_alias_and_its_name_by_config(
    source: str, expected: tuple[str, ...], names: Names
) -> None:
    assert names(source) == {"id": expected}


def test_a_child_model_class_keyword_overrides_the_base_config(names: Names) -> None:
    source = (
        "class Base(BaseModel, populate_by_name=True):\n    id: int = Field(alias='a')\n"
        "class T(Base, populate_by_name=False):\n    pass"
    )
    assert names(source) == {"id": ("a",)}


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ("populate_by_name=True, validate_by_name=False", ("a",)),
        ("validate_by_alias=False, populate_by_name=True", ("a", "id")),
        ("validate_by_alias=False, populate_by_name=False", ("a",)),
        ("validate_by_alias=False, validate_by_name=True", ("id",)),
        ("validate_by_alias=False", ("id",)),
    ],
)
@pytest.mark.parametrize("spelling", ["keyword", "model_config"])
def test_pydantic_name_config_follows_the_effective_config_precedence(
    config: str, expected: tuple[str, ...], spelling: str, names: Names
) -> None:
    if spelling == "keyword":
        source = f"class T(BaseModel, {config}):\n    id: int = Field(alias='a')"
    else:
        source = f"class T(BaseModel):\n    model_config = ConfigDict({config})\n    id: int = Field(alias='a')"
    assert names(source) == {"id": expected}


def test_a_child_populate_by_name_does_not_override_an_inherited_validate_by_name(names: Names) -> None:
    source = (
        "class Base(BaseModel, validate_by_name=True):\n    id: int = Field(alias='a')\n"
        "class T(Base, populate_by_name=False):\n    pass"
    )
    assert names(source) == {"id": ("a", "id")}


def test_a_pydantic_dataclass_follows_the_effective_config_precedence(names: Names) -> None:
    source = (
        "@pydantic.dataclasses.dataclass(config=ConfigDict(populate_by_name=True, validate_by_name=False))\n"
        "class T:\n    id: int = Field(alias='a')"
    )
    assert names(source) == {"id": ("a",)}


def test_a_serialization_alias_does_not_change_the_names(names: Names) -> None:
    assert names("class T(BaseModel):\n    id: int = Field(serialization_alias='s')") == {"id": ("id",)}


def test_an_alias_generator_object_with_a_validation_alias_gives_the_generated_names(names: Names) -> None:
    source = (
        "class T(BaseModel):\n    model_config = ConfigDict(alias_generator=pydantic.AliasGenerator("
        "validation_alias=lambda name: 'v_' + name))\n    id: int"
    )
    assert names(source) == {"id": ("v_id",)}


def test_an_excluded_field_is_still_a_field(names: Names) -> None:
    assert names("class T(BaseModel):\n    id: int\n    n: int = Field(0, exclude=True)") == {
        "id": ("id",),
        "n": ("n",),
    }


def test_init_false_on_a_model_field_leaves_it_required(evaluate: Evaluate) -> None:
    plan = row_plan(evaluate("class T(BaseModel):\n    id: int\n    count: int = Field(init=False)"))
    assert [(f.key, f.required) for f in plan.fields] == [("id", True), ("count", True)]


def test_init_false_with_a_default_on_a_model_field_is_optional(evaluate: Evaluate) -> None:
    plan = row_plan(evaluate("class T(BaseModel):\n    id: int\n    count: int = Field(0, init=False)"))
    assert [(f.key, f.required) for f in plan.fields] == [("id", True), ("count", False)]


def test_init_var_on_a_model_field_is_a_normal_field(names: Names) -> None:
    assert names("class T(BaseModel):\n    id: int\n    n: int = Field(0, init_var=True)") == {
        "id": ("id",),
        "n": ("n",),
    }


def test_a_forward_reference_is_not_resolved_against_the_planners_locals(evaluate: Evaluate) -> None:
    t = evaluate("class T(BaseModel):\n    id: 't'\nt = int")
    plan = row_plan(t)
    assert plan.kind == "named"
    assert t.model_fields["id"].annotation is int  # ty: ignore[unresolved-attribute]


def test_a_model_incomplete_for_a_reason_other_than_a_name_is_an_interface_error_with_its_cause(
    evaluate: Evaluate,
) -> None:
    source = "class T(BaseModel):\n    id: 'Plain'\nclass Plain:\n    pass"
    with pytest.raises(InterfaceError, match="T is not fully defined") as caught:
        row_plan(evaluate(source))
    assert caught.value.__cause__ is not None


def test_a_missing_name_hints_at_model_rebuild(evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError, match=r"call model_rebuild\(\) where the name is in scope"):
        row_plan(evaluate("class T(BaseModel):\n    id: 'Never'"))


@pytest.mark.parametrize(
    "source",
    [
        "class T(BaseModel):\n    id: int = Field(validation_alias=AliasChoices('a', 'b'))",
        "class T(BaseModel):\n    id: int = Field(validation_alias=AliasPath('a', 0))",
    ],
)
def test_alias_choices_and_alias_path_make_only_that_field_unknown(source: str, names: Names) -> None:
    source = source.replace("\n    id", "\n    other: str\n    id", 1)
    source = "from pydantic import AliasPath\n" + source
    assert names(source) == {"other": ("other",), "id": None}


@pytest.mark.parametrize("mode", ["before", "wrap"])
def test_a_before_or_wrap_model_validator_makes_every_field_unknown(mode: str, names: Names) -> None:
    wrap = ", handler" if mode == "wrap" else ""
    ret = "handler(data)" if mode == "wrap" else "data"
    source = (
        f"class T(BaseModel):\n    id: int\n    n: str\n    @model_validator(mode='{mode}')\n    @classmethod\n"
        f"    def check(cls, data{wrap}):\n        return {ret}"
    )
    assert names(source) == {"id": None, "n": None}


def test_a_before_validator_inherited_from_a_base_makes_every_field_unknown(names: Names) -> None:
    source = (
        "class Base(BaseModel):\n    @model_validator(mode='before')\n    @classmethod\n"
        "    def check(cls, data):\n        return data\nclass T(Base):\n    id: int"
    )
    assert names(source) == {"id": None}


def test_an_after_model_validator_keeps_the_names_known(names: Names) -> None:
    source = "class T(BaseModel):\n    id: int\n    @model_validator(mode='after')\n    def check(self):\n        return self"
    assert names(source) == {"id": ("id",)}


def test_unknown_names_still_drive_the_null_check_flag(evaluate: Evaluate) -> None:
    plan = row_plan(evaluate("class T(BaseModel):\n    id: int = Field(validation_alias=AliasChoices('a', 'b'))"))
    assert [(f.names, f.nullable) for f in plan.fields] == [(None, False)]


def test_pydantic_members_that_are_not_fields_are_excluded(names: Names) -> None:
    source = (
        "class T(BaseModel):\n    id: int\n    _secret: int = 0\n    _hidden: int = pydantic.PrivateAttr(default=1)\n"
        "    kind: ClassVar[str] = 'x'\n    @pydantic.computed_field\n    @property\n    def total(self) -> int:\n"
        "        return 1"
    )
    assert names(source) == {"id": ("id",)}


def test_a_pydantic_dataclass_field_with_init_false_is_not_a_field(names: Names) -> None:
    source = "@pydantic.dataclasses.dataclass\nclass T:\n    id: int\n    derived: int = Field(default=1, init=False)"
    assert names(source) == {"id": ("id",)}


def test_pydantic_defaults_are_not_required(evaluate: Evaluate) -> None:
    source = (
        "class T(BaseModel):\n    a: int\n    b: int = 1\n    c: list[int] = Field(default_factory=list)\n"
        "    d: int | None = None\n    e: int | None"
    )
    plan = row_plan(evaluate(source))
    assert [(f.key, f.required) for f in plan.fields] == [
        ("a", True),
        ("b", False),
        ("c", False),
        ("d", False),
        ("e", True),
    ]


def test_a_model_with_a_forward_reference_resolved_later_is_rebuilt(evaluate: Evaluate) -> None:
    t = evaluate("class T(BaseModel):\n    id: 'Later'\nLater = int")
    assert not t.__pydantic_complete__  # ty: ignore[unresolved-attribute]
    plan = row_plan(t)
    assert t.__pydantic_complete__  # ty: ignore[unresolved-attribute]
    assert plan.shape.ctor is t.__pydantic_validator__  # ty: ignore[unresolved-attribute]
    assert plan.kind == "named"


def test_a_pydantic_dataclass_with_a_forward_reference_resolved_later_is_rebuilt(evaluate: Evaluate) -> None:
    t = evaluate("@pydantic.dataclasses.dataclass\nclass T:\n    id: 'Later'\nLater = int")
    assert row_plan(t).kind == "named"
    assert t.__pydantic_complete__  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize(
    "source",
    [
        "class T(BaseModel):\n    id: 'Never'",
        "@pydantic.dataclasses.dataclass\nclass T:\n    id: 'Never'",
    ],
)
def test_a_model_that_stays_incomplete_is_an_interface_error_naming_the_missing_name(
    source: str, evaluate: Evaluate
) -> None:
    with pytest.raises(InterfaceError, match=r"T is not fully defined \(it needs 'Never'\)"):
        row_plan(evaluate(source))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("class T(RootModel[int]): ...", "RootModel"),
        ("import pydantic.v1\nclass T(pydantic.v1.BaseModel):\n    id: int", "pydantic v1"),
        ("class T(BaseModel):\n    url: AnyUrl", "not derivable"),
        ("class T(BaseModel):\n    secret: SecretStr", "not derivable"),
        ("class T(BaseModel): ...", "has no fields"),
        ("class T(BaseModel):\n    _only: int = 0", "has no fields"),
        ("@pydantic.dataclasses.dataclass\nclass T:\n    id: int\n    scale: InitVar[int]", "InitVar"),
        (
            "class U(BaseModel):\n    id: int\nclass T(BaseModel):\n    u: U",
            "named row type is only allowed as the row",
        ),
        ("class T[X](BaseModel):\n    id: int", "is not bound"),
    ],
)
def test_pydantic_rejections_have_their_messages(source: str, message: str, evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError, match=message):
        row_plan(evaluate(source))


def test_email_str_is_rejected_when_the_optional_package_is_installed(evaluate: Evaluate) -> None:
    pytest.importorskip("email_validator")
    with pytest.raises(InterfaceError, match="not derivable"):
        row_plan(evaluate("class T(BaseModel):\n    email: pydantic.EmailStr"))


@pytest.mark.parametrize(
    "annotation",
    ["PositiveInt", "pydantic.constr(min_length=1)", "Annotated[int, Field(gt=0)]", "pydantic.conint(gt=0) | None"],
)
def test_pydantic_constraint_aliases_are_accepted_at_runtime(annotation: str, evaluate: Evaluate) -> None:
    plan = row_plan(evaluate(f"class T(BaseModel):\n    id: {annotation}"))
    assert plan.kind == "named"


def test_value_classes_are_accepted_as_pydantic_field_types(evaluate: Evaluate) -> None:
    source = "class T(BaseModel):\n    i: Interval\n    r: Range[int]\n    m: Money | None\n    p: Point\n    c: Cube"
    plan = row_plan(evaluate(source))
    assert plan.interval_mask == (True, False, False, False, False)
    assert plan.nullable_mask == (False, False, True, False, False)


def test_a_stdlib_dataclass_is_not_taken_for_a_pydantic_one(evaluate: Evaluate) -> None:
    plan = row_plan(evaluate("@dataclass\nclass T:\n    id: int"))
    assert plan.form == "keyword"


NEVER_IMPORTS_PYDANTIC: Final[str] = """
import sys
from dataclasses import dataclass
from typing import NamedTuple, TypedDict
from rowx._rowplan import row_plan

@dataclass
class D:
    id: int

class N(NamedTuple):
    id: int

class Z(TypedDict):
    id: int

def stdlib(name):
    return name.partition(".")[0] in sys.stdlib_module_names

before = set(sys.modules)
for t in (int, tuple[int, str]):
    row_plan(t)
assert set(sys.modules) == before, sorted(set(sys.modules) - before)
for t in (D, N, Z):
    row_plan(t)
# a named plan may import rowx modules and the stdlib that package init pulls in
extra = sorted(m for m in set(sys.modules) - before if not m.startswith("rowx.") and not stdlib(m))
assert not extra, extra
assert "pydantic" not in sys.modules, "pydantic was imported"
"""


def test_planning_never_imports_pydantic() -> None:
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-I", "-c", NEVER_IMPORTS_PYDANTIC],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
        "S = TypeVar('S')\nclass T(NamedTuple, typing.Generic[S]):\n    a: S",
    ],
)
def test_a_generic_row_type_that_is_not_subscripted_has_an_unbound_parameter(source: str, evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError, match="is not bound; subscript it"):
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


type Swap[A, B] = tuple[B, A]
type Pair[A, B] = tuple[A, B]
type Wrap[X] = tuple[X, str]
type Defaulted[X = int] = tuple[X, str]
type Partly[A, B = str] = tuple[A, B]
type Variadic[*Ts] = tuple[int, str]
type Spec[**P] = tuple[int, str]

CRATE: Final[str] = "@dataclass\nclass Crate[X]:\n    a: X\n    b: str\n"


def test_a_generic_alias_is_substituted_through_its_type_parameters_not_the_order_of_use() -> None:
    assert row_plan(Swap[int, str | None]).nullable_mask == (True, False)
    assert row_plan(Pair[int, str | None]).nullable_mask == (False, True)


def test_a_generic_alias_is_substituted_recursively() -> None:
    type Nested[X] = tuple[list[X | None], X | None]
    type Outer[Y] = Wrap[Y | None]
    assert row_plan(Nested[int]).nullable_mask == (False, True)
    assert row_plan(Outer[int]).nullable_mask == (True, False)


def test_a_scalar_generic_alias_is_a_scalar_plan() -> None:
    type Opt[X] = X | None
    plan = row_plan(Opt[int])
    assert (plan.kind, plan.nullable_mask) == ("scalar", (True,))


def test_a_bare_generic_alias_takes_its_defaults() -> None:
    assert row_plan(Defaulted).nullable_mask == (False, False)
    assert row_plan(Partly[int]).nullable_mask == (False, False)


def test_a_bare_generic_alias_without_defaults_is_unbound() -> None:
    with pytest.raises(InterfaceError, match=r"the type parameter `X` of `Wrap` is not bound; subscript it"):
        row_plan(Wrap)


def test_an_empty_subscript_of_a_plain_generic_alias_takes_its_defaults() -> None:
    A = TypeVar("A", default=str)
    plain = tuple[A, int]
    assert row_plan(plain[()]).arity == 2
    with pytest.raises(InterfaceError, match="the type parameter `A` is not bound"):
        row_plan(plain)


def test_a_plain_assignment_of_a_generic_alias_is_the_same_generic_alias() -> None:
    A = TypeVar("A")
    pair = tuple[A, str]
    outer = pair
    assert row_plan(outer[int | None]).nullable_mask == (True, False)
    with pytest.raises(InterfaceError, match="the type parameter `A` is not bound"):
        row_plan(outer)


def test_an_ellipsis_argument_is_shown_as_dots() -> None:
    assert _type_text(tuple[int, ...]) == "tuple[int, ...]"


@pytest.mark.parametrize(
    ("t", "message"),
    [
        (Swap[int], "`Swap` takes 2 type arguments, but 1 were given"),  # ty: ignore[invalid-type-arguments]
        (Swap[int, str, bytes], "`Swap` takes 2 type arguments, but 3 were given"),  # ty: ignore[invalid-type-arguments]
        (Partly[int, str, bytes], "`Partly` takes 1 to 2 type arguments, but 3 were given"),  # ty: ignore[invalid-type-arguments]
        (Variadic[int], "the TypeVarTuple `Ts`"),
        (Spec[[int]], "the ParamSpec `P`"),
    ],
)
def test_a_generic_alias_is_rejected_for_a_wrong_count_or_an_unsupported_parameter(t: object, message: str) -> None:
    with pytest.raises(InterfaceError, match=message):
        row_plan(t)


def test_an_unbound_type_variable_is_rejected() -> None:
    with pytest.raises(InterfaceError, match="the type parameter `Y` is not bound"):
        row_plan(TypeVar("Y"))  # ty: ignore[invalid-legacy-type-variable]


def test_a_type_variable_bound_or_constraint_is_not_enforced() -> None:
    type Bounded[X: str] = tuple[X, str]
    type Constrained[X: (int, str)] = tuple[X, str]
    assert row_plan(Bounded[int]).nullable_mask == (False, False)  # ty: ignore[invalid-type-arguments]
    assert row_plan(Constrained[bytes]).nullable_mask == (False, False)  # ty: ignore[invalid-type-arguments]


def test_a_generic_dataclass_is_built_by_the_origin_class_and_named_with_its_arguments(evaluate: Evaluate) -> None:
    crate = evaluate(CRATE + "T = Crate")
    plan = row_plan(crate[int | None])  # ty: ignore[not-subscriptable]
    assert plan.type_name == "Crate[int | None]"
    assert plan.shape is not None
    # calling the alias would set `__orig_class__` on every row
    assert plan.shape.ctor is crate
    assert [(f.key, f.nullable) for f in plan.fields] == [("a", True), ("b", False)]


@pytest.mark.parametrize(
    ("argument", "name"),
    [
        ("int", "int"),
        ("datetime.date", "date"),
        ("list[int]", "list[int]"),
        ("int | None", "int | None"),
        ("Range[datetime.date]", "Range[date]"),
        ("list[datetime.date | None]", "list[date | None]"),
    ],
)
def test_the_type_name_renders_the_arguments(argument: str, name: str, evaluate: Evaluate) -> None:
    assert row_plan(evaluate(f"{CRATE}T = Crate[{argument}]")).type_name == f"Crate[{name}]"


def test_the_type_name_of_two_parameters_and_of_defaults(evaluate: Evaluate) -> None:
    two = "@dataclass\nclass Crate[A, B = str]:\n    a: A\n    b: B\n"
    assert row_plan(evaluate(f"{two}T = Crate[int]")).type_name == "Crate[int, str]"
    assert row_plan(evaluate(f"{two}T = Crate[int, bytes]")).type_name == "Crate[int, bytes]"
    assert row_plan(evaluate("@dataclass\nclass Crate[X = int]:\n    a: X\nT = Crate")).type_name == "Crate[int]"


def test_a_generic_row_plan_is_cached_per_subscripted_type(evaluate: Evaluate) -> None:
    crate = evaluate(CRATE + "T = Crate")
    first = row_plan(crate[int])  # ty: ignore[not-subscriptable]
    assert row_plan(crate[int]) is first  # ty: ignore[not-subscriptable]
    other = row_plan(crate[int | None])  # ty: ignore[not-subscriptable]
    assert other is not first
    assert other.nullable_mask == (True, False)


def test_a_generic_alias_of_a_generic_class(evaluate: Evaluate) -> None:
    plan = row_plan(evaluate(CRATE + "type B[Y] = Crate[Y | None]\nT = B[int]"))
    assert plan.type_name == "Crate[int | None]"
    assert [f.nullable for f in plan.fields] == [True, False]


def test_inherited_parameters_are_rebound_through_the_bases(evaluate: Evaluate) -> None:
    source = (
        "@dataclass\nclass Base[A, B]:\n    a: A\n    b: B\n"
        "@dataclass\nclass Child[U](Base[U | None, str]):\n    c: U\nT = Child[int]"
    )
    plan = row_plan(evaluate(source))
    assert plan.type_name == "Child[int]"
    assert [(f.key, f.nullable) for f in plan.fields] == [("a", True), ("b", False), ("c", False)]


def test_inherited_parameters_of_a_generic_base_class_are_rebound(evaluate: Evaluate) -> None:
    source = (
        "A = TypeVar('A')\n@dataclass\nclass Base(typing.Generic[A]):\n    a: A\n"
        "@dataclass\nclass Child(Base[int | None]):\n    b: str\nT = Child"
    )
    assert [(f.key, f.nullable) for f in row_plan(evaluate(source)).fields] == [("a", True), ("b", False)]


def test_a_bare_generic_base_without_defaults_is_unbound(evaluate: Evaluate) -> None:
    source = "@dataclass\nclass Base[X]:\n    a: X\n@dataclass\nclass Child(Base):\n    b: int\nT = Child"
    with pytest.raises(InterfaceError, match="the type parameter `X` of `Base` is not bound"):
        row_plan(evaluate(source))


def test_a_quoted_field_annotation_names_the_type_parameter(evaluate: Evaluate) -> None:
    plan = row_plan(evaluate("@dataclass\nclass Crate[X]:\n    a: 'X | None'\nT = Crate[int]"))
    assert [f.nullable for f in plan.fields] == [True]


def test_a_generic_typeddict_and_namedtuple_inherit_their_parameters(evaluate: Evaluate) -> None:
    typeddict = evaluate(
        "class Base[X](TypedDict):\n    a: X\nclass Child[U](Base[U | None], TypedDict):\n    b: NotRequired[U]\nT = Child[int]"
    )
    plan = row_plan(typeddict)
    assert [(f.key, f.nullable, f.required) for f in plan.fields] == [("a", True, True), ("b", False, False)]
    assert row_plan(evaluate("class Pt[X](NamedTuple):\n    a: X\n    b: str\nT = Pt[int | None]")).nullable_mask == (
        True,
        False,
    )


def test_a_subscripted_pydantic_model_is_used_as_it_is(evaluate: Evaluate) -> None:
    page = evaluate("class Page[X](BaseModel):\n    a: X\nT = Page")
    plan = row_plan(page[int])  # ty: ignore[not-subscriptable]
    assert plan.type_name == "Page[int]"
    assert plan.shape is not None
    assert plan.shape.ctor is page[int].__pydantic_validator__  # ty: ignore[not-subscriptable]


def test_a_bare_pydantic_model_takes_its_defaults_or_is_unbound(evaluate: Evaluate) -> None:
    assert row_plan(evaluate("class Page[X = int](BaseModel):\n    a: X\nT = Page")).type_name == "Page[int]"
    with pytest.raises(InterfaceError, match="the type parameter `X` of `Page` is not bound"):
        row_plan(evaluate("class Page[X](BaseModel):\n    a: X\nT = Page"))


def test_a_generic_pydantic_dataclass_is_called_as_the_origin_class(evaluate: Evaluate) -> None:
    crate = evaluate("@pydantic.dataclasses.dataclass\nclass Crate[X]:\n    a: X\nT = Crate")
    plan = row_plan(crate[int | None])  # ty: ignore[not-subscriptable]
    assert plan.type_name == "Crate[int | None]"
    assert plan.shape is not None
    assert plan.shape.ctor is crate
    assert [f.nullable for f in plan.fields] == [True]


def chain(length: int) -> str:
    lines = ["@dataclass\nclass C0[X]:\n    a: X"]
    lines += [f"@dataclass\nclass C{index}[X](C{index - 1}[X]):\n    f{index}: X" for index in range(1, length)]
    return "\n".join(lines) + f"\nT = C{length - 1}[int]"


def test_generic_bases_are_followed_to_the_depth_limit(evaluate: Evaluate) -> None:
    assert len(row_plan(evaluate(chain(MAX_ALIAS_DEPTH + 1))).fields) == MAX_ALIAS_DEPTH + 1


def test_generic_bases_deeper_than_the_depth_limit_are_rejected(evaluate: Evaluate) -> None:
    with pytest.raises(InterfaceError, match=f"generic bases deeper than {MAX_ALIAS_DEPTH}"):
        row_plan(evaluate(chain(MAX_ALIAS_DEPTH + 2)))


def test_a_generic_alias_chain_is_followed_to_the_depth_limit_and_no_further() -> None:
    namespace: dict[str, object] = {}
    lines = ["type A0[X] = tuple[X, str]"] + [f"type A{i}[X] = A{i - 1}[X]" for i in range(1, MAX_ALIAS_DEPTH + 1)]
    exec("\n".join(lines), namespace)  # ruff: ignore[exec-builtin]
    assert row_plan(namespace[f"A{MAX_ALIAS_DEPTH - 1}"][int]).arity == 2  # ty: ignore[not-subscriptable]
    with pytest.raises(InterfaceError, match=f"alias chain deeper than {MAX_ALIAS_DEPTH}"):
        row_plan(namespace[f"A{MAX_ALIAS_DEPTH}"][int])  # ty: ignore[not-subscriptable]
