import json
import sys
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from textwrap import dedent
from typing import TYPE_CHECKING, Final

import pytest
from rowx_postgres._backend import load

from rowx.check.discover import discover_file
from rowx.check.resolve import ModuleGraph, Resolution, RowType, resolve_row_type

if TYPE_CHECKING:
    from pathlib import Path

PG_RULES: Final = load().checker()


def write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(source))


def resolve(root: Path, name: str, *, roots: tuple[Path, ...] | None = None) -> Resolution:
    result = discover_file(root / name)
    assert not result.diagnostics
    [call] = result.calls
    return resolve_row_type(ModuleGraph(), call, roots or (root,), backend=PG_RULES)


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
    results = [resolve_row_type(graph, call, (tmp_path,), backend=PG_RULES) for call in calls]
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
    source = "import datetime\nfrom rowx_postgres import Range\ntype Dates = Range[datetime.date]\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Dates | None, Range[datetime.datetime], Range[int]]")})
    assert leaves(resolve(tmp_path, "main.py")) == [
        ("rowx_postgres.Range[datetime.date]", True),
        ("rowx_postgres.Range[datetime.datetime]", False),
        ("rowx_postgres.Range[int]", False),
    ]


def test_range_through_its_defining_module_and_a_list(tmp_path: Path) -> None:
    source = "from rowx_postgres._range import Range\nimport decimal\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="list[Range[decimal.Decimal] | None]")})
    [elem] = row_type(resolve(tmp_path, "main.py")).elements
    assert elem.leaf == "list"
    assert elem.item is not None
    assert (elem.item.leaf, elem.item.nullable) == ("rowx_postgres.Range[decimal.Decimal]", True)


@pytest.mark.parametrize("subtype", ["str", "float", "bool", "int | None", "list[int]", "Range[int]", "bytes"])
def test_range_of_an_unsupported_subtype_is_invalid(tmp_path: Path, subtype: str) -> None:
    source = "from rowx_postgres import Range\n"
    write(tmp_path, {"main.py": source + QUERY.format(t=f"Range[{subtype}]")})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_bare_range_and_a_range_with_two_arguments_are_invalid(tmp_path: Path) -> None:
    source = "from rowx_postgres import Range\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="Range")})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]
    write(tmp_path, {"main.py": source + QUERY.format(t="Range[int, int]")})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_money_and_interval_leaves_through_aliases_and_optional(tmp_path: Path) -> None:
    source = "import datetime\nfrom rowx_postgres import Interval, Money\ntype Span = Interval\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Span | None, Money, datetime.timedelta]")})
    assert leaves(resolve(tmp_path, "main.py")) == [
        ("rowx_postgres.Interval", True),
        ("rowx_postgres.Money", False),
        ("datetime.timedelta", False),
    ]


def test_money_and_interval_through_their_defining_modules_and_a_list(tmp_path: Path) -> None:
    source = "from rowx_postgres._money import Money\nfrom rowx_postgres._interval import Interval\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[list[Money | None], list[Interval]]")})
    first, second = row_type(resolve(tmp_path, "main.py")).elements
    assert first.item is not None
    assert (first.item.leaf, first.item.nullable) == ("rowx_postgres.Money", True)
    assert second.item is not None
    assert (second.item.leaf, second.item.nullable) == ("rowx_postgres.Interval", False)


@pytest.mark.parametrize("annotation", ["Interval | Money", "Interval | datetime.timedelta", "Money | int"])
def test_money_and_interval_cannot_be_unioned_with_another_leaf(tmp_path: Path, annotation: str) -> None:
    source = "import datetime\nfrom rowx_postgres import Interval, Money\n"
    write(tmp_path, {"main.py": source + QUERY.format(t=annotation)})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_money_and_interval_cannot_be_subscripted(tmp_path: Path) -> None:
    source = "from rowx_postgres import Interval\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="Interval[int]")})
    assert resolve(tmp_path, "main.py").diagnostic.rule == "invalid-row-type"  # ty: ignore[unresolved-attribute]


def test_geometric_leaves_through_aliases_optional_and_a_list(tmp_path: Path) -> None:
    source = "from rowx_postgres import Box, Point, Path\ntype Corner = Point\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Corner | None, Box, list[Path | None]]")})
    first, second, third = row_type(resolve(tmp_path, "main.py")).elements
    assert (first.leaf, first.nullable) == ("rowx_postgres.Point", True)
    assert (second.leaf, second.nullable) == ("rowx_postgres.Box", False)
    assert third.item is not None
    assert (third.item.leaf, third.item.nullable) == ("rowx_postgres.Path", True)


def test_geometric_leaves_through_their_defining_module(tmp_path: Path) -> None:
    source = "from rowx_postgres._geometric import Circle, LineSegment, Line, Polygon\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Circle, LineSegment, Line, Polygon]")})
    assert leaves(resolve(tmp_path, "main.py")) == [
        ("rowx_postgres.Circle", False),
        ("rowx_postgres.LineSegment", False),
        ("rowx_postgres.Line", False),
        ("rowx_postgres.Polygon", False),
    ]


@pytest.mark.parametrize("annotation", ["Point | Circle", "Point | int", "Path | Polygon", "Point[int]"])
def test_geometric_classes_cannot_be_unioned_or_subscripted(tmp_path: Path, annotation: str) -> None:
    source = "from rowx_postgres import Circle, Path, Point, Polygon\n"
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
    source = "from rowx_postgres._cube import Cube\nimport rowx_postgres\n"
    write(tmp_path, {"main.py": source + QUERY.format(t="tuple[Cube, rowx_postgres.Cube | None]")})
    assert leaves(resolve(tmp_path, "main.py")) == [("rowx_postgres.Cube", False), ("rowx_postgres.Cube", True)]


@pytest.mark.parametrize(
    "annotation",
    ["dict", "dict[str, str]", "dict[str, int]", "dict[int, str | None]", "dict[str, str | None] | Cube", "Cube[int]"],
)
def test_only_the_hstore_dict_and_a_plain_cube_are_leaves(tmp_path: Path, annotation: str) -> None:
    source = "from rowx_postgres import Cube\n"
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


def test_a_type_checking_only_alias_is_rejected_as_the_row_type(tmp_path: Path) -> None:
    source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    type A = int\nT = A\n"
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help == "import `A` at runtime"


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
        "import enum\nclass T(enum.Enum):\n    a = 1\n",
        "from typing import NamedTuple, TypedDict\nclass A(NamedTuple):\n    a: int\nclass T(A, TypedDict):\n    b: int\n",
    ],
)
def test_rejections_on_both_sides(tmp_path: Path, source: str) -> None:
    assert rejected(named(tmp_path, source)) == "invalid-row-type"


@pytest.mark.parametrize(
    "source",
    [
        "from dataclasses import dataclass\n@dataclass\nclass T(Exception):\n    a: int\n",
        "from dataclasses import dataclass\nimport abc\n@dataclass\nclass T(abc.ABC):\n    a: int\n",
        "from dataclasses import dataclass\nimport abc\nclass M(abc.ABC): ...\n@dataclass\nclass T(M):\n    a: int\n",
    ],
)
def test_a_base_outside_the_project_is_a_checker_only_rejection_with_help(tmp_path: Path, source: str) -> None:
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help == "inherit only from project classes and the row-type base"


