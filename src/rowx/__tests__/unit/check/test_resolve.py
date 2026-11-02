import sys
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from rowx.check.discover import discover_file
from rowx.check.resolve import ModuleGraph, Resolution, RowType, resolve_row_type

if TYPE_CHECKING:
    from pathlib import Path


def write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(source))


def resolve(root: Path, name: str, *, roots: tuple[Path, ...] | None = None) -> Resolution:
    result = discover_file(root / name)
    assert not result.diagnostics
    [call] = result.calls
    return resolve_row_type(ModuleGraph(), call, roots or (root,))


def row_type(resolution: Resolution) -> RowType:
    assert resolution.diagnostic is None
    assert resolution.row_type is not None
    return resolution.row_type


def leaves(resolution: Resolution) -> list[tuple[str, bool]]:
    return [(elem.leaf, elem.nullable) for elem in row_type(resolution).elements]


QUERY = 'from rowx import query\nquery[{t}](t"select 1")\n'


def test_cross_module_alias_with_note(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "models.py": "import datetime as dt\ntype Row = tuple[int, dt.date | None]\n",
            "main.py": "from models import Row\n" + QUERY.format(t="Row"),
        },
    )
    resolution = resolve(tmp_path, "main.py")
    assert leaves(resolution) == [("int", False), ("datetime.date", True)]
    alias = row_type(resolution).alias
    assert alias is not None
    assert (alias.path.name, alias.span.line, alias.span.col) == ("models.py", 2, 1)


def test_relative_import_and_init_reexport(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "from .rows import Pair\n",
            "pkg/rows.py": "type Pair = tuple[str, bytes]\n",
            "pkg/use.py": "from . import Pair\n" + QUERY.format(t="Pair"),
            "top.py": "import pkg\n" + QUERY.format(t="pkg.Pair"),
            "alt.py": "from pkg import rows as r\n" + QUERY.format(t="r.Pair"),
        },
    )
    for name in ("pkg/use.py", "top.py", "alt.py"):
        assert leaves(resolve(tmp_path, name)) == [("str", False), ("bytes", False)]


def test_star_import_uses_all(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "shared.py": '__all__ = ["Public"]\ntype Public = int\ntype Hidden = str\n',
            "main.py": "from shared import *\n" + QUERY.format(t="Public"),
            "hidden.py": "from shared import *\n" + QUERY.format(t="Hidden"),
        },
    )
    assert leaves(resolve(tmp_path, "main.py")) == [("int", False)]
    assert resolve(tmp_path, "hidden.py").diagnostic.rule == "unresolved-row-type"  # ty: ignore[unresolved-attribute]


def test_src_layout_root(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "src/app/__init__.py": "",
            "src/app/types.py": "Id = int\n",
            "src/app/db.py": "from app.types import Id\n" + QUERY.format(t="Id"),
        },
    )
    assert leaves(resolve(tmp_path, "src/app/db.py", roots=(tmp_path / "src",))) == [("int", False)]
    assert resolve(tmp_path, "src/app/db.py", roots=(tmp_path,)).diagnostic is not None


def test_cycle_is_unresolved(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "a.py": "from b import X\n",
            "b.py": "from a import X\n",
            "main.py": "from a import X\n" + QUERY.format(t="X"),
            "self.py": "type X = list[X]\n" + QUERY.format(t="X"),
        },
    )
    for name in ("main.py", "self.py"):
        diagnostic = resolve(tmp_path, name).diagnostic
        assert diagnostic is not None
        assert diagnostic.rule == "unresolved-row-type"


def test_hop_limit(tmp_path: Path) -> None:
    files = {f"m{i}.py": f"from m{i + 1} import X\n" for i in range(20)}
    files["m20.py"] = "type X = int\n"
    files["main.py"] = "from m0 import X\n" + QUERY.format(t="X")
    write(tmp_path, files)
    diagnostic = resolve(tmp_path, "main.py").diagnostic
    assert diagnostic is not None
    assert diagnostic.rule == "unresolved-row-type"
    assert "hops" in diagnostic.message


def test_generic_alias_is_invalid(tmp_path: Path) -> None:
    write(tmp_path, {"main.py": "type Row[X] = tuple[X, str]\n" + QUERY.format(t="Row[int]")})
    diagnostic = resolve(tmp_path, "main.py").diagnostic
    assert diagnostic is not None
    assert diagnostic.rule == "invalid-row-type"
    assert diagnostic.span.line == 3


def test_multiple_bindings_are_unresolved(tmp_path: Path) -> None:
    write(tmp_path, {"main.py": "X = int\nX = str\n" + QUERY.format(t="X")})
    diagnostic = resolve(tmp_path, "main.py").diagnostic
    assert diagnostic is not None
    assert diagnostic.rule == "unresolved-row-type"


