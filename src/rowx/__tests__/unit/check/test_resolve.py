import sys
from textwrap import dedent
from typing import TYPE_CHECKING

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