def test_a_nullable_named_row_says_to_use_optional(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\n@dataclass\nclass U:\n    a: int\nT = U | None\n"
    resolution = named(tmp_path, source)
    assert resolution.help == "use .optional()"


@pytest.mark.parametrize(
    "source",
    [
        "from rowx_postgres import Point\nclass T(Point): ...\n",
        "from rowx_postgres import Point\nfrom dataclasses import dataclass\n@dataclass(frozen=True)\nclass T(Point):\n    z: int\n",
        "from rowx_postgres import Range\nclass T(Range[int]): ...\n",
        "import rowx_postgres as pg\nclass T(pg.Interval): ...\n",
        "from rowx_postgres import Money\nclass T(Money): ...\n",
        "from rowx_postgres._cube import Cube\nclass T(Cube): ...\n",
        "from rowx_postgres import Circle\nclass M:\n    pass\nclass T(M, Circle): ...\n",
    ],
)
def test_a_subclass_of_a_value_class_is_rejected(tmp_path: Path, source: str) -> None:
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert "is not a row type" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


def test_value_classes_stay_leaves(tmp_path: Path) -> None:
    source = "from rowx_postgres import Money, Range\nT = tuple[Money, Range[int]]\n"
    assert [leaf for leaf, _ in leaves(named(tmp_path, source))] == ["rowx_postgres.Money", "rowx_postgres.Range[int]"]


PYDANTIC: Final[str] = (
    "from typing import Annotated, ClassVar\nfrom dataclasses import InitVar\nimport pydantic as pd\n"
    "from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, RootModel, model_validator\n"
    "from pydantic.dataclasses import dataclass as pdc\n"
)


def pyd(tmp_path: Path, source: str, *, extra: dict[str, str] | None = None) -> Resolution:
    return named(tmp_path, PYDANTIC + source, extra=extra)


def names(resolution: Resolution) -> list[tuple[str, tuple[str, ...] | None]]:
    return [(f.key, f.names) for f in row_type(resolution).fields]


def reasons(resolution: Resolution) -> list[str | None]:
    return [f.reason for f in row_type(resolution).fields]


@pytest.mark.parametrize(
    "head",
    [
        "from pydantic import BaseModel\nclass T(BaseModel):",
        "import pydantic as pd\nclass T(pd.BaseModel):",
        "import pydantic\nclass T(pydantic.BaseModel):",
        "import pydantic.main as m\nclass T(m.BaseModel):",
        "from pydantic.main import BaseModel as B\nclass T(B):",
    ],
)
def test_a_pydantic_model_is_a_validator_row_by_name(tmp_path: Path, head: str) -> None:
    row = row_type(named(tmp_path, f"{head}\n    id: int\n    name: str | None\n    tags: list[str] = []\n"))
    assert (row.kind, row.form, row.matching, row.type_name) == ("named", "validator", "by_name", "T")
    assert [(f.key, f.names, f.elem.nullable, f.required) for f in row.fields] == [
        ("id", ("id",), False, True),
        ("name", ("name",), True, True),
        ("tags", ("tags",), False, False),
    ]
    assert [f.reason for f in row.fields] == [None, None, None]


def test_a_pydantic_dataclass_is_a_validator_row_by_name(tmp_path: Path) -> None:
    for decorator in (
        "@pdc",
        "@pdc()",
        "@pd.dataclasses.dataclass(frozen=True)",
        "@pd.dataclasses.dataclass(kw_only=True)",
    ):
        row = row_type(pyd(tmp_path, f"{decorator}\nclass T:\n    id: int\n    n: int = 0\n"))
        assert (row.form, row.matching) == ("validator", "by_name")
        assert [(f.key, f.required) for f in row.fields] == [("id", True), ("n", False)]


def test_an_inherited_model_collects_the_base_fields_first_and_a_redeclared_one_keeps_its_place(tmp_path: Path) -> None:
    source = "class A(BaseModel):\n    a: int\n    b: int\nclass B(A):\n    c: int\n    b: str | None = None\nclass T(B):\n    d: int\n"
    assert [(f.key, f.elem.nullable, f.required) for f in row_type(pyd(tmp_path, source)).fields] == [
        ("a", False, True),
        ("b", True, False),
        ("c", False, True),
        ("d", False, True),
    ]


def test_a_redeclared_field_without_a_default_is_required_again(tmp_path: Path) -> None:
    source = "class A(BaseModel):\n    x: int = 1\nclass T(A):\n    x: int\n"
    assert fields(pyd(tmp_path, source)) == [("x", False, True)]


def test_a_non_model_base_contributes_its_annotations_and_attributes(tmp_path: Path) -> None:
    source = "class Mixin:\n    a: int\n    b: int = 1\nclass T(Mixin, BaseModel):\n    c: str\n    b: int\n"
    assert fields(pyd(tmp_path, source)) == [("a", False, True), ("b", False, False), ("c", False, True)]


def test_a_base_redeclared_through_two_models_takes_the_first_base_copy(tmp_path: Path) -> None:
    source = (
        "class A(BaseModel):\n    x: int = Field(alias='a')\nclass B(BaseModel):\n    x: int = Field(alias='b')\n"
        "class T(A, B): ...\n"
    )
    assert names(pyd(tmp_path, source)) == [("x", ("a",))]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("id: int", ("id",)),
        ("id: int = Field(alias='user_id')", ("user_id",)),
        ("id: int = Field(validation_alias='user_id')", ("user_id",)),
        ("id: int = Field(validation_alias='v', alias='a')", ("v",)),
        ("id: int = Field(validation_alias=None, alias='a')", ("a",)),
        ("id: int = Field(serialization_alias='out')", ("id",)),
        ("id: int = Field(alias='a', serialization_alias='out')", ("a",)),
        ("id: Annotated[int, Field(alias='a')]", ("a",)),
        ("id: Annotated[Annotated[int, Field(alias='a')], 'note']", ("a",)),
        ("id: Annotated[int, Field(alias='a')] = Field(alias='b')", ("b",)),
        ("id: 'Annotated[int, Field(alias=\"a\")]'", ("a",)),
    ],
)
def test_a_fields_alias_forms(tmp_path: Path, field: str, expected: tuple[str, ...]) -> None:
    resolution = pyd(tmp_path, f"class T(BaseModel):\n    {field}\n")
    assert names(resolution) == [("id", expected)]


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("id: int = Field(validation_alias=AliasChoices('a', 'b'))", "field `id` uses AliasChoices"),
        ("id: int = Field(alias=AliasChoices('a', 'b'))", "field `id` uses AliasChoices"),
        ("id: int = Field(validation_alias=AliasPath('a', 0))", "field `id` uses AliasPath"),
        ("id: int = Field(validation_alias=pd.AliasChoices('a'), alias='x')", "field `id` uses AliasChoices"),
        ("id: int = Field(alias=NAME)", "field `id` has an alias that isn't a literal string"),
        ("id: int = Field(validation_alias=f'{1}')", "field `id` has an alias that isn't a literal string"),
        ("id: Annotated[int, Field(validation_alias=AliasChoices('a'))]", "field `id` uses AliasChoices"),
    ],
)
def test_an_unreadable_alias_makes_only_that_fields_names_unknown(tmp_path: Path, field: str, reason: str) -> None:
    resolution = pyd(tmp_path, f"NAME = 'x'\nclass T(BaseModel):\n    {field}\n    other: int = Field(alias='o')\n")
    assert names(resolution) == [("id", None), ("other", ("o",))]
    assert reasons(resolution) == [reason, None]


@pytest.mark.parametrize(
    ("value", "required"),
    [
        ("", True),
        (" = 1", False),
        (" = None", False),
        (" = Field()", True),
        (" = Field(...)", True),
        (" = ...", True),
        (" = Field(default=...)", True),
        (" = Field(default=1)", False),
        (" = Field(1)", False),
        (" = Field(default=None)", False),
        (" = Field(default_factory=list)", False),
        (" = Field(alias='a')", True),
        (" = pd.Field(default_factory=list, alias='a')", False),
    ],
)
def test_a_pydantic_field_is_optional_by_a_default(tmp_path: Path, value: str, *, required: bool) -> None:
    assert fields(pyd(tmp_path, f"class T(BaseModel):\n    x: int{value}\n")) == [("x", False, required)]


@pytest.mark.parametrize(
    ("annotation", "required"),
    [
        ("Annotated[int, Field(default=1)]", False),
        ("Annotated[int, Field(default_factory=int)]", False),
        ("Annotated[int, Field(...)]", True),
        ("Annotated[int, Field()]", True),
        ("Annotated[int | None, Field(default=None)]", False),
    ],
)
def test_a_default_in_annotated_metadata_makes_a_field_optional(
    tmp_path: Path, annotation: str, *, required: bool
) -> None:
    assert [f.required for f in row_type(pyd(tmp_path, f"class T(BaseModel):\n    x: {annotation}\n")).fields] == [
        required
    ]


def test_a_value_field_overrides_annotated_metadata(tmp_path: Path) -> None:
    source = "class T(BaseModel):\n    x: Annotated[int, Field(default=1)] = Field(...)\n"
    assert fields(pyd(tmp_path, source)) == [("x", False, False)]


def test_fields_that_are_not_fields(tmp_path: Path) -> None:
    source = (
        "class T(BaseModel):\n    model_config = ConfigDict(extra='forbid')\n    id: int\n    _secret: int = 0\n"
        "    __hidden: int\n    k: ClassVar[int] = 0\n    m: ClassVar[dict[str, int]]\n"
        "    @pd.computed_field\n    @property\n    def double(self) -> int:\n        return self.id * 2\n"
    )
    assert fields(pyd(tmp_path, source)) == [("id", False, True)]


def test_a_model_config_with_an_annotation_is_not_a_field(tmp_path: Path) -> None:
    source = "class T(BaseModel):\n    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')\n"
    assert names(pyd(tmp_path, source)) == [("id", ("u", "id"))]


def test_a_model_may_define_init(tmp_path: Path) -> None:
    source = "class T(BaseModel):\n    id: int\n    def __init__(self, **data):\n        super().__init__(**data)\n"
    assert fields(pyd(tmp_path, source)) == [("id", False, True)]
    source = "@pdc\nclass T:\n    id: int\n    def __init__(self, id):\n        self.id = id\n"
    assert fields(pyd(tmp_path, source)) == [("id", False, True)]


# the config a field with an `alias='u'` accepts: `(populate_by_name, validate_by_name, validate_by_alias)`
@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ("", ("u",)),
        ("populate_by_name=True", ("u", "id")),
        ("populate_by_name=False", ("u",)),
        ("validate_by_name=True", ("u", "id")),
        ("validate_by_name=False", ("u",)),
        ("validate_by_alias=False", ("id",)),
        ("validate_by_alias=True", ("u",)),
        ("validate_by_alias=False, validate_by_name=True", ("id",)),
        ("validate_by_alias=True, validate_by_name=True", ("u", "id")),
        # an explicit `validate_by_name` wins outright
        ("populate_by_name=True, validate_by_name=False", ("u",)),
        ("populate_by_name=False, validate_by_name=True", ("u", "id")),
        # otherwise a set `populate_by_name` forces `validate_by_alias=True`
        ("populate_by_name=True, validate_by_alias=False", ("u", "id")),
        ("populate_by_name=False, validate_by_alias=False", ("u",)),
        # an explicit `None` is unset
        ("populate_by_name=None, validate_by_alias=False", ("id",)),
        ("validate_by_name=None, populate_by_name=True", ("u", "id")),
        # keys that don't touch names are ignored, even when they aren't literal
        ("extra='forbid', strict=True, json_schema_extra=SOMETHING", ("u",)),
    ],
)
def test_the_name_config_keys_follow_pydantics_precedence(
    tmp_path: Path, config: str, expected: tuple[str, ...]
) -> None:
    source = f"SOMETHING = {{}}\nclass T(BaseModel):\n    model_config = ConfigDict({config})\n    id: int = Field(alias='u')\n"
    assert names(pyd(tmp_path, source)) == [("id", expected)]
    # the same keys as class keywords
    keywords = f"class T(BaseModel, {config}):" if config else "class T(BaseModel):"
    if "SOMETHING" not in config:
        assert names(pyd(tmp_path, f"{keywords}\n    id: int = Field(alias='u')\n")) == [("id", expected)]
    # and as a pydantic dataclass's `config=`
    source = f"SOMETHING = {{}}\n@pdc(config=ConfigDict({config}))\nclass T:\n    id: int = Field(alias='u')\n"
    assert names(pyd(tmp_path, source)) == [("id", expected)]