def test_type_checking_first_binding_wins(tmp_path: Path) -> None:
    source = """
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            type X = int
        else:
            type X = str
    """
    write(tmp_path, {"main.py": dedent(source) + QUERY.format(t="X")})
    assert leaves(resolve(tmp_path, "main.py")) == [("int", False)]


def test_alias_forms(tmp_path: Path) -> None:
    source = """
        from typing import TypeAlias
        A: TypeAlias = int | None
        B = A
    """
    write(tmp_path, {"main.py": dedent(source) + QUERY.format(t="tuple[B, A]")})
    assert leaves(resolve(tmp_path, "main.py")) == [("int", True), ("int", True)]


def test_call_alias_is_invalid(tmp_path: Path) -> None:
    write(tmp_path, {"main.py": "from typing import TypeVar\nT = TypeVar('T')\n" + QUERY.format(t="T")})
    diagnostic = resolve(tmp_path, "main.py").diagnostic
    assert diagnostic is not None
    assert diagnostic.rule == "invalid-row-type"


def test_unbound_name_is_unresolved(tmp_path: Path) -> None:
    write(tmp_path, {"main.py": QUERY.format(t="Missing")})
    diagnostic = resolve(tmp_path, "main.py").diagnostic
    assert diagnostic is not None
    assert diagnostic.rule == "unresolved-row-type"


def test_missing_row_type_is_none(tmp_path: Path) -> None:
    write(tmp_path, {"main.py": 'from rowx import query\nquery(t"select 1")\n'})
    assert row_type(resolve(tmp_path, "main.py")).kind == "none"


def test_fixture_module_is_never_executed(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "boom.py": "raise SystemExit(3)\ntype Row = int\n",
            "main.py": "from boom import Row\n" + QUERY.format(t="Row"),
        },
    )
    before = set(sys.modules)
    assert leaves(resolve(tmp_path, "main.py")) == [("int", False)]
    assert set(sys.modules) == before
    assert "boom" not in sys.modules


def test_local_binding_shadows_module_name(tmp_path: Path) -> None:
    source = """
        from rowx import query

        Row = int

        def f():
            Row = str
            return query[Row](t"select 1")

        def g(Row):
            return query[Row](t"select 1")

        def h():
            return query[Row](t"select 1")
    """
    write(tmp_path, {"main.py": source})
    calls = discover_file(tmp_path / "main.py").calls
    graph = ModuleGraph()
    results = [resolve_row_type(graph, call, (tmp_path,)) for call in calls]
    assert [r.diagnostic.rule if r.diagnostic else None for r in results] == [
        "unresolved-row-type",
        "unresolved-row-type",
        None,
    ]


def test_conflicting_star_imports_are_unresolved(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "a.py": "Row = int\n",
            "b.py": "Row = str\n",
            "main.py": "from a import *\nfrom b import *\n" + QUERY.format(t="Row"),
        },
    )
    diagnostic = resolve(tmp_path, "main.py").diagnostic
    assert diagnostic is not None
    assert diagnostic.rule == "unresolved-row-type"


