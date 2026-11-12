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
        "maybe_factory = int if dynamic else None\n@attrs.define\nclass Row:\n    value: int = attrs.field(factory=maybe_factory)",
        "def erase(fn): return None\n@erase\ndef make(): return 1\n@attrs.define\nclass Row:\n    value: int = attrs.field(factory=make)",
        "@attrs.define\nclass Row:\n    value: int\n    hidden = attrs.field(init=False, default=1)",
        "@attrs.define\nclass Base:\n    hidden = attrs.field()\n@attrs.define\nclass Row(Base):\n    value: int",
        "def convert(value): return value\n@attrs.define\nclass Row:\n    value: int = attrs.field(converter=convert)",
        "def erase(cls): return None\n@erase\nclass Factory: pass\n@attrs.define\nclass Row:\n    value: int = attrs.field(factory=Factory)",
        "class Meta(type):\n    def __new__(meta, name, bases, namespace): return None\nclass Factory(metaclass=Meta): pass\n@attrs.define\nclass Row:\n    value: int = attrs.field(factory=Factory)",
        "class Meta(type): pass\nclass Base(metaclass=Meta): pass\nclass Factory(Base): pass\n@attrs.define\nclass Row:\n    value: int = attrs.field(factory=Factory)",
        "import attr\n@attrs.define\nclass Row:\n    value: int = attr.ib()\n    hidden = attr.attrib()",
        "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    hidden = make_field()",
        "spec = attrs.field()\n@attrs.define\nclass Row:\n    value: int = attrs.field()\n    hidden = spec",
        "import attr\nfield_alias = attr.ib\n@attrs.define\nclass Base:\n    hidden = field_alias()\n@attrs.define\nclass Row(Base):\n    value: int",
        "import attr\n@attrs.define\nclass Row:\n    value: int\n    hidden = attr.ib(init=False)",
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


@pytest.mark.parametrize("reference", ["fields.spec", "spec", "alias"])
@pytest.mark.parametrize(
    "mutation", ["@mutated.default\ndef make(self): raise RuntimeError('must not execute')", "mutated.default(make)"]
)
def test_attrs_rejects_mutated_shared_field_without_executing_source(
    tmp_path: Path, reference: str, mutation: str
) -> None:
    (tmp_path / "fields.py").write_text("import attrs\nraise RuntimeError('must not execute')\nspec = attrs.field()\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nimport fields\nfrom fields import spec\nfrom rowx import query\n"
        "alias = spec\nmutated = fields.spec\ndef make(self): raise RuntimeError('must not execute')\n"
        f"@attrs.define\nclass Before:\n    value: int = {reference}\n"
        f"{mutation}\n"
        f"@attrs.define\nclass After:\n    value: int = {reference}\n"
        'query[Before](t"select 1")\nquery[After](t"select 1")\n'
    )
    calls = discover_file(path).calls
    assert len(calls) == 2
    for call in calls:
        result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
        assert result.row_type is None
        assert result.diagnostic is not None
        assert result.diagnostic.rule == "invalid-row-type"
        assert "post-construction default" in result.diagnostic.message


@pytest.mark.parametrize("excluded", [False, True])
def test_attrs_rejects_direct_mutated_shared_field(tmp_path: Path, *, excluded: bool) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nraise RuntimeError('must not execute')\n"
        f"spec = attrs.field(init={not excluded})\nother = spec\n"
        "@other.default\ndef make(self): raise RuntimeError('must not execute')\n"
        "@attrs.define\nclass Row:\n    id: int = attrs.field()\n    value: int = spec\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.row_type is None
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


def test_attrs_rejects_mutation_in_field_defining_module(tmp_path: Path) -> None:
    (tmp_path / "fields.py").write_text(
        "import attrs\nraise RuntimeError('must not execute')\nspec = attrs.field()\nother = spec\n"
        "@other.default\ndef make(self): raise RuntimeError('must not execute')\n"
    )
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom fields import spec\nfrom rowx import query\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.row_type is None
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


@pytest.mark.parametrize("reference", ["factories.make", "make", "alias", "frozenset", "factories.Factory"])
def test_attrs_resolves_factory_without_executing_source(tmp_path: Path, reference: str) -> None:
    (tmp_path / "factories.py").write_text(
        'raise RuntimeError("must not execute")\ndef make(): return 1\nclass Factory: pass\n'
    )
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nimport factories\nfrom factories import make\nfrom rowx import query\n"
        "alias = factories.make\n"
        f"@attrs.define\nclass Row:\n    value: int = attrs.field(factory={reference})\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert not result.row_type.fields[0].required


@pytest.mark.parametrize("reference", ["factories.Factory", "Factory", "alias"])
def test_attrs_rejects_imported_decorated_factory(tmp_path: Path, reference: str) -> None:
    (tmp_path / "factories.py").write_text("def erase(cls): return None\n@erase\nclass Factory: pass\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nimport factories\nfrom factories import Factory\nfrom rowx import query\n"
        "alias = Factory\n"
        f"@attrs.define\nclass Row:\n    value: int = attrs.field(factory={reference})\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


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