def test_the_alias_equal_to_the_field_name_is_listed_once(tmp_path: Path) -> None:
    source = "class T(BaseModel, populate_by_name=True):\n    id: int = Field(alias='id')\n"
    assert names(pyd(tmp_path, source)) == [("id", ("id",))]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("class Config:\n        populate_by_name = True", ("u", "id")),
        ("class Config:\n        validate_by_name = True\n        validate_by_alias = True", ("u", "id")),
        ("class Config:\n        validate_by_alias = False", ("id",)),
        # pydantic 2 keeps this v1 key unrenamed and ignores it
        ("class Config:\n        allow_population_by_field_name = True", ("u",)),
        ("class Config:\n        extra = 'forbid'", ("u",)),
        ("class Config:\n        pass", ("u",)),
    ],
)
def test_a_class_config_is_read(tmp_path: Path, body: str, expected: tuple[str, ...]) -> None:
    source = f"class T(BaseModel):\n    {body}\n    id: int = Field(alias='u')\n"
    assert names(pyd(tmp_path, source)) == [("id", expected)]


def test_the_class_keywords_win_over_the_config(tmp_path: Path) -> None:
    source = (
        "class T(BaseModel, populate_by_name=False):\n    model_config = ConfigDict(populate_by_name=True)\n"
        "    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u",))]


def test_a_models_config_merges_through_its_bases_and_the_child_wins(tmp_path: Path) -> None:
    source = (
        "class A(BaseModel):\n    model_config = ConfigDict(populate_by_name=True)\n    a: int = Field(alias='x')\n"
        "class B(A, validate_by_alias=False):\n    b: int = Field(alias='y')\n"
        "class C(B):\n    model_config = ConfigDict(validate_by_alias=True)\n    c: int = Field(alias='z')\n"
        "class T(C):\n    model_config = ConfigDict(populate_by_name=False)\n    d: int = Field(alias='w')\n"
    )
    # A: alias+name; B: alias off, name on; C: alias on again with `populate_by_name` still set; T: name off
    assert names(pyd(tmp_path, source)) == [("a", ("x",)), ("b", ("y",)), ("c", ("z",)), ("d", ("w",))]
    source = (
        "class A(BaseModel):\n    model_config = ConfigDict(populate_by_name=True)\n"
        "class B(A):\n    id: int = Field(alias='u')\n"
        "class T(B):\n    n: int = Field(alias='m')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u", "id")), ("n", ("m", "n"))]


def test_the_last_base_wins_when_several_models_set_a_config_key(tmp_path: Path) -> None:
    source = (
        "class A(BaseModel, populate_by_name=True): ...\nclass B(BaseModel, populate_by_name=False): ...\n"
        "class T(A, B):\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u",))]
    source = source.replace("class T(A, B)", "class T(B, A)")
    assert names(pyd(tmp_path, source)) == [("id", ("u", "id"))]


def test_keys_from_different_bases_both_survive_and_a_class_keyword_wins_over_them(tmp_path: Path) -> None:
    source = (
        "class A(BaseModel, populate_by_name=True): ...\nclass B(BaseModel, validate_by_alias=True): ...\n"
        "class T(A, B):\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u", "id"))]
    source = source.replace("class T(A, B)", "class T(A, B, populate_by_name=False)")
    assert names(pyd(tmp_path, source)) == [("id", ("u",))]


def test_a_pydantic_dataclass_takes_the_decorators_config_else_the_nearest_bases(tmp_path: Path) -> None:
    source = (
        "@pdc(config=ConfigDict(populate_by_name=True))\nclass A:\n    a: int = Field(alias='x')\n"
        "@pdc\nclass B(A):\n    b: int = Field(alias='y')\n"
        "@pdc(config=ConfigDict(validate_by_alias=False))\nclass T(B):\n    c: int = Field(alias='z')\n"
    )
    # the decorator's config replaces the base's, and doesn't merge with it
    assert names(pyd(tmp_path, source)) == [("a", ("a",)), ("b", ("b",)), ("c", ("c",))]
    assert names(
        pyd(
            tmp_path,
            source.replace("class T(B)", "class T(B)").replace(
                "@pdc(config=ConfigDict(validate_by_alias=False))\nclass T", "@pdc\nclass T"
            ),
        )
    ) == [
        ("a", ("x", "a")),
        ("b", ("y", "b")),
        ("c", ("z", "c")),
    ]


def test_a_pydantic_dataclass_reads_its_pydantic_config_attribute(tmp_path: Path) -> None:
    source = (
        "@pdc\nclass T:\n    __pydantic_config__ = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u", "id"))]
    source = (
        "@pdc(config=ConfigDict(populate_by_name=False))\nclass T:\n"
        "    __pydantic_config__ = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u",))]


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            "class T(BaseModel):\n    @model_validator(mode='before')\n    @classmethod\n    def pre(cls, data):\n        return data\n    id: int\n",
            "`T` has a before-validator",
        ),
        (
            "class T(BaseModel):\n    id: int\n    @pd.model_validator(mode='wrap')\n    @classmethod\n    def w(cls, data, handler):\n        return handler(data)\n",
            "`T` has a wrap-validator",
        ),
        (
            "MODE = 'before'\nclass T(BaseModel):\n    id: int\n    @model_validator(mode=MODE)\n    @classmethod\n    def f(cls, data):\n        return data\n",
            "`T` has a model validator whose mode isn't a literal",
        ),
        (
            "class Base(BaseModel):\n    @model_validator(mode='before')\n    @classmethod\n    def pre(cls, data):\n        return data\nclass Mid(Base): ...\nclass T(Mid):\n    id: int\n",
            "`Base` has a before-validator",
        ),
        (
            "class T(BaseModel):\n    model_config = ConfigDict(alias_generator=str.upper)\n    id: int\n",
            "`T` has an alias_generator",
        ),
        (
            "class Base(BaseModel):\n    model_config = ConfigDict(alias_generator=str.upper)\nclass T(Base):\n    id: int\n",
            "`Base` has an alias_generator",
        ),
        (
            "class T(BaseModel, alias_generator=str.upper):\n    id: int\n",
            "`T` has an alias_generator",
        ),
        (
            "class T(BaseModel):\n    model_config = {'populate_by_name': True}\n    id: int\n",
            "`T` has a config that isn't a literal `ConfigDict(...)`",
        ),
        (
            "CONFIG = ConfigDict()\nclass T(BaseModel):\n    model_config = CONFIG\n    id: int\n",
            "`T` has a config that isn't a literal `ConfigDict(...)`",
        ),
        (
            "class T(BaseModel):\n    model_config = ConfigDict(**{'populate_by_name': True})\n    id: int\n",
            "`T` passes `**` to `ConfigDict`",
        ),
        (
            "FLAG = True\nclass T(BaseModel, populate_by_name=FLAG):\n    id: int\n",
            "the class keyword `populate_by_name` of `T` isn't a literal",
        ),
        (
            "class T(BaseModel):\n    model_config = ConfigDict(populate_by_name=FLAG)\n    id: int\nFLAG = True\n",
            "the config key `populate_by_name` of `T` isn't a literal",
        ),
        (
            "X = {}\nclass T(BaseModel, json_schema_extra=X):\n    id: int\n",
            "the class keyword `json_schema_extra` of `T` isn't a literal",
        ),
        (
            "OPTS = {}\nclass T(BaseModel, **OPTS):\n    id: int\n",
            "`T` takes `**` class keywords",
        ),
        (
            "class T(BaseModel):\n    class Config:\n        populate_by_name = FLAG\n    id: int\nFLAG = True\n",
            "the config key `populate_by_name` of `T` isn't a literal",
        ),
        (
            "class Cfg: ...\nclass T(BaseModel):\n    class Config(Cfg): ...\n    id: int\n",
            "the `Config` of `T` has a base class",
        ),
        (
            "CFG = ConfigDict()\n@pdc(config=CFG)\nclass T:\n    id: int\n",
            "`T` has a config that isn't a literal `ConfigDict(...)`",
        ),
        (
            "@pdc(config=ConfigDict(alias_generator=str.upper))\nclass T:\n    id: int\n",
            "`T` has an alias_generator",
        ),
        (
            "@pdc\nclass T:\n    id: int\n    @model_validator(mode='before')\n    @classmethod\n    def pre(cls, data):\n        return data\n",
            "`T` has a before-validator",
        ),
    ],
)
def test_each_model_level_trigger_makes_every_fields_names_unknown(tmp_path: Path, source: str, reason: str) -> None:
    resolution = pyd(tmp_path, source)
    assert [n for _, n in names(resolution)] == [None] * len(names(resolution))
    assert set(reasons(resolution)) == {reason}


def test_a_sticky_unreadable_config_survives_a_childs_own_config(tmp_path: Path) -> None:
    source = (
        "class Base(BaseModel):\n    model_config = {'populate_by_name': True}\n"
        "class T(Base):\n    model_config = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", None)]


def test_a_child_can_clear_an_inherited_alias_generator(tmp_path: Path) -> None:
    source = (
        "class Base(BaseModel):\n    model_config = ConfigDict(alias_generator=str.upper)\n"
        "class T(Base):\n    model_config = ConfigDict(alias_generator=None)\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u",))]


@pytest.mark.parametrize(
    "decorators",
    [
        "    @model_validator(mode='after')\n    def check(self):\n        return self\n",
        "    @pd.field_validator('id', mode='before')\n    @classmethod\n    def f(cls, v):\n        return v\n",
        "    @pd.field_validator('*', mode='wrap')\n    @classmethod\n    def f(cls, v, handler):\n        return handler(v)\n",
        "    @pd.computed_field\n    @property\n    def twice(self) -> int:\n        return 2\n",
        "    @staticmethod\n    def helper():\n        return 1\n",
    ],
)
def test_after_and_field_validators_keep_the_names_known(tmp_path: Path, decorators: str) -> None:
    resolution = pyd(tmp_path, f"class T(BaseModel):\n    id: int = Field(alias='u')\n{decorators}")
    assert names(resolution) == [("id", ("u",))]
    assert reasons(resolution) == [None]