def test_plain_alias_of_constructor_is_subscriptable(tmp_path: Path) -> None:
    write(tmp_path, {"main.py": "L = list\n" + QUERY.format(t="L[int]")})
    resolution = resolve(tmp_path, "main.py")
    assert row_type(resolution).elements[0].leaf == "list"
    write(tmp_path, {"typed.py": "type L = list\n" + QUERY.format(t="L[int]")})
    assert resolve(tmp_path, "typed.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_network_pair_is_one_leaf_in_either_order(tmp_path: Path) -> None:
    pair = "ipaddress.IPv6Interface | ipaddress.IPv4Interface"
    write(tmp_path, {"main.py": "import ipaddress\n" + QUERY.format(t=f"tuple[{pair}, {pair} | None]")})
    leaf = "ipaddress.IPv4Interface | ipaddress.IPv6Interface"
    assert leaves(resolve(tmp_path, "main.py")) == [(leaf, False), (leaf, True)]


def test_network_pair_through_an_alias_and_optional(tmp_path: Path) -> None:
    source = "import ipaddress\ntype Net = ipaddress.IPv4Network | ipaddress.IPv6Network\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="list[Net | None]")})
    [elem] = row_type(resolve(tmp_path, "main.py")).elements
    assert elem.leaf == "list"
    assert elem.item is not None
    assert (elem.item.leaf, elem.item.nullable) == ("ipaddress.IPv4Network | ipaddress.IPv6Network", True)


def test_a_lone_network_class_or_mixed_pair_is_invalid(tmp_path: Path) -> None:
    for index, annotation in enumerate([
        "ipaddress.IPv4Interface",
        "ipaddress.IPv4Interface | ipaddress.IPv6Network",
        "ipaddress.IPv4Interface | ipaddress.IPv6Interface | str",
        "ipaddress.IPv4Address | ipaddress.IPv6Address",
    ]):
        write(tmp_path, {f"main{index}.py": "import ipaddress\n" + QUERY.format(t=annotation)})
        diagnostic = resolve(tmp_path, f"main{index}.py").diagnostic
        assert diagnostic is not None, annotation
        assert diagnostic.rule == "invalid-row-type", annotation


def test_network_pair_split_across_nested_aliases_is_one_nullable_leaf(tmp_path: Path) -> None:
    source = "import ipaddress\ntype V4 = ipaddress.IPv4Interface | None\ntype V6 = ipaddress.IPv6Interface\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="V4 | V6")})
    assert leaves(resolve(tmp_path, "main.py")) == [("ipaddress.IPv4Interface | ipaddress.IPv6Interface", True)]


def test_range_leaf_per_subtype_through_an_alias_and_optional(tmp_path: Path) -> None:
    source = "import datetime\nfrom rowx.postgres import Range\ntype Dates = Range[datetime.date]\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Dates | None, Range[datetime.datetime], Range[int]]")})
    assert leaves(resolve(tmp_path, "main.py")) == [
        ("rowx.postgres.Range[datetime.date]", True),
        ("rowx.postgres.Range[datetime.datetime]", False),
        ("rowx.postgres.Range[int]", False),
    ]


def test_range_through_its_defining_module_and_a_list(tmp_path: Path) -> None:
    source = "from rowx.postgres._range import Range\nimport decimal\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="list[Range[decimal.Decimal] | None]")})
    [elem] = row_type(resolve(tmp_path, "main.py")).elements
    assert elem.leaf == "list"
    assert elem.item is not None
    assert (elem.item.leaf, elem.item.nullable) == ("rowx.postgres.Range[decimal.Decimal]", True)


@pytest.mark.parametrize("subtype", ["str", "float", "bool", "int | None", "list[int]", "Range[int]", "bytes"])
def test_range_of_an_unsupported_subtype_is_invalid(tmp_path: Path, subtype: str) -> None:
    source = "from rowx.postgres import Range\n"
    write(tmp_path, {"main.py": source + QUERY.format(t=f"Range[{subtype}]")})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_bare_range_and_a_range_with_two_arguments_are_invalid(tmp_path: Path) -> None:
    source = "from rowx.postgres import Range\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="Range")})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]
    write(tmp_path, {"main.py": source + QUERY.format(t="Range[int, int]")})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_money_and_interval_leaves_through_aliases_and_optional(tmp_path: Path) -> None:
    source = "import datetime\nfrom rowx.postgres import Interval, Money\ntype Span = Interval\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Span | None, Money, datetime.timedelta]")})
    assert leaves(resolve(tmp_path, "main.py")) == [
        ("rowx.postgres.Interval", True),
        ("rowx.postgres.Money", False),
        ("datetime.timedelta", False),
    ]


def test_money_and_interval_through_their_defining_modules_and_a_list(tmp_path: Path) -> None:
    source = "from rowx.postgres._money import Money\nfrom rowx.postgres._interval import Interval\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[list[Money | None], list[Interval]]")})
    first, second = row_type(resolve(tmp_path, "main.py")).elements
    assert first.item is not None
    assert (first.item.leaf, first.item.nullable) == ("rowx.postgres.Money", True)
    assert second.item is not None
    assert (second.item.leaf, second.item.nullable) == ("rowx.postgres.Interval", False)


@pytest.mark.parametrize("annotation", ["Interval | Money", "Interval | datetime.timedelta", "Money | int"])
def test_money_and_interval_cannot_be_unioned_with_another_leaf(tmp_path: Path, annotation: str) -> None:
    source = "import datetime\nfrom rowx.postgres import Interval, Money\n"
    write(tmp_path, {"main.py": source + QUERY.format(t=annotation)})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_money_and_interval_cannot_be_subscripted(tmp_path: Path) -> None:
    source = "from rowx.postgres import Interval\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="Interval[int]")})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_geometric_leaves_through_aliases_optional_and_a_list(tmp_path: Path) -> None:
    source = "from rowx.postgres import Box, Point, Path\ntype Corner = Point\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Corner | None, Box, list[Path | None]]")})
    first, second, third = row_type(resolve(tmp_path, "main.py")).elements
    assert (first.leaf, first.nullable) == ("rowx.postgres.Point", True)
    assert (second.leaf, second.nullable) == ("rowx.postgres.Box", False)
    assert third.item is not None
    assert (third.item.leaf, third.item.nullable) == ("rowx.postgres.Path", True)


