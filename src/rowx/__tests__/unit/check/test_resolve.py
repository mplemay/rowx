from typing import TYPE_CHECKING

import pytest

from rowx.check.config import Config
from rowx.check.discover import discover_file
from rowx.check.resolve import ModuleGraph, resolve_row_type, roots_for

if TYPE_CHECKING:
    from pathlib import Path


def test_glob_include_uses_literal_prefix_for_roots(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    path = src / "queries.py"
    path.write_text("", encoding="utf-8")
    assert roots_for(Config(root=tmp_path, include=("src/**/*.py",)), path) == (src,)
    assert roots_for(Config(root=tmp_path, include=("**/*.py",)), path) == (tmp_path,)
    assert roots_for(Config(root=tmp_path, include=("src/queries.py",)), path) == (src,)


@pytest.mark.parametrize(
    "declaration",
    [
        "@attrs.define(field_transformer=transform)\nclass Row:\n    value: int",
        "@attrs.define(init=dynamic)\nclass Row:\n    value: int",
        "@attrs.define(kw_only=dynamic)\nclass Row:\n    value: int",
        "@attrs.define(**options)\nclass Row:\n    value: int",
        "@attrs.define(collect_by_mro=False)\nclass Row:\n    value: int",
        "@attrs.define\nclass Row:\n    value: int = attrs.field(init=dynamic)",
        "@attrs.define\nclass Row:\n    value: int = attrs.field(alias=dynamic)",
        "@attrs.define\nclass Row:\n    a: int = attrs.field(alias='same')\n    b: int = attrs.field(alias='same')",
        "@attrs.define\nclass Row:\n    value: int = attrs.field(default=dynamic)",
        "def convert(value): return value\n@attrs.define\nclass Row:\n    value: int = attrs.field(converter=convert)",
        "@attrs.define\nclass Row:\n    value: int\n    def __init__(self, value): self.value = value",
    ],
)
def test_attrs_rejects_dynamic_or_unsupported_declarations(tmp_path: Path, declaration: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(f'import attrs\n{declaration}\nfrom rowx import query\nquery[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


@pytest.mark.parametrize("reference", ["converters.convert", "convert", "alias"])
def test_attrs_resolves_imported_converter_without_executing_source(tmp_path: Path, reference: str) -> None:
    (tmp_path / "converters.py").write_text(
        'raise RuntimeError("must not execute")\ndef convert(value: int | None) -> str: return str(value)\n'
    )
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nimport converters\nfrom converters import convert\nfrom rowx import query\n"
        "alias = converters.convert\n"
        f"@attrs.define\nclass Row:\n    value: str = attrs.field(converter={reference})\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].elem.nullable
    assert result.row_type.fields[0].elem.leaf == "int"


def test_attrs_rejects_ambiguous_inherited_fields(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\n"
        "@attrs.define(slots=False)\nclass Left:\n    value: int\n"
        "@attrs.define(slots=False)\nclass Right:\n    value: int\n"
        "@attrs.define(slots=False)\nclass Row(Left, Right):\n    other: int\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"