def test_a_validator_in_a_plain_mixin_is_registered_by_pydantic(tmp_path: Path) -> None:
    mixin = "class Mixin:\n    @model_validator(mode='before')\n    @classmethod\n    def pre(cls, data):\n        return data\n"
    for head in ("class T(Mixin, BaseModel):", "class T(BaseModel, Mixin):"):
        resolution = pyd(tmp_path, f"{mixin}{head}\n    id: int\n")
        assert names(resolution) == [("id", None)]
        assert reasons(resolution) == ["`Mixin` has a before-validator"]


def test_an_unknown_field_next_to_known_ones_keeps_the_known_names(tmp_path: Path) -> None:
    source = (
        "class T(BaseModel):\n    a: int = Field(validation_alias=AliasChoices('x', 'y'))\n    b: int = Field(alias='bb')\n"
        "    c: int = Field(validation_alias=AliasPath('p', 0))\n"
    )
    resolution = pyd(tmp_path, source)
    assert names(resolution) == [("a", None), ("b", ("bb",)), ("c", None)]
    assert reasons(resolution) == ["field `a` uses AliasChoices", None, "field `c` uses AliasPath"]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("class T(RootModel[int]): ...\n", "a pydantic RootModel is not a row type"),
        ("class T(RootModel): ...\n", "a pydantic RootModel is not a row type"),
        ("import pydantic.root_model as r\nclass T(r.RootModel[int]): ...\n", "a pydantic RootModel is not a row type"),
        ("import pydantic.v1\nclass T(pydantic.v1.BaseModel):\n    id: int\n", "a pydantic v1 model is not supported"),
        (
            "from pydantic.v1 import BaseModel as V1\nclass T(V1):\n    id: int\n",
            "a pydantic v1 model is not supported",
        ),
        ("import pydantic.v1.main as m\nclass T(m.BaseModel):\n    id: int\n", "a pydantic v1 model is not supported"),
    ],
)
def test_a_root_model_and_pydantic_v1_are_invalid(tmp_path: Path, source: str, message: str) -> None:
    resolution = pyd(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.diagnostic.message == message  # ty: ignore[unresolved-attribute]


def test_another_pydantic_name_as_a_base_or_row_is_invalid(tmp_path: Path) -> None:
    assert rejected(pyd(tmp_path, "class T(pd.BaseModel, pd.Field): ...\n")) == "invalid-row-type"
    assert rejected(pyd(tmp_path, "T = BaseModel\n")) == "invalid-row-type"
    assert rejected(pyd(tmp_path, "@pd.validate_call\nclass T(BaseModel):\n    id: int\n")) == "invalid-row-type"


@pytest.mark.parametrize(
    "annotation",
    [
        "pd.EmailStr",
        "pd.AnyUrl",
        "pd.SecretStr",
        "pd.PositiveInt",
        "pd.constr(min_length=1)",
        "pd.Json[list[int]]",
        "pd.conint(gt=0)",
        "list[pd.EmailStr]",
        "pd.EmailStr | None",
        "Annotated[pd.PositiveInt, 'x']",
    ],
)
def test_pydantic_only_types_and_constraint_aliases_are_invalid_with_the_help(tmp_path: Path, annotation: str) -> None:
    resolution = pyd(tmp_path, f"class T(BaseModel):\n    id: int\n    x: {annotation}\n")
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help == (
        "annotate the column's Python type (for example `str`) and validate with `Annotated[str, AfterValidator(...)]`"
    )
    assert resolution.diagnostic.message.endswith('(field "x" of `T`)')  # ty: ignore[unresolved-attribute]


def test_a_pydantic_only_type_in_a_pydantic_dataclass_is_invalid(tmp_path: Path) -> None:
    resolution = pyd(tmp_path, "@pdc\nclass T:\n    x: pd.EmailStr\n")
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help is not None
    assert resolution.help.startswith("annotate the column's Python type")


def test_a_pydantic_help_is_only_for_pydantic_names(tmp_path: Path) -> None:
    resolution = pyd(tmp_path, "from fractions import Fraction\nclass T(BaseModel):\n    x: Fraction\n")
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help is None


def test_a_literal_init_false_on_the_pydantic_decorator_is_accepted(tmp_path: Path) -> None:
    for decorator in ("@pdc(init=False)", "@pd.dataclasses.dataclass(init=False, frozen=True)"):
        assert fields(pyd(tmp_path, f"{decorator}\nclass T:\n    id: int\n")) == [("id", False, True)]


def test_a_non_literal_pydantic_decorator_init_is_unresolved(tmp_path: Path) -> None:
    assert rejected(pyd(tmp_path, "X = False\n@pdc(init=X)\nclass T:\n    id: int\n")) == "unresolved-row-type"


def test_an_initvar_field_on_a_pydantic_dataclass_is_invalid(tmp_path: Path) -> None:
    resolution = pyd(tmp_path, "@pdc\nclass T:\n    id: int\n    scale: InitVar[int]\n")
    assert rejected(resolution) == "invalid-row-type"
    assert "InitVar" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


def test_field_init_false_excludes_a_field_only_on_a_pydantic_dataclass(tmp_path: Path) -> None:
    body = "    id: int\n    count: int = Field(init=False, default=0)\n    plain: int = Field(init=False)\n"
    assert fields(pyd(tmp_path, f"class T(BaseModel):\n{body}")) == [
        ("id", False, True),
        ("count", False, False),
        ("plain", False, True),
    ]
    assert fields(pyd(tmp_path, f"@pdc\nclass T:\n{body}")) == [("id", False, True)]
    source = "import dataclasses\n@pdc\nclass T:\n    id: int\n    n: int = dataclasses.field(init=False, default=0)\n"
    assert fields(pyd(tmp_path, source)) == [("id", False, True)]
    source = "@pdc\nclass T:\n    id: int\n    n: Annotated[int, Field(init=False)] = 0\n"
    assert fields(pyd(tmp_path, source)) == [("id", False, True)]


def test_a_redeclared_pydantic_dataclass_field_with_init_false_drops_the_inherited_one(tmp_path: Path) -> None:
    source = "@pdc\nclass A:\n    a: int\n    b: int\n@pdc\nclass T(A):\n    b: int = Field(init=False, default=0)\n"
    assert fields(pyd(tmp_path, source)) == [("a", False, True)]


def test_a_pydantic_dataclass_inherits_a_default_like_a_dataclass(tmp_path: Path) -> None:
    source = (
        "@pdc\nclass A:\n    x: int = Field(default=1)\n@pdc\nclass T(A):\n    x: int\n    y: int = Field(alias='w')\n"
    )
    assert fields(pyd(tmp_path, source)) == [("x", False, False), ("y", False, True)]
    source = "@pdc\nclass A:\n    x: int = Field(alias='a')\n@pdc\nclass T(A):\n    x: int\n"
    assert fields(pyd(tmp_path, source)) == [("x", False, True)]


def test_a_non_literal_field_init_on_a_pydantic_dataclass_is_unresolved(tmp_path: Path) -> None:
    assert (
        rejected(pyd(tmp_path, "X = False\n@pdc\nclass T:\n    id: int = Field(init=X, default=1)\n"))
        == "unresolved-row-type"
    )
    assert fields(pyd(tmp_path, "X = False\nclass T(BaseModel):\n    id: int = Field(init=X)\n")) == [
        ("id", False, True)
    ]


def test_a_field_with_double_star_arguments_is_unresolved(tmp_path: Path) -> None:
    assert (
        rejected(pyd(tmp_path, "OPTS = {}\nclass T(BaseModel):\n    id: int = Field(**OPTS)\n"))
        == "unresolved-row-type"
    )


def test_a_subclass_of_a_pydantic_dataclass_needs_the_decorator(tmp_path: Path) -> None:
    resolution = pyd(tmp_path, "@pdc\nclass A:\n    id: int\nclass T(A): ...\n")
    assert rejected(resolution) == "invalid-row-type"
    assert "without the pydantic decorator" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize(
    "source",
    [
        "from dataclasses import dataclass\n@dataclass\nclass M:\n    a: int\nclass T(BaseModel, M): ...\n",
        "from typing import NamedTuple\nclass T(NamedTuple, BaseModel):\n    a: int\n",
        "from dataclasses import dataclass\n@dataclass\n@pdc\nclass T:\n    a: int\n",
        "@pdc\nclass T(BaseModel):\n    a: int\n",
        "class T(BaseModel, metaclass=type):\n    a: int\n",
    ],
)
def test_mixing_a_model_with_another_form_is_invalid(tmp_path: Path, source: str) -> None:
    assert rejected(pyd(tmp_path, source)) == "invalid-row-type"


def test_a_model_with_no_fields_is_invalid(tmp_path: Path) -> None:
    resolution = pyd(tmp_path, "class T(BaseModel):\n    _x: int = 0\n    k: ClassVar[int] = 0\n")
    assert rejected(resolution) == "invalid-row-type"
    assert "has no fields" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


def test_a_model_from_another_module_resolves_with_its_field_notes(tmp_path: Path) -> None:
    extra = {
        "models.py": "from pydantic import BaseModel, Field\nclass User(BaseModel):\n    id: int = Field(alias='uid')\n"
    }
    write(tmp_path, {"main.py": "from models import User\n" + QUERY.format(t="User"), **extra})
    row = row_type(resolve(tmp_path, "main.py"))
    assert [(f.key, f.names, f.path.name, f.span.line) for f in row.fields] == [("id", ("uid",), "models.py", 3)]


def test_the_model_form_is_a_row_only_at_the_row_level(tmp_path: Path) -> None:
    source = "class U(BaseModel):\n    id: int\nT = tuple[U, int]\n"
    assert rejected(pyd(tmp_path, source)) == "invalid-row-type"
    source = "class U(BaseModel):\n    id: int\nT = U | None\n"
    assert rejected(pyd(tmp_path, source)) == "invalid-row-type"
    source = "class U(BaseModel):\n    id: int\nclass T(BaseModel):\n    u: U\n"
    assert rejected(pyd(tmp_path, source)) == "invalid-row-type"


def test_a_quoted_and_future_annotations_work_in_a_model(tmp_path: Path) -> None:
    source = "class T(BaseModel):\n    a: 'int'\n    b: 'str | None' = None\n"
    assert [(f.key, f.elem.leaf, f.elem.nullable) for f in row_type(pyd(tmp_path, source)).fields] == [
        ("a", "int", False),
        ("b", "str", True),
    ]
    source = (
        "from __future__ import annotations\n"
        + PYDANTIC
        + "class T(BaseModel):\n    a: int\n    b: str | None = None\n"
    )
    assert fields(named(tmp_path, source)) == [("a", False, True), ("b", True, False)]


BLOCKED_PYDANTIC: Final[str] = """\
import json
import sys
from pathlib import Path

sys.modules["pydantic"] = None
sys.modules["pydantic.dataclasses"] = None
sys.modules["pydantic.v1"] = None
from rowx.check.discover import discover_file
from rowx.check.resolve import ModuleGraph, resolve_row_type

path = Path(sys.argv[1])
[call] = discover_file(path).calls
resolution = resolve_row_type(ModuleGraph(), call, (path.parent,))
if resolution.diagnostic is not None:
    print(json.dumps(["error", resolution.diagnostic.rule, resolution.diagnostic.message, resolution.help]))
else:
    row = resolution.row_type
    print(json.dumps([[f.key, f.names, f.required, f.reason] for f in row.fields]))
"""


@pytest.mark.parametrize(
    "source",
    [
        "class T(BaseModel):\n    id: int = Field(alias='u')\n    n: str | None = None\n",
        "class T(BaseModel, populate_by_name=True):\n    id: int = Field(alias='u')\n",
        "class T(BaseModel):\n    id: int = Field(validation_alias=AliasChoices('a', 'b'))\n",
        "@pdc(config=ConfigDict(populate_by_name=True))\nclass T:\n    id: int = Field(alias='u')\n",
        "class T(RootModel[int]): ...\n",
        "class T(BaseModel):\n    u: pd.AnyUrl\n",
    ],
)
def test_the_result_is_identical_when_pydantic_cannot_be_imported(tmp_path: Path, source: str) -> None:
    resolution = pyd(tmp_path, source)
    expected = (
        ["error", resolution.diagnostic.rule, resolution.diagnostic.message, resolution.help]
        if resolution.diagnostic is not None
        else [
            [f.key, list(f.names) if f.names is not None else None, f.required, f.reason]
            for f in row_type(resolution).fields
        ]
    )
    done = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-I", "-c", BLOCKED_PYDANTIC, str(tmp_path / "main.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(done.stdout) == expected


def test_the_resolver_never_touches_a_pydantic_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a poisoned entry fails any import or attribute access of pydantic
    for name in ("pydantic", "pydantic.dataclasses", "pydantic.v1"):
        monkeypatch.setitem(sys.modules, name, None)
    source = "class T(BaseModel):\n    id: int = Field(alias='u')\n"
    assert names(pyd(tmp_path, source)) == [("id", ("u",))]


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


TYPE_CHECKING_HEADER = (
    "from typing import TYPE_CHECKING, NamedTuple, TypeAlias, TypedDict\nfrom dataclasses import dataclass\n"
    "if TYPE_CHECKING:\n    from fractions import Fraction\n"
)


@pytest.mark.parametrize(
    "alias",
    [
        "type D = Fraction\n",
        "D = Fraction\n",
        "D: TypeAlias = Fraction\n",
        "type D = list[Fraction]\n",
        "D = Fraction | None\n",
        "type E = Fraction\ntype D = E\n",
    ],
)
@pytest.mark.parametrize(
    "body",
    [
        "@dataclass\nclass T:\n    a: D\n",
        "@dataclass\nclass T:\n    a: list[D]\n",
        "@dataclass\nclass T:\n    a: D | None\n",
        "class T(NamedTuple):\n    a: D\n",
        "class T(TypedDict):\n    a: D\n",
        "T = TypedDict('T', {'a': D})\n",
    ],
)
def test_a_type_checking_only_name_behind_an_alias_in_a_field_is_rejected(
    tmp_path: Path, alias: str, body: str
) -> None:
    resolution = named(tmp_path, TYPE_CHECKING_HEADER + alias + body)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help == "import `Fraction` at runtime"


def test_a_type_checking_only_name_behind_an_alias_from_another_module_is_rejected(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\nfrom aliases import D\n@dataclass\nclass T:\n    a: D\n"
    extra = {
        "aliases.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import datetime\ntype D = datetime.date\n"
    }
    resolution = named(tmp_path, source, extra=extra)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.help == "import `datetime` at runtime"


def test_an_alias_of_a_runtime_name_is_fine_in_a_field_and_a_quoted_alias_body_stays_rejected(tmp_path: Path) -> None:
    header = "from dataclasses import dataclass\nimport datetime\n"
    source = header + "type D = datetime.date\n@dataclass\nclass T:\n    a: D\n    b: list[D | None]\n"
    assert fields(named(tmp_path, source)) == [("a", False, True), ("b", False, True)]
    quoted = header + "type D = 'datetime.date'\n@dataclass\nclass T:\n    a: D\n"
    assert rejected(named(tmp_path, quoted)) == "invalid-row-type"


def test_an_inconsistent_mro_is_unresolved(tmp_path: Path) -> None:
    source = (
        "from dataclasses import dataclass\nclass A: ...\nclass B(A): ...\n@dataclass\nclass T(A, B):\n    x: int\n"
    )
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "unresolved-row-type"
    assert "method resolution order" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


def test_a_class_that_inherits_from_itself_is_unresolved(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\n@dataclass\nclass A(B):\n    a: int\n@dataclass\nclass B(A):\n    b: int\nT = A\n"
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "unresolved-row-type"
    assert "inherits from itself" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


def test_an_inheritance_chain_has_no_depth_cap(tmp_path: Path) -> None:
    depth = 40
    lines = ["from dataclasses import dataclass", "@dataclass", "class C0:", "    a: int"]
    for index in range(1, depth):
        lines += ["@dataclass", f"class C{index}(C{index - 1}):", f"    f{index}: int"]
    lines.append(f"T = C{depth - 1}")
    resolution = named(tmp_path, "\n".join(lines) + "\n")
    assert [key for key, _, _ in fields(resolution)] == ["a", *(f"f{index}" for index in range(1, depth))]


@pytest.mark.parametrize(
    "block",
    [
        "    if True:\n        b: int\n",
        "    try:\n        b: int\n    except ImportError:\n        pass\n",
        "    with open('x') as handle:\n        b: int\n",
        "    for _ in ():\n        b: int\n",
    ],
)
@pytest.mark.parametrize("head", ["@dataclass\nclass T:\n    a: int\n", "class T(TypedDict):\n    a: int\n"])
def test_a_field_in_a_conditional_block_is_rejected(tmp_path: Path, head: str, block: str) -> None:
    resolution = named(tmp_path, "from dataclasses import dataclass\nfrom typing import TypedDict\n" + head + block)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.message == "a field declared inside a conditional block can't be checked"


def test_a_conditional_assignment_without_an_annotation_is_not_a_field(tmp_path: Path) -> None:
    source = "from dataclasses import dataclass\n@dataclass\nclass T:\n    a: int\n    if True:\n        c = 1\n"
    assert fields(named(tmp_path, source)) == [("a", False, True)]


def test_a_private_field_is_keyed_by_its_mangled_name(tmp_path: Path) -> None:
    source = (
        "from dataclasses import dataclass\n@dataclass\nclass _T:\n    __x: int\n    __y__: int\n    _z: int\nT = _T\n"
    )
    assert [key for key, _, _ in fields(named(tmp_path, source))] == ["_T__x", "__y__", "_z"]


def test_a_known_root_never_resolves_into_a_project_module(tmp_path: Path) -> None:
    source = "from rowx_postgres import Money\nT = Money\n"
    stub = "class Money:\n    pass\n"
    extra = {"rowx/postgres/_money.py": stub, "rowx/postgres/__init__.py": "from ._money import Money\n"}
    assert leaves(named(tmp_path, source, extra=extra)) == [("rowx_postgres.Money", False)]


def test_a_type_checking_block_in_a_class_body_adds_no_field(tmp_path: Path) -> None:
    source = "from typing import TYPE_CHECKING\nfrom dataclasses import dataclass\n"
    source += "@dataclass\nclass T:\n    a: int\n    if TYPE_CHECKING:\n        b: int\n"
    assert fields(named(tmp_path, source)) == [("a", False, True)]


def test_the_else_of_a_type_checking_block_in_a_class_body_adds_fields(tmp_path: Path) -> None:
    source = "from typing import TYPE_CHECKING\nfrom dataclasses import dataclass\n"
    source += "@dataclass\nclass T:\n    a: int\n    if TYPE_CHECKING:\n        ...\n    else:\n        b: int = 1\n"
    assert fields(named(tmp_path, source)) == [("a", False, True), ("b", False, False)]


def test_a_field_is_read_through_an_alias(tmp_path: Path) -> None:
    alias = "from typing import TypeAlias\nId: TypeAlias = Annotated[int, Field(alias='user_id')]\n"
    assert names(pyd(tmp_path, f"{alias}class T(BaseModel):\n    id: Id\n")) == [("id", ("user_id",))]
    plain = "Id = Annotated[int, Field(alias='user_id')]\n"
    assert names(pyd(tmp_path, f"{plain}class T(BaseModel):\n    id: Id\n")) == [("id", ("user_id",))]
    assert names(pyd(tmp_path, f"{plain}class T(BaseModel):\n    id: 'Id'\n")) == [("id", ("user_id",))]
    nested = f"{plain}class T(BaseModel):\n    id: Annotated[Id, Field(gt=0)]\n"
    assert names(pyd(tmp_path, nested)) == [("id", ("user_id",))]
    chained = f"{plain}Alias = Id\nclass T(BaseModel):\n    id: Alias\n"
    assert names(pyd(tmp_path, chained)) == [("id", ("user_id",))]
    default = "Id = Annotated[int, Field(default=5)]\nclass T(BaseModel):\n    id: Id\n"
    assert fields(pyd(tmp_path, default)) == [("id", False, False)]
    dataclass = f"{plain}@pdc\nclass T:\n    id: Id\n"
    assert names(pyd(tmp_path, dataclass)) == [("id", ("user_id",))]


def test_a_field_is_read_through_an_alias_of_another_module(tmp_path: Path) -> None:
    extra = {
        "ids.py": "from typing import Annotated\nfrom pydantic import Field\nId = Annotated[int, Field(alias='uid')]\n"
    }
    for imports in ("from ids import Id\n", "import ids\n"):
        annotation = "Id" if "from" in imports else "ids.Id"
        source = f"{imports}class T(BaseModel):\n    id: {annotation}\n"
        assert names(pyd(tmp_path, source, extra=extra)) == [("id", ("uid",))]


def test_a_type_statement_alias_gives_pydantic_no_field_metadata(tmp_path: Path) -> None:
    # pydantic drops the `Field` attributes inside a `type` alias, and the runtime follows pydantic
    alias = "type Id = Annotated[int, Field(alias='user_id')]\nclass T(BaseModel):\n    id: Id\n"
    assert names(pyd(tmp_path, alias)) == [("id", ("id",))]
    default = "type D = Annotated[int, Field(default=1)]\nclass T(BaseModel):\n    id: D\n"
    assert fields(pyd(tmp_path, default)) == [("id", False, True)]


def test_a_plain_alias_with_a_field_call_is_a_row_type_alias(tmp_path: Path) -> None:
    resolution = named(tmp_path, PYDANTIC + "Id = Annotated[int, Field(alias='x')]\nT = tuple[Id, Id | None]\n")
    assert leaves(resolution) == [("int", False), ("int", True)]
    # a call outside the `Annotated` metadata still isn't a type
    assert rejected(named(tmp_path, PYDANTIC + "Id = Annotated[int, 'a'] | make()\nT = tuple[Id]\n")) is not None


@pytest.mark.parametrize(
    "annotation",
    [
        "CV",
        "CV2",
        "Annotated[ClassVar[int], 'meta']",
        "'CV'",
        "Annotated[CV, 'meta']",
        "typing.ClassVar[int]",
    ],
)
def test_a_class_var_behind_an_alias_or_annotated_is_no_pydantic_field(tmp_path: Path, annotation: str) -> None:
    alias = "import typing\nfrom typing import TypeAlias\nCV = ClassVar[int]\nCV2: TypeAlias = ClassVar[int]\n"
    assert names(pyd(tmp_path, f"{alias}class T(BaseModel):\n    a: int\n    b: {annotation}\n")) == [("a", ("a",))]
    source = f"{alias}@pdc\nclass T:\n    a: int\n    b: {annotation}\n"
    assert names(pyd(tmp_path, source)) == [("a", ("a",))]


def test_kw_only_behind_an_alias_is_no_pydantic_dataclass_field(tmp_path: Path) -> None:
    source = "from dataclasses import KW_ONLY\nK = KW_ONLY\n@pdc\nclass T:\n    a: int\n    _: K\n    b: int\n"
    assert names(pyd(tmp_path, source)) == [("a", ("a",)), ("b", ("b",))]
    annotated = (
        "from dataclasses import KW_ONLY\n@pdc\nclass T:\n    a: int\n    _: Annotated[KW_ONLY, 1]\n    b: int\n"
    )
    # pydantic unwraps `Annotated` only for a `ClassVar`, so this `KW_ONLY` is a field type it can't build a schema for
    assert rejected(pyd(tmp_path, annotated)) == "invalid-row-type"


def test_annotated_unwraps_only_for_a_class_var_in_a_pydantic_dataclass(tmp_path: Path) -> None:
    # the runtime keeps this as a field, so the checker sees a field type (which it can't model), not an `InitVar`
    source = "@pdc\nclass T:\n    a: int\n    b: Annotated[InitVar[int], 'm']\n"
    resolution = pyd(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.diagnostic is not None
    assert "field" in resolution.diagnostic.message
    assert "InitVar` field" not in resolution.diagnostic.message
    nested = "@pdc\nclass T:\n    a: int\n    b: Annotated[Annotated[ClassVar[int], 1], 2]\n"
    assert names(pyd(tmp_path, nested)) == [("a", ("a",))]


def test_an_aliased_annotated_is_followed_to_find_a_class_var(tmp_path: Path) -> None:
    source = "A = Annotated\nclass T(BaseModel):\n    a: int\n    b: A[ClassVar[int], 'm']\n"
    assert names(pyd(tmp_path, source)) == [("a", ("a",))]
    source = "A = Annotated\n@pdc\nclass T:\n    a: int\n    b: A[ClassVar[int], 'm']\n"
    assert names(pyd(tmp_path, source)) == [("a", ("a",))]


def test_a_stdlib_dataclass_reads_a_class_var_alias_but_not_annotated(tmp_path: Path) -> None:
    head = "from dataclasses import dataclass\nCV = ClassVar[int]\nIV = InitVar[int]\n"
    assert names(pyd(tmp_path, f"{head}@dataclass\nclass T:\n    a: int\n    b: CV = 1\n")) == [("a", ("a",))]
    assert rejected(pyd(tmp_path, f"{head}@dataclass\nclass T:\n    a: int\n    b: IV\n")) == "invalid-row-type"
    annotated = f"{head}@dataclass\nclass T:\n    a: int\n    b: Annotated[ClassVar[int], 1] = 1\n"
    assert rejected(pyd(tmp_path, annotated)) == "invalid-row-type"


def test_a_before_validator_in_a_conditional_block_makes_every_name_unknown(tmp_path: Path) -> None:
    validator = "        @model_validator(mode='before')\n        @classmethod\n        def pre(cls, data):\n            return data\n"
    source = f"import sys\nclass T(BaseModel):\n    if sys.platform:\n{validator}    id: int\n"
    resolution = pyd(tmp_path, source)
    assert names(resolution) == [("id", None)]
    assert reasons(resolution) == ["`T` has a validator in a conditional block"]
    # an after-validator leaves the names alone
    after = source.replace("mode='before'", "mode='after'")
    assert names(pyd(tmp_path, after)) == [("id", ("id",))]
    try_block = f"class T(BaseModel):\n    try:\n{validator}    except Exception:\n        pass\n    id: int\n"
    assert names(pyd(tmp_path, try_block)) == [("id", None)]


def test_config_in_a_conditional_block_makes_every_name_unknown(tmp_path: Path) -> None:
    for config in (
        "model_config = ConfigDict(populate_by_name=True)",
        "class Config:\n            populate_by_name = True",
    ):
        source = (
            f"import sys\nclass T(BaseModel):\n    if sys.platform:\n        {config}\n    id: int = Field(alias='u')\n"
        )
        resolution = pyd(tmp_path, source)
        assert names(resolution) == [("id", None)]
        assert reasons(resolution) == ["`T` sets config in a conditional block"]
    # a base's conditional config reaches the subclass
    base = "import sys\nclass B(BaseModel):\n    if sys.platform:\n        model_config = ConfigDict(populate_by_name=True)\n"
    assert names(pyd(tmp_path, f"{base}class T(B):\n    id: int\n")) == [("id", None)]


def test_the_else_of_a_type_checking_block_in_a_class_is_not_conditional(tmp_path: Path) -> None:
    source = (
        "from typing import TYPE_CHECKING\nclass T(BaseModel):\n    if TYPE_CHECKING:\n        pass\n    else:\n"
        "        model_config = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u", "id"))]
    # a config in the `if TYPE_CHECKING:` body never runs
    hidden = (
        "from typing import TYPE_CHECKING\nclass T(BaseModel):\n    if TYPE_CHECKING:\n"
        "        model_config = ConfigDict(populate_by_name=True)\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, hidden)) == [("id", ("u",))]


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("mode='before'", "`T` has a before-validator"),
        ("mode='wrap'", "`T` has a wrap-validator"),
        ("mode=MODE", "`T` has a model validator whose mode isn't a literal"),
    ],
)
def test_an_assigned_model_validator_makes_every_name_unknown(tmp_path: Path, mode: str, reason: str) -> None:
    source = (
        "MODE = 'before'\ndef g(cls, data):\n    return data\n"
        f"class T(BaseModel):\n    f = model_validator({mode})(classmethod(g))\n    id: int\n"
    )
    resolution = pyd(tmp_path, source)
    assert names(resolution) == [("id", None)]
    assert reasons(resolution) == [reason]


def test_an_assigned_after_validator_leaves_the_names_known(tmp_path: Path) -> None:
    source = (
        "def g(self):\n    return self\nclass T(BaseModel):\n    f = model_validator(mode='after')(g)\n    id: int\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("id",))]


def test_an_ellipsis_clears_an_annotated_default_on_a_model_only(tmp_path: Path) -> None:
    assert fields(pyd(tmp_path, "class T(BaseModel):\n    a: Annotated[int, Field(default=3)] = ...\n")) == [
        ("a", False, True)
    ]
    # a `default_factory` survives it, and a pydantic dataclass keeps the default
    factory = "class T(BaseModel):\n    a: Annotated[int, Field(default_factory=int)] = ...\n"
    assert fields(pyd(tmp_path, factory)) == [("a", False, False)]
    assert fields(pyd(tmp_path, "@pdc\nclass T:\n    a: Annotated[int, Field(default=3)] = ...\n")) == [
        ("a", False, False)
    ]


@pytest.mark.parametrize(
    ("parent", "child", "expected"),
    [
        # a `Field(...)` without a default stays the class attribute, so a bare redeclaration merges it
        ("b: int = Field(alias='B')", "b: int", (("B",), True)),
        ("b: int = Field(alias='B')", "b: Annotated[int, Field(gt=0)]", (("B",), True)),
        ("b: int = Field(alias='B')", "b: Annotated[int, Field(alias='C')]", (("B",), True)),
        ("b: int = Field(alias='B', default_factory=int)", "b: int", (("B",), False)),
        # one with a default becomes that plain default, and its alias is gone
        ("b: int = Field(alias='B', default=1)", "b: int", (("b",), False)),
        ("b: int = Field(alias='B')", "b: int = Field(alias='C')", (("C",), True)),
        ("b: int = Field(alias='B')", "b: int = 7", (("b",), False)),
        ("b: int = Field(gt=0)", "b: int", (("b",), True)),
        ("b: Annotated[int, Field(alias='B')]", "b: int", (("b",), True)),
    ],
)
def test_a_pydantic_dataclass_redeclaration_inherits_the_parents_field(
    tmp_path: Path, parent: str, child: str, expected: tuple[tuple[str, ...], bool]
) -> None:
    source = f"@pdc\nclass P:\n    {parent}\n@pdc\nclass T(P):\n    {child}\n"
    resolution = pyd(tmp_path, source)
    [field] = row_type(resolution).fields
    assert (field.names, field.required) == expected


def test_a_redeclaration_inherits_through_an_intermediate_class(tmp_path: Path) -> None:
    source = (
        "@pdc\nclass P:\n    b: int = Field(alias='B')\n@pdc\nclass Q(P):\n    b: int\n@pdc\nclass T(Q):\n    b: int\n"
    )
    assert names(pyd(tmp_path, source)) == [("b", ("B",))]


def test_a_second_model_config_replaces_the_first(tmp_path: Path) -> None:
    source = (
        "class T(BaseModel):\n    model_config = ConfigDict(populate_by_name=True)\n"
        "    model_config = ConfigDict(validate_by_alias=True)\n    id: int = Field(alias='u')\n"
    )
    assert names(pyd(tmp_path, source)) == [("id", ("u",))]


@pytest.mark.parametrize(
    "source",
    [
        "from dataclasses import dataclass\n@dataclass\nclass D:\n    x: int = 1\nclass T(BaseModel, D):\n    id: int\n",
        "@pdc\nclass P:\n    x: int\nfrom dataclasses import dataclass\n@dataclass\nclass T(P):\n    id: int\n",
        "from dataclasses import dataclass\n@dataclass\nclass S:\n    x: int\n@pdc\nclass T(S):\n    id: int\n",
    ],
)
def test_mixed_row_type_forms_are_rejected_with_help(tmp_path: Path, source: str) -> None:
    resolution = pyd(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert "mixes row type forms" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]
    assert resolution.help is not None


DATACLASS: Final[str] = "from dataclasses import dataclass\n"
CRATE: Final[str] = DATACLASS + "@dataclass\nclass Crate[X]:\n    a: X\n    b: str\n"


def pairs(resolution: Resolution) -> list[tuple[str, bool]]:
    return [(f.key, f.elem.nullable) for f in row_type(resolution).fields]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("type Row[X] = tuple[X, str]\nT = Row[int]\n", [("int", False), ("str", False)]),
        ("type Swap[A, B] = tuple[B, A]\nT = Swap[int, str | None]\n", [("str", True), ("int", False)]),
        ("type Pair[A, B] = tuple[A, B]\nT = Pair[int, str | None]\n", [("int", False), ("str", True)]),
        ("type Nested[X] = tuple[list[X | None], X | None]\nT = Nested[int]\n", [("list", False), ("int", True)]),
        (
            "type Inner[X] = tuple[X, str]\ntype Outer[Y] = Inner[Y | None]\nT = Outer[int]\n",
            [("int", True), ("str", False)],
        ),
        ("type Row[X = int] = tuple[X, str]\nT = Row\n", [("int", False), ("str", False)]),
        ("type Row[A, B = str | None] = tuple[A, B]\nT = Row[int]\n", [("int", False), ("str", True)]),
        ("type Row[X] = tuple[X, str]\nSub = Row[int]\nT = Sub\n", [("int", False), ("str", False)]),
    ],
)
def test_a_subscripted_generic_alias_is_substituted_through_its_parameters(
    tmp_path: Path, source: str, expected: list[tuple[str, bool]]
) -> None:
    assert leaves(named(tmp_path, source)) == expected