def test_geometric_leaves_through_their_defining_module(tmp_path: Path) -> None:
    source = "from rowx.postgres._geometric import Circle, LineSegment, Line, Polygon\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Circle, LineSegment, Line, Polygon]")})
    assert leaves(resolve(tmp_path, "main.py")) == [
        ("rowx.postgres.Circle", False),
        ("rowx.postgres.LineSegment", False),
        ("rowx.postgres.Line", False),
        ("rowx.postgres.Polygon", False),
    ]


@pytest.mark.parametrize("annotation", ["Point | Circle", "Point | int", "Path | Polygon", "Point[int]"])
def test_geometric_classes_cannot_be_unioned_or_subscripted(tmp_path: Path, annotation: str) -> None:
    source = "from rowx.postgres import Circle, Path, Point, Polygon\n"
    write(tmp_path, {"main.py": source + QUERY.format(t=annotation)})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_hstore_leaf_through_aliases_and_a_list(tmp_path: Path) -> None:
    source = "from typing import Optional\ntype Tags = dict[str, str | None]\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Tags, Optional[dict[str, Optional[str]]], list[Tags]]")})
    first, second, third = row_type(resolve(tmp_path, "main.py")).elements
    assert (first.leaf, first.nullable) == ("dict[str, str | None]", False)
    assert (second.leaf, second.nullable) == ("dict[str, str | None]", True)
    assert third.item is not None
    assert third.item.leaf == "dict[str, str | None]"


def test_cube_leaf_through_its_defining_module(tmp_path: Path) -> None:
    source = "from rowx.postgres._cube import Cube\nimport rowx.postgres\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Cube, rowx.postgres.Cube | None]")})
    assert leaves(resolve(tmp_path, "main.py")) == [("rowx.postgres.Cube", False), ("rowx.postgres.Cube", True)]


@pytest.mark.parametrize(
    "annotation",
    ["dict", "dict[str, str]", "dict[str, int]", "dict[int, str | None]", "dict[str, str | None] | Cube", "Cube[int]"],
)
def test_only_the_hstore_dict_and_a_plain_cube_are_leaves(tmp_path: Path, annotation: str) -> None:
    source = "from rowx.postgres import Cube\n"
    write(tmp_path, {"main.py": source + QUERY.format(t=annotation)})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def fields(resolution: Resolution) -> list[tuple[str, bool, bool]]:
    return [(f.key, f.elem.nullable, f.required) for f in row_type(resolution).fields]


def rejected(resolution: Resolution) -> str:
    assert resolution.diagnostic is not None
    return resolution.diagnostic.rule


def named(tmp_path: Path, source: str, *, extra: dict[str, str] | None = None) -> Resolution:
    write(tmp_path, {"main.py": source + QUERY.format(t="T"), **(extra or {})})
    return resolve(tmp_path, "main.py")