def test_a_scalar_generic_alias(tmp_path: Path) -> None:
    assert leaves(named(tmp_path, "type Opt[X] = X | None\nT = Opt[int]\n")) == [("int", True)]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("type Row[X] = tuple[X, str]\nT = Row\n", "the type parameter `X` of `Row` is not bound"),
        ("type Row[X] = tuple[X, str]\nT = Row[int, str]\n", "`Row` takes 1 type arguments, but 2 were given"),
        ("type Two[A, B] = tuple[A, B]\nT = Two[int]\n", "`Two` takes 2 type arguments, but 1 were given"),
        ("type Two[A, B = int] = tuple[A, B]\nT = Two[int, str, bytes]\n", "`Two` takes 1 to 2 type arguments"),
        ("type Row[*Ts] = tuple[int, str]\nT = Row[int]\n", "the TypeVarTuple `Ts`"),
        ("type Row[**P] = tuple[int, str]\nT = Row[[int]]\n", "the ParamSpec `P`"),
        ("type Row = tuple[int, str]\nT = Row[int]\n", "`Row` takes 0 type arguments, but 1 were given"),
    ],
)
def test_a_generic_alias_is_rejected_for_an_unbound_parameter_a_wrong_count_or_an_unsupported_kind(
    tmp_path: Path, source: str, message: str
) -> None:
    resolution = named(tmp_path, source)
    assert rejected(resolution) == "invalid-row-type"
    assert message in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


def test_an_unbound_parameter_has_a_help(tmp_path: Path) -> None:
    resolution = named(tmp_path, "type Row[X] = tuple[X, str]\nT = Row\n")
    assert resolution.help == "subscript the class or alias, for example `Row[int]`"


def test_the_arguments_are_read_at_the_call_site_and_the_body_in_the_definition(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "defs.py": "import datetime as dt\nfrom decimal import Decimal as X\ntype Row[Y] = tuple[Y, dt.date, X]\n",
            "main.py": "from defs import Row\nimport uuid as dt\nfrom rowx_postgres import Money as X\n"
            + QUERY.format(t="Row[dt.UUID | None]"),
        },
    )
    assert leaves(resolve(tmp_path, "main.py")) == [
        ("uuid.UUID", True),
        ("datetime.date", False),
        ("decimal.Decimal", False),
    ]


def test_a_type_parameter_shadows_a_module_name_inside_its_definition(tmp_path: Path) -> None:
    write(tmp_path, {"main.py": "T = int\ntype Row[T] = tuple[T, str]\n" + QUERY.format(t="Row[bytes]")})
    assert leaves(resolve(tmp_path, "main.py")) == [("bytes", False), ("str", False)]


def test_a_generic_dataclass_is_substituted_and_named_with_its_arguments(tmp_path: Path) -> None:
    row = row_type(named(tmp_path, CRATE + "T = Crate[int | None]\n"))
    assert row.type_name == "Crate[int | None]"
    assert pairs(named(tmp_path, CRATE + "T = Crate[int | None]\n")) == [("a", True), ("b", False)]