def test_a_dataclass_is_a_keyword_row_by_name(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass, field\n@dataclass(frozen=True)\nclass T:\n    id: int\n    name: str | None\n    tags: list[str] = field(default_factory=list)\n    n: int = 0\n"
    row = row_type(named(tmp_path, source))
    assert (row.kind, row.form, row.matching, row.type_name) == ("named", "keyword", "by_name", "T")
    assert [(f.key, f.names, f.elem.nullable, f.required) for f in row.fields] == [
        ("id", ("id",), False, True),
        ("name", ("name",), True, True),
        ("tags", ("tags",), False, False),
        ("n", ("n",), False, False),
    ]
    assert [(f.span.line, f.span.col) for f in row.fields] == [(4, 5), (5, 5), (6, 5), (7, 5)]
    assert all(f.path.name == "main.py" for f in row.fields)


def test_a_class_namedtuple_is_positional_and_defaults_are_optional(tmp_path: Path) -> None:
    source = "from typing import NamedTuple\nclass T(NamedTuple):\n    id: int\n    name: str = ''\n"
    row = row_type(named(tmp_path, source))
    assert (row.form, row.matching) == ("positional", "positional")
    assert fields(named(tmp_path, source)) == [("id", False, True), ("name", False, False)]


def test_the_functional_forms(tmp_path: Path) -> None:
    nt = "from typing import NamedTuple\nT = NamedTuple('T', [('id', int), ('name', str | None)])\n"
    td = "from typing import TypedDict\nT = TypedDict('T', {'id': int, 'n': str | None}, total=False)\n"
    assert fields(named(tmp_path, nt)) == [("id", False, True), ("name", True, True)]
    resolution = named(tmp_path, td)
    assert (row_type(resolution).form, row_type(resolution).matching) == ("dict", "by_name")
    assert fields(resolution) == [("id", False, False), ("n", True, False)]


def test_a_typing_extensions_typeddict_and_requiredness(tmp_path: Path) -> None:
    source = (
        "import typing_extensions as te\nfrom typing import ReadOnly, Required\n"
        "class V(te.TypedDict, total=False):\n    a: int\n    b: Required[str]\n    c: ReadOnly[int]\n"
        "class U(te.TypedDict):\n    a: te.NotRequired[int]\n    b: str\n"
    )
    assert fields(named(tmp_path, source + "T = V\n")) == [("a", False, False), ("b", False, True), ("c", False, False)]
    assert fields(named(tmp_path, source + "T = U\n")) == [("a", False, False), ("b", False, True)]


def test_total_belongs_to_the_declaring_class(tmp_path: Path) -> None:
    source = (
        "from typing import TypedDict\nclass A(TypedDict):\n    a: int\n    b: str\n"
        "class B(A, total=False):\n    c: int\nclass T(B):\n    b: str | None\n    d: int\n"
    )
    assert fields(named(tmp_path, source)) == [
        ("a", False, True),
        ("b", True, True),
        ("c", False, False),
        ("d", False, True),
    ]


@pytest.mark.parametrize(
    "annotations",
    [
        "n: 'NotRequired[str]'\n    m: NotRequired['str']\n    k: 'Annotated[NotRequired[int], 1]'\n    j: Annotated['ReadOnly[NotRequired[int]]', 1]",
        "n: NotRequired[Annotated[str, 1]]\n    m: ReadOnly[NotRequired[str]]\n    k: NotRequired[ReadOnly[int]]\n    j: NotRequired[int]",
    ],
)
def test_a_notrequired_is_found_in_quoted_and_wrapped_annotations(tmp_path: Path, annotations: str) -> None:
    source = (
        "from typing import Annotated, NotRequired, ReadOnly, TypedDict\nclass T(TypedDict):\n    id: int\n"
        f"    {annotations}\n"
    )
    assert [required for *_, required in fields(named(tmp_path, source))] == [True, False, False, False, False]


def test_a_quoted_required_overrides_total_false(tmp_path: Path) -> None:
    source = (
        "from typing import Required, TypedDict\nclass T(TypedDict, total=False):\n    a: int\n    b: 'Required[int]'\n"
    )
    assert fields(named(tmp_path, source)) == [("a", False, False), ("b", False, True)]


def test_inheritance_and_a_redeclared_field_keep_the_base_position(tmp_path: Path) -> None:
    source = (
        "from dataclasses import dataclass\n@dataclass\nclass A:\n    a: int\n    b: str\n"
        "@dataclass\nclass B(A):\n    c: int\n@dataclass\nclass T(B):\n    b: str | None\n    d: bool = False\n"
    )
    assert fields(named(tmp_path, source)) == [
        ("a", False, True),
        ("b", True, True),
        ("c", False, True),
        ("d", False, False),
    ]


def test_an_undecorated_subclass_of_a_dataclass_adds_no_fields(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\n@dataclass\nclass A:\n    a: int\nclass T(A):\n    extra: str\n"
    assert fields(named(tmp_path, source)) == [("a", False, True)]


def test_a_mixin_contributes_no_fields_and_a_later_redeclaration_can_drop_one(tmp_path: Path) -> None:
    source = (
        "from dataclasses import dataclass\nfrom typing import ClassVar\nclass Mixin:\n    x: int = 0\n"
        "@dataclass\nclass A:\n    a: int\n    b: int\n@dataclass\nclass T(Mixin, A):\n    b: ClassVar[int] = 0\n"
    )
    assert fields(named(tmp_path, source)) == [("a", False, True)]


def test_namedtuple_fields_come_only_from_the_class_that_subclasses_namedtuple(tmp_path: Path) -> None:
    source = "from typing import NamedTuple\nclass Base(NamedTuple):\n    id: int\nclass T(Base):\n    extra: undefined_name\n"
    assert fields(named(tmp_path, source)) == [("id", False, True)]


def test_not_fields_are_skipped(tmp_path: Path) -> None:
    source = (
        "from dataclasses import KW_ONLY, dataclass, field\nfrom typing import ClassVar\n@dataclass\nclass T:\n"
        "    a: int\n    c: ClassVar[int] = 0\n    d: int = field(init=False, default=1)\n    _: KW_ONLY\n"
        "    e: 'ClassVar[str]' = ''\n    f: str = field(kw_only=True, default='')\n"
    )
    assert fields(named(tmp_path, source)) == [("a", False, True), ("f", False, False)]


def test_annotations_under_future_annotations_and_quoted(tmp_path: Path) -> None:
    source = (
        "from __future__ import annotations\nimport datetime as dt\nfrom dataclasses import dataclass\n"
        "@dataclass\nclass T:\n    a: dt.date | None\n    b: list['int']\n    c: 'str'\n"
    )
    assert fields(named(tmp_path, source)) == [("a", True, True), ("b", False, True), ("c", False, True)]


def test_a_type_checking_only_name_in_a_field_is_rejected_with_help(tmp_path: Path) -> None:
    source = (
        "from typing import TYPE_CHECKING\nfrom dataclasses import dataclass\nif TYPE_CHECKING:\n    import datetime\n"
        "@dataclass\nclass T:\n    a: 'datetime.date'\n"
    )
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help == "import `datetime` at runtime"
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.notes[-1].startswith("field defined at ")
    assert resolution.diagnostic.notes[-1].endswith("main.py:7:5")


def test_a_type_checking_only_name_outside_the_selected_fields_is_ignored(tmp_path: Path) -> None:
    source = (
        "from typing import TYPE_CHECKING, ClassVar, NamedTuple\nfrom dataclasses import dataclass, field\n"
        "if TYPE_CHECKING:\n    import datetime\n"
        "@dataclass\nclass D:\n    a: int\n    b: ClassVar[datetime.date]\n    c: datetime.date = field(init=False)\n"
        "class Base(NamedTuple):\n    a: int\nclass T(Base):\n    b: datetime.date\n"
    )
    assert fields(named(tmp_path, source)) == [("a", False, True)]
    assert fields(named(tmp_path, source.replace("class T(Base):\n    b: datetime.date\n", "T = D\n"))) == [
        ("a", False, True)
    ]


def test_a_type_checking_only_alias_stays_valid_as_the_row_type(tmp_path: Path) -> None:
    source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    type A = int\nT = A\n"
    assert leaves(named(tmp_path, source)) == [("int", False)]


def test_the_dataclass_decorator_under_an_alias_and_final(tmp_path: Path) -> None:
    source = (
        "import dataclasses as dc\nfrom typing import final\nfrom dataclasses import dataclass as dcls\n"
        "@final\n@dc.dataclass(slots=True, kw_only=True)\nclass Base:\n    a: int\n"
        "@dcls\nclass T(Base):\n    b: int\n"
    )
    assert fields(named(tmp_path, source)) == [("a", False, True), ("b", False, True)]


def test_a_non_literal_decorator_argument_is_unresolved(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\nFLAG = True\n@dataclass(init=FLAG)\nclass T:\n    a: int\n"
    assert rejected(named(tmp_path, source)) == "unresolved-row-type"
    source = "from dataclasses import dataclass\n@dataclass(kw_only=not False)\nclass T:\n    a: int\n"
    assert rejected(named(tmp_path, source)) == "unresolved-row-type"


@pytest.mark.parametrize(
    ("source", "help_"),
    [
        (
            "def deco(c):\n    return c\n@deco\n@dataclass\nclass T:\n    a: int\n",
            "remove the decorator, or use `@dataclass`",
        ),
        (
            "import functools\n@functools.total_ordering\n@dataclass\nclass T:\n    a: int\n",
            "remove the decorator, or use `@dataclass`",
        ),
        (
            "class Meta(type): ...\n@dataclass\nclass T(metaclass=Meta):\n    a: int\n",
            "remove the metaclass or class keyword",
        ),
        (
            "class B:\n    def __init_subclass__(cls, flag=False): ...\n@dataclass\nclass T(B, flag=True):\n    a: int\n",
            "remove the metaclass or class keyword",
        ),
        (
            "@dataclass\nclass T:\n    a: int\n    def __init__(self, a: int) -> None:\n        self.a = a\n",
            "remove `__init__` and use `__post_init__`",
        ),
    ],
)
def test_checker_only_rejections_carry_their_help(tmp_path: Path, source: str, help_: str) -> None:
    resolution = named(tmp_path, "from dataclasses import dataclass\n" + source)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help == help_


def test_a_typeddict_accepts_its_known_keywords_and_rejects_others(tmp_path: Path) -> None:
    source = "from typing_extensions import TypedDict\nclass T(TypedDict, total=False, closed=True):\n    a: int\n"
    assert fields(named(tmp_path, source)) == [("a", False, False)]
    resolution = named(tmp_path, "from typing import TypedDict\nclass T(TypedDict, other=1):\n    a: int\n")
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help == "remove the metaclass or class keyword"
    assert (
        rejected(
            named(tmp_path, "from typing import TypedDict\nT_ = True\nclass T(TypedDict, total=T_):\n    a: int\n")
        )
        == "unresolved-row-type"
    )


@pytest.mark.parametrize(
    "source",
    [
        "from typing import Protocol\nclass T(Protocol):\n    a: int\n",
        "import collections\nT = collections.namedtuple('T', ['a'])\n",
        "from dataclasses import dataclass\n@dataclass(init=False)\nclass T:\n    a: int\n",
        "from dataclasses import InitVar, dataclass\n@dataclass\nclass T:\n    a: int\n    b: InitVar[int]\n",
        "from dataclasses import dataclass\n@dataclass\nclass T: ...\n",
        "from typing import NamedTuple\nclass T(NamedTuple): ...\n",
        "from typing import TypedDict\nclass T(TypedDict): ...\n",
        "class T:\n    a: int\n",
        "from dataclasses import dataclass\n@dataclass\nclass U:\n    a: int\nT = U | None\n",
        "from dataclasses import dataclass\n@dataclass\nclass U:\n    a: int\nT = tuple[U, int]\n",
        "from dataclasses import dataclass\n@dataclass\nclass U:\n    a: int\nT = list[U]\n",
        "from dataclasses import dataclass\n@dataclass\nclass U:\n    a: int\n@dataclass\nclass T:\n    u: U\n",
        "from typing import NamedTuple\nclass T(NamedTuple):\n    a: tuple[int, int]\n",
        "from typing import TypedDict\nclass T(TypedDict):\n    a: int | str\n",
        "from dataclasses import dataclass\n@dataclass\nclass T:\n    a: 'not a type'\n",
        "from dataclasses import dataclass\n@dataclass\nclass T(Exception):\n    a: int\n",
        "from dataclasses import dataclass\nimport abc\n@dataclass\nclass T(abc.ABC):\n    a: int\n",
        "import enum\nclass T(enum.Enum):\n    a = 1\n",
        "from typing import NamedTuple, TypedDict\nclass A(NamedTuple):\n    a: int\nclass T(A, TypedDict):\n    b: int\n",
    ],
)
def test_rejections_on_both_sides(tmp_path: Path, source: str) -> None:
    assert rejected(named(tmp_path, source)) == "invalid-row-type"


def test_a_nullable_named_row_says_to_use_optional(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\n@dataclass\nclass U:\n    a: int\nT = U | None\n"
    resolution = named(tmp_path, source)
    assert resolution.help == "use .optional()"


@pytest.mark.parametrize(
    "source",
    [
        "from rowx.postgres import Point\nclass T(Point): ...\n",
        "from rowx.postgres import Point\nfrom dataclasses import dataclass\n@dataclass(frozen=True)\nclass T(Point):\n    z: int\n",
        "from rowx.postgres import Range\nclass T(Range[int]): ...\n",
        "import rowx.postgres as pg\nclass T(pg.Interval): ...\n",
        "from rowx.postgres import Money\nclass T(Money): ...\n",
        "from rowx.postgres._cube import Cube\nclass T(Cube): ...\n",
        "from rowx.postgres import Circle\nclass M:\n    pass\nclass T(M, Circle): ...\n",
    ],
)
def test_a_subclass_of_a_value_class_is_rejected(tmp_path: Path, source: str) -> None:
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert "is not a row type" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


def test_value_classes_stay_leaves(tmp_path: Path) -> None:
    source = "from rowx.postgres import Money, Range\nT = tuple[Money, Range[int]]\n"
    assert [leaf for leaf, _ in leaves(named(tmp_path, source))] == ["rowx.postgres.Money", "rowx.postgres.Range[int]"]


@pytest.mark.parametrize(
    "source",
    [
        "from dataclasses import dataclass\n@dataclass\nclass T[X]:\n    a: int\n",
        "from dataclasses import dataclass\nfrom typing import Generic, TypeVar\nX = TypeVar('X')\n@dataclass\nclass T(Generic[X]):\n    a: int\n",
        "from dataclasses import dataclass\n@dataclass\nclass G[X]:\n    a: int\nT = G[int]\n",
        "from dataclasses import dataclass\nfrom typing import Generic, TypeVar\nX = TypeVar('X')\n@dataclass\nclass G(Generic[X]):\n    a: X\n@dataclass\nclass T(G[int]):\n    b: int\n",
        "from dataclasses import dataclass\nfrom typing import Generic\n@dataclass\nclass T(Generic):\n    a: int\n",
    ],
)
def test_generic_row_types_are_rejected_until_they_are_supported(tmp_path: Path, source: str) -> None:
    assert rejected(named(tmp_path, source)) == "invalid-row-type"


@pytest.mark.parametrize(
    "source",
    [
        "import pydantic\nclass T(pydantic.BaseModel):\n    a: int\n",
        "from pydantic import BaseModel\nclass T(BaseModel):\n    a: int\n",
        "import pydantic.dataclasses\n@pydantic.dataclasses.dataclass\nclass T:\n    a: int\n",
    ],
)
def test_pydantic_row_types_are_not_checked_yet(tmp_path: Path, source: str) -> None:
    assert rejected(named(tmp_path, source)) == "invalid-row-type"


def test_a_stdlib_class_is_invalid_and_a_third_party_class_is_unresolved(tmp_path: Path) -> None:
    assert rejected(named(tmp_path, "from collections import OrderedDict\nT = OrderedDict\n")) == "invalid-row-type"
    resolution = named(tmp_path, "from thirdparty import Model\nT = Model\n")
    assert rejected(resolution) == "unresolved-row-type"
    assert resolution.help == "define the row type inside the project"
    source = "from dataclasses import dataclass\nfrom thirdparty import Base\n@dataclass\nclass T(Base):\n    a: int\n"
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "unresolved-row-type"
    assert resolution.help == "define the row type inside the project"
    source = "from dataclasses import dataclass\n@dataclass\nclass T:\n    a: numpy.int64\nimport numpy\n"
    assert rejected(named(tmp_path, source)) == "unresolved-row-type"


def test_a_class_from_another_module_through_a_reexport_and_an_import_alias(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "from .models import User\n",
            "pkg/models.py": "from dataclasses import dataclass\n@dataclass\nclass User:\n    id: int\n    name: str | None = None\n",
            "a.py": "from pkg import User\n" + QUERY.format(t="User"),
            "b.py": "import pkg.models as m\n" + QUERY.format(t="m.User"),
            "c.py": "from pkg import models\n" + QUERY.format(t="models.User"),
            "d.py": "import pkg\n" + QUERY.format(t="pkg.User"),
        },
    )
    for name in ("a.py", "b.py", "c.py", "d.py"):
        resolution = resolve(tmp_path, name)
        assert fields(resolution) == [("id", False, True), ("name", True, False)]
        assert row_type(resolution).fields[0].path.name == "models.py"


def test_inheritance_across_modules_uses_each_modules_names(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "base.py": "import datetime as dt\nfrom dataclasses import dataclass\n@dataclass\nclass Base:\n    when: dt.date\n    n: int\n",
            "main.py": "from dataclasses import dataclass\nfrom base import Base\n@dataclass\nclass T(Base):\n    n: str | None\n"
            + QUERY.format(t="T"),
        },
    )
    resolution = resolve(tmp_path, "main.py")
    assert [(f.key, f.elem.leaf, f.path.name) for f in row_type(resolution).fields] == [
        ("when", "datetime.date", "base.py"),
        ("n", "str", "main.py"),
    ]


def test_a_class_alias_adds_the_alias_note(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\n@dataclass\nclass U:\n    a: int\ntype A = U\nT = A\n"
    row = row_type(named(tmp_path, source))
    assert row.alias is not None
    assert row.type_name == "U"


def test_a_name_bound_twice_and_a_cycle_are_unresolved(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\n@dataclass\nclass T:\n    a: int\n@dataclass\nclass T:\n    b: int\n"
    assert rejected(named(tmp_path, source)) == "unresolved-row-type"
    source = "from dataclasses import dataclass\n@dataclass\nclass A(B):\n    a: int\n@dataclass\nclass B(A):\n    b: int\nT = A\n"
    assert rejected(named(tmp_path, source)) == "unresolved-row-type"


def test_a_field_error_names_the_field_and_where_it_is_defined(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\n@dataclass\nclass T:\n    a: int\n    b: int | str\n"
    resolution = named(tmp_path, source)
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.message.endswith('(field "b" of `T`)')
    assert resolution.diagnostic.notes[-1].endswith("main.py:5:5")


def test_user_code_is_never_imported(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "boom.py": "raise RuntimeError('imported')\n",
            "models.py": (
                "import boom\nfrom dataclasses import dataclass\nfrom typing import NamedTuple, TypedDict\n"
                "@dataclass\nclass D:\n    a: int\nclass N(NamedTuple):\n    a: int\nclass TD(TypedDict):\n    a: int\n"
                "F = NamedTuple('F', [('a', int)])\n"
            ),
            "main.py": "from models import D, N, TD, F\n" + QUERY.format(t="tuple[int]"),
        },
    )
    before = set(sys.modules)
    for target in ("D", "N", "TD", "F"):
        (tmp_path / "use.py").write_text(f"from models import {target}\n" + QUERY.format(t=target))
        assert row_type(resolve(tmp_path, "use.py")).kind == "named"
    assert set(sys.modules) == before
    assert "boom" not in sys.modules