@pytest.mark.parametrize(
    ("argument", "name"),
    [("int", "int"), ("dt.date", "date"), ("list[int]", "list[int]"), ("int | None", "int | None")],
)
def test_the_type_name_renders_the_arguments(tmp_path: Path, argument: str, name: str) -> None:
    source = "import datetime as dt\n" + CRATE + f"T = Crate[{argument}]\n"
    assert row_type(named(tmp_path, source)).type_name == f"Crate[{name}]"


def test_the_type_name_follows_an_alias_to_its_arguments(tmp_path: Path) -> None:
    source = CRATE + "type B[Y] = Crate[Y | None]\nT = B[int]\n"
    row = row_type(named(tmp_path, source))
    assert row.type_name == "Crate[int | None]"
    assert [f.elem.nullable for f in row.fields] == [True, False]


def test_a_parameter_named_like_the_row_alias_still_reads_as_the_parameter(tmp_path: Path) -> None:
    source = DATACLASS + "@dataclass\nclass Crate[T]:\n    a: T\nT = Crate[int]\n"
    assert pairs(named(tmp_path, source)) == [("a", False)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "X = TypeVar('X')\n@dataclass\nclass Crate(Generic[X]):\n    a: X\nT = Crate[int | None]\n",
            [("a", True)],
        ),
        (
            (
                "A = TypeVar('A')\nB = TypeVar('B')\n@dataclass\nclass Crate(Generic[A, B]):\n    a: B\n    b: A\n"
                "T = Crate[int, str | None]\n"
            ),
            [("a", True), ("b", False)],
        ),
        (
            "X = TypeVar('X', default=int)\n@dataclass\nclass Crate(Generic[X]):\n    a: X\nT = Crate\n",
            [("a", False)],
        ),
        ("@dataclass\nclass Crate[X = int | None]:\n    a: X\nT = Crate\n", [("a", True)]),
        (
            "@dataclass\nclass Crate[A = int, B = list[A]]:\n    a: A\n    b: B\nT = Crate\n",
            [("a", False), ("b", False)],
        ),
    ],
)
def test_generic_classes_in_each_spelling_and_with_defaults(
    tmp_path: Path, source: str, expected: list[tuple[str, bool]]
) -> None:
    head = DATACLASS + "from typing import Generic, TypeVar\n"
    assert pairs(named(tmp_path, head + source)) == expected


@pytest.mark.parametrize(
    "source",
    [
        "@dataclass\nclass Crate[X]:\n    a: X\nT = Crate\n",
        "@dataclass\nclass Crate[X]:\n    a: X\nT = Crate[int, str]\n",
        "@dataclass\nclass Crate[A, B]:\n    a: A\nT = Crate[int]\n",
        "@dataclass\nclass Crate[*Ts]:\n    a: int\nT = Crate[int]\n",
        "@dataclass\nclass Crate[**P]:\n    a: int\nT = Crate[[int]]\n",
        "@dataclass\nclass Base[X]:\n    a: X\n@dataclass\nclass T(Base):\n    b: int\n",
        "@dataclass\nclass Crate[X]:\n    a: X\ntype B[Y] = Crate[Y]\nT = B\n",
    ],
)
def test_a_generic_class_is_rejected_for_an_unbound_parameter_a_wrong_count_or_an_unsupported_kind(
    tmp_path: Path, source: str
) -> None:
    assert rejected(named(tmp_path, DATACLASS + source)) == "invalid-row-type"


def test_inherited_parameters_are_rebound_through_project_bases(tmp_path: Path) -> None:
    source = (
        DATACLASS
        + "@dataclass\nclass Base[A, B]:\n    a: A\n    b: B\n"
        + "@dataclass\nclass Child[U](Base[U | None, str]):\n    c: U\nT = Child[int]\n"
    )
    row = row_type(named(tmp_path, source))
    assert row.type_name == "Child[int]"
    assert [(f.key, f.elem.leaf, f.elem.nullable) for f in row.fields] == [
        ("a", "int", True),
        ("b", "str", False),
        ("c", "int", False),
    ]


def test_inherited_parameters_are_rebound_through_a_chain_of_bases_in_other_modules(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "base.py": DATACLASS + "@dataclass\nclass Base[X]:\n    a: X\n",
            "mid.py": DATACLASS + "from base import Base\n@dataclass\nclass Mid[Y](Base[Y | None]):\n    b: Y\n",
            "main.py": DATACLASS
            + "from mid import Mid\n@dataclass\nclass Child[U](Mid[U]):\n    c: U\n"
            + QUERY.format(t="Child[int]"),
        },
    )
    assert pairs(resolve(tmp_path, "main.py")) == [("a", True), ("b", False), ("c", False)]


def test_generic_typeddict_namedtuple_and_pydantic_bases(tmp_path: Path) -> None:
    typeddict = (
        "from typing import NotRequired, TypedDict\nclass Base[X](TypedDict):\n    a: X\n"
        "class Child[U](Base[U | None], TypedDict):\n    b: NotRequired[U]\nT = Child[int]\n"
    )
    resolution = named(tmp_path, typeddict)
    assert [(f.key, f.elem.nullable, f.required) for f in row_type(resolution).fields] == [
        ("a", True, True),
        ("b", False, False),
    ]
    namedtuple = "from typing import NamedTuple\nclass T[X](NamedTuple):\n    a: X\n    b: str\nT = T[int | None]\n"
    assert pairs(named(tmp_path, namedtuple.replace("class T[", "class P[").replace("T = T[", "T = P["))) == [
        ("a", True),
        ("b", False),
    ]


def test_a_generic_pydantic_model_is_substituted(tmp_path: Path) -> None:
    source = (
        "from pydantic import BaseModel\nclass Page[X](BaseModel):\n    a: X\n    b: str\n"
        "class Child[U](Page[U | None]):\n    c: U\nT = Child[int]\nP = Page[int]\n"
    )
    resolution = named(tmp_path, source)
    row = row_type(resolution)
    assert (row.form, row.type_name) == ("validator", "Child[int]")
    assert pairs(resolution) == [("a", True), ("b", False), ("c", False)]
    page = named(tmp_path / "other", source.replace("T = Child[int]", "T = Page[int]"))
    assert row_type(page).type_name == "Page[int]"


def test_a_generic_pydantic_dataclass_and_a_default_are_substituted(tmp_path: Path) -> None:
    source = "import pydantic\n@pydantic.dataclasses.dataclass\nclass T[X = int]:\n    a: X\n    b: str\n"
    resolution = named(tmp_path, source)
    assert pairs(resolution) == [("a", False), ("b", False)]
    assert row_type(resolution).type_name == "T[int]"


def test_a_generic_alias_note_names_the_definition_and_the_arguments(tmp_path: Path) -> None:
    resolution = named(tmp_path, "type Pair[A, B] = tuple[A, B]\nT = Pair[int, str | None]\n")
    alias = row_type(resolution).alias
    assert alias is not None
    path = tmp_path / "main.py"
    assert alias.notes() == (
        f"alias defined at {path}:2:1",
        f"generic alias `Pair` defined at {path}:1:1, with A = int, B = str | None",
    )


def test_a_plain_alias_of_type_variables_is_a_generic_alias(tmp_path: Path) -> None:
    source = "from typing import TypeVar\nA = TypeVar('A')\nB = TypeVar('B')\nPair = tuple[B, A]\nT = Pair[int, str | None]\n"
    resolution = named(tmp_path, source)
    assert leaves(resolution) == [("int", False), ("str", True)]
    alias = row_type(resolution).alias
    assert alias is not None
    assert (
        alias.notes()[-1] == f"generic alias `Pair` defined at {tmp_path / 'main.py'}:4:1, with B = int, A = str | None"
    )


def test_a_plain_assignment_of_a_generic_alias_is_the_same_generic_alias(tmp_path: Path) -> None:
    source = "from typing import TypeVar\nA = TypeVar('A')\nPair = tuple[A, str]\nOuter = Pair\nT = Outer[int | None]\n"
    assert leaves(named(tmp_path, source)) == [("int", True), ("str", False)]


@pytest.mark.parametrize("subscript", ["", "[int, str]"])
def test_a_plain_generic_alias_is_rejected_for_an_unbound_parameter_or_a_wrong_count(
    tmp_path: Path, subscript: str
) -> None:
    source = f"from typing import TypeVar\nA = TypeVar('A', default=str)\nPair = tuple[A, int]\nT = Pair{subscript}\n"
    assert rejected(named(tmp_path, source)) == "invalid-row-type"


def test_an_empty_subscript_of_a_plain_generic_alias_takes_its_defaults(tmp_path: Path) -> None:
    source = "from typing import TypeVar\nA = TypeVar('A', default=str)\nPair = tuple[A, int]\nT = Pair[()]\n"
    assert leaves(named(tmp_path, source)) == [("str", False), ("int", False)]


def test_an_empty_subscript_of_a_plain_generic_alias_without_defaults_is_rejected(tmp_path: Path) -> None:
    source = "from typing import TypeVar\nA = TypeVar('A')\nPair = tuple[A, int]\nT = Pair[()]\n"
    assert rejected(named(tmp_path, source)) == "invalid-row-type"


def test_a_rejection_inside_a_substituted_field_names_the_class_and_the_arguments(tmp_path: Path) -> None:
    resolution = named(tmp_path, CRATE + "T = Crate[int | str]\n")
    assert rejected(resolution) == "invalid-row-type"
    diagnostic = resolution.diagnostic
    assert diagnostic is not None
    assert 'field "a" of `Crate[int | str]`' in diagnostic.message
    assert diagnostic.notes[-1] == f"`Crate` is generic, defined at {tmp_path / 'main.py'}:3:1, with X = int | str"


def alias_chain(length: int, body: str, final: str = "int") -> str:
    lines = [f"type A0[X] = {body}"] + [f"type A{i}[X] = A{i - 1}[X]" for i in range(1, length)]
    return "\n".join(lines) + f"\nT = A{length - 1}[{final}]\n"


def test_a_generic_alias_chain_is_followed_for_16_hops_and_rejected_at_17(tmp_path: Path) -> None:
    assert leaves(named(tmp_path, alias_chain(16, "tuple[X, str]"))) == [("int", False), ("str", False)]
    resolution = named(tmp_path / "deep", alias_chain(17, "tuple[X, str]"))
    assert rejected(resolution) == "invalid-row-type"
    assert "hops" in resolution.diagnostic.message  # ty: ignore[unresolved-attribute]


def test_a_class_at_the_end_of_an_alias_chain_is_one_more_hop(tmp_path: Path) -> None:
    assert pairs(named(tmp_path, CRATE + alias_chain(15, "Crate[X]"))) == [("a", False), ("b", False)]
    assert rejected(named(tmp_path / "deep", CRATE + alias_chain(16, "Crate[X]"))) == "invalid-row-type"


def test_a_plain_assignment_is_not_a_hop(tmp_path: Path) -> None:
    source = alias_chain(16, "tuple[X, str]").replace("T = A15[int]", "S = A15[int]\nT = S")
    assert len(leaves(named(tmp_path, source))) == 2


def test_generic_bases_are_followed_for_16_subscripted_bases_and_rejected_beyond(tmp_path: Path) -> None:
    def chain(length: int) -> str:
        lines = [DATACLASS, "@dataclass\nclass C0[X]:\n    a: X"]
        lines += [f"@dataclass\nclass C{i}[X](C{i - 1}[X]):\n    f{i}: X" for i in range(1, length)]
        return "\n".join(lines) + f"\nT = C{length - 1}[int]\n"

    assert len(row_type(named(tmp_path, chain(17))).fields) == 17
    assert rejected(named(tmp_path / "deep", chain(18))) == "invalid-row-type"


def test_the_whole_generic_base_chain_is_counted_even_when_only_the_row_class_has_fields(tmp_path: Path) -> None:
    def chain(length: int) -> str:
        lines = [DATACLASS, "@dataclass\nclass C0[X]:\n    pass"]
        lines += [
            f"@dataclass\nclass C{i}[X](C{i - 1}[X]):\n    {'f: X' if i == length else 'pass'}"
            for i in range(1, length + 1)
        ]
        return "\n".join(lines) + f"\nT = C{length}[int]\n"

    assert len(row_type(named(tmp_path, chain(16))).fields) == 1
    resolution = named(tmp_path / "deep", chain(20))
    assert rejected(resolution) == "invalid-row-type"
    assert resolution.diagnostic is not None
    assert "deeper than 16" in resolution.diagnostic.message


def test_a_generic_row_type_never_imports_the_user_module(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "boom.py": "raise RuntimeError('imported')\n",
            "main.py": "from boom import X\ntype Row[Y] = tuple[Y, str]\n" + QUERY.format(t="Row[int]"),
        },
    )
    assert len(leaves(resolve(tmp_path, "main.py"))) == 2
