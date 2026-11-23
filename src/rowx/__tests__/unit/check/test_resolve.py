import ast
from typing import TYPE_CHECKING

import pytest

from rowx.check.config import Config
from rowx.check.discover import discover_file
from rowx.check.resolve import ModuleGraph, _Module, _Resolver, resolve_row_type, roots_for

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator
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
    "body",
    [
        "for alias in iter((value,)):\n        alias.default(lambda self: 1)",
        "for alias in tuple([value]):\n        if True:\n            copy = alias\n            copy.default(lambda self: 1)",
        "for alias in (value if True else attrs.field(),):\n        alias.default(lambda self: 1)",
        "with nullcontext(value) as alias:\n        copy = alias\n        copy.default(lambda self: 1)",
        "match value:\n        case alias:\n            alias.default(lambda self: 1)",
        "(alias := value)\n    match 1:\n        case 0: alias = attrs.field()\n        case 1: alias.default(lambda self: 1)",
        "(alias := value)\n    for _ in ():\n        alias = attrs.field()\n    else:\n        alias.default(lambda self: 1)",
        "(alias := value)\n    while False:\n        alias = attrs.field()\n    else:\n        alias.default(lambda self: 1)",
        "(alias := (other := value))\n    alias.default(lambda self: 1)",
    ],
)
def test_attrs_local_binding_paths_reject_registration(tmp_path: Path, body: str) -> None:
    path = tmp_path / "case.py"
    cleanup = "alias"
    if "copy = alias" in body:
        cleanup += ", copy"
    if "other := value" in body:
        cleanup += ", other"
    path.write_text(
        "import attrs\nfrom contextlib import nullcontext\nfrom rowx import query\n"
        f"@attrs.define\nclass Row:\n    value: int = attrs.field()\n    {body}\n    del {cleanup}\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


@pytest.mark.parametrize(
    "body",
    [
        "(alias := spec)\nalias, copy = attrs.field(), alias\ncopy.default(lambda self: 1)",
        "(alias := (other := spec))\nalias.default(lambda self: 1)",
        "(alias := spec)\nfor _ in ():\n    alias = attrs.field()\nelse:\n    alias.default(lambda self: 1)",
        "(alias := spec)\nwhile False:\n    alias = attrs.field()\nelse:\n    alias.default(lambda self: 1)",
        "(alias := spec)\nmatch 1:\n    case 0: alias = attrs.field()\n    case 1: alias.default(lambda self: 1)",
    ],
)
def test_attrs_shared_binding_paths_reject_registration(tmp_path: Path, body: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        f'{body}\n@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


@pytest.mark.parametrize(
    "body",
    [
        "with nullcontext(value) as alias:\n        alias = attrs.field()\n        alias.default(lambda self: 1)",
        "match value:\n        case alias:\n            alias = attrs.field()\n            alias.default(lambda self: 1)",
        "for alias in tuple([value]):\n        alias = attrs.field()\n        alias.default(lambda self: 1)",
        "for alias in (attrs.field(validator=lambda s, a, v: value),):\n        alias.default(lambda self: 1)",
        "match attrs.field():\n        case alias:\n            alias.default(lambda self: 1)",
        "match attr.field():\n        case alias:\n            alias.default(lambda self: 1)",
        "def later():\n        (alias := (other := value))\n        alias.default(lambda self: 1)",
    ],
)
def test_attrs_binding_paths_allow_fresh_and_deferred(tmp_path: Path, body: str) -> None:
    path = tmp_path / "case.py"
    cleanup = "" if body.startswith("def later") else "    del alias\n"
    path.write_text(
        "import attr\nimport attrs\nfrom contextlib import nullcontext\nfrom rowx import query\n"
        f"@attrs.define\nclass Row:\n    value: int = attrs.field()\n    {body}\n{cleanup}"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None


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


@pytest.mark.parametrize("reference", ["fields.spec", "spec", "alias"])
@pytest.mark.parametrize(
    "mutation",
    [
        "class Helper:\n    @spec.default\n    def make(self): raise RuntimeError('deferred')",
        "class Outer:\n    class Helper:\n        @spec.default\n        def make(self): raise RuntimeError('deferred')",
        "class Helper:\n    local = spec\n    local.default(lambda self: 1)",
        "def helper(value=spec.default(lambda self: 1)): raise RuntimeError('deferred')",
        "def helper(*, value=spec.default(lambda self: 1)): raise RuntimeError('deferred')",
        "@attrs.define\nclass Other:\n    value: int = spec\n    spec.default(lambda self: 1)",
    ],
)
def test_attrs_rejects_eager_shared_field_mutations(tmp_path: Path, reference: str, mutation: str) -> None:
    (tmp_path / "fields.py").write_text("import attrs\nraise RuntimeError('not executed')\nspec = attrs.field()\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nimport fields\nfrom fields import spec\nfrom rowx import query\n"
        "alias = fields.spec\nraise RuntimeError('not executed')\n"
        f"{mutation}\n@attrs.define\nclass Row:\n    value: int = {reference}\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.row_type is None
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


@pytest.mark.parametrize(
    "helper",
    [
        "class Helper:\n    spec = attrs.field()\n    @spec.default\n    def make(self): return 1",
        "def helper():\n    spec.default(lambda self: 1)",
        "helper = lambda: spec.default(lambda self: 1)",
    ],
)
def test_attrs_shared_field_ignores_shadow_and_deferred_body(tmp_path: Path, helper: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        f"{helper}\n@attrs.define\nclass Row:\n    value: int = spec\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize(
    ("body", "rejected"),
    [
        ("import fields as d\n    @d.spec.default\n    def make(self): return 1", True),
        ("d = fields\n    @d.spec.default\n    def make(self): return 1", True),
        ("local, = (spec,)\n    local.default(lambda self: 1)", True),
        ("(local := spec)\n    local.default(lambda self: 1)", True),
        ("if condition:\n        local = spec\n    local.default(lambda self: 1)", True),
        ("for item in ():\n        local = spec\n    local.default(lambda self: 1)", True),
        ("for local in (spec,):\n        local.default(lambda self: 1)", True),
        ("from fields import spec as local\n    @local.default\n    def make(self): return 1", True),
        ("spec = 0\n    del spec\n    @spec.default\n    def make(self): return 1", True),
        ("spec = attrs.field()\n    @spec.default\n    def make(self): return 1", False),
        ("spec, = (attrs.field(),)\n    @spec.default\n    def make(self): return 1", False),
        ("fields = object()\n    fields.spec.default(lambda self: 1)", False),
    ],
)
def test_attrs_shared_field_class_scope_bindings(tmp_path: Path, body: str, *, rejected: bool) -> None:
    (tmp_path / "fields.py").write_text("import attrs\nspec = attrs.field()\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nimport fields\nfrom fields import spec\nfrom rowx import query\n"
        f"class Helper:\n    {body}\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    if rejected:
        assert result.diagnostic is not None
        assert result.diagnostic.rule == "invalid-row-type"
    else:
        assert result.diagnostic is None
        assert result.row_type is not None
        assert result.row_type.fields[0].required


@pytest.mark.parametrize(
    "binding", ["(alias := spec)", "def helper(value=(alias := spec)): raise RuntimeError('not executed')"]
)
@pytest.mark.parametrize(
    "helper",
    [
        "class Defaults:\n    @alias.default\n    def make(self): return 1",
        "class Outer:\n    alias = attrs.field()\n    class Defaults:\n        @alias.default\n        def make(self): return 1",
    ],
)
def test_attrs_rejects_module_eager_namedexpr_alias(tmp_path: Path, binding: str, helper: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        f"{binding}\n{helper}\n@attrs.define\nclass Row:\n    value: int = spec\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


def test_attrs_module_namedexpr_alias_class_shadow(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n(alias := spec)\n"
        "class Defaults:\n    alias = attrs.field()\n    @alias.default\n    def make(self): return 1\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


def test_attrs_shared_field_class_submodule_import(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "fields.py").write_text("import attrs\nspec = attrs.field()\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom pkg.fields import spec\nfrom rowx import query\n"
        "class Helper:\n    from pkg import fields as d\n    @d.spec.default\n    def make(self): return 1\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
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


@pytest.mark.parametrize(
    "mutation",
    [
        "for alias in (spec,):\n    alias.default(lambda self: 1)",
        "if True:\n    alias, = (spec,)\nalias.default(lambda self: 1)",
        "if True:\n    alias, = (spec,)\n    class Helper:\n        alias.default(lambda self: 1)",
        "for alias in (spec,):\n    class Helper:\n        alias.default(lambda self: 1)",
        "class Helper:\n    for alias, in ((spec,),):\n        alias.default(lambda self: 1)",
        "class Helper:\n    for [alias] in ([spec],):\n        alias.default(lambda self: 1)",
        "unused = (x for x in (spec.default(lambda self: 1),))",
        "unused = [spec.default(lambda self: 1) for _ in ()]",
        "unused = [alias.default(lambda self: 1) for alias in (spec,)]",
        "unused = {alias.default(lambda self: 1) for alias, in ((spec,),)}",
        "unused = {0: alias.default(lambda self: 1) for alias in (spec,)}",
        "for alias in unknown():\n    alias.default(lambda self: 1)",
        "unused = [alias.default(lambda self: 1) for alias in unknown()]",
    ],
)
def test_attrs_shared_field_flow_mutation(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        f"{mutation}\n@attrs.define\nclass Row:\n    value: int = spec\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


def test_attrs_shared_field_unconsumed_generator(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        "unused = (spec.default(lambda self: 1) for _ in ())\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize(
    "hook",
    [
        "if True:\n        @value.default\n        def make(self): return 1",
        "if True:\n        if True:\n            @value.default\n            def make(self): return 1",
        "value.default(lambda self: 1)",
    ],
)
def test_attrs_rejects_unsupported_local_default_registration(tmp_path: Path, hook: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\n@attrs.define\nclass Row:\n"
        f'    value: int = attrs.field()\n    {hook}\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


def test_attrs_deferred_loop_and_local_generator(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        "for alias in unknown():\n    def later(): alias.default(lambda self: 1)\n"
        "@attrs.define\nclass Row:\n    value: int = spec\n"
        "    (value.default(lambda self: 1) for _ in ())\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize(
    "hook",
    [
        "@value.default\n    class Maker:\n        def __init__(self, row): pass",
        "@value.default(lambda self: 1)\n    def hook(): pass",
        "@identity(value.default(lambda self: 1))\n    def hook(self): pass",
        "@identity(value.default(lambda self: 1))\n    async def hook(self): pass",
        "def hook(self, arg=value.default(lambda self: 1)): pass",
    ],
)
def test_attrs_rejects_eager_local_header_registration(tmp_path: Path, hook: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\ndef identity(x): return lambda f: f\n"
        f"@attrs.define\nclass Row:\n    value: int = attrs.field()\n    {hook}\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


@pytest.mark.parametrize(
    "expression",
    [
        "[(alias := spec) for _ in (1,)]",
        "[[(alias := spec) for _ in (1,)] for _ in (1,)]",
        "[(alias := spec if flag else None) for _ in (1,)]",
        "[0 for _ in (1,) if (alias := spec)]",
    ],
)
def test_attrs_shared_field_comprehension_walrus(tmp_path: Path, expression: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        f"unused = {expression}\nalias.default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


def test_attrs_reexported_module_mutation_without_execution(tmp_path: Path) -> None:
    (tmp_path / "fields.py").write_text("import attrs\nraise RuntimeError('must not execute')\nspec = attrs.field()\n")
    (tmp_path / "exports.py").write_text("import fields\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nfrom exports import fields\n"
        "fields.spec.default(lambda self: 1)\n@attrs.define\nclass Row:\n    value: int = fields.spec\n"
        'query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


@pytest.mark.parametrize(("target", "iterable"), [("spec", "(attrs.field(),)"), ("(spec,)", "((attrs.field(),),)")])
def test_attrs_class_loop_fresh_field_shadows_shared(tmp_path: Path, target: str, iterable: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        f"class Helper:\n    for {target} in {iterable}:\n        spec.default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize(
    "body",
    [
        "for alias in (value,):\n        alias.default(lambda self: 1)",
        "for (alias,) in ((value,),):\n        alias.default(lambda self: 1)",
        "unused = [alias.default(lambda self: 1) for alias in (value,)]",
        "for alias in (value,):\n        copy = alias\n        copy.default(lambda self: 1)\n    del alias, copy",
    ],
)
@pytest.mark.parametrize("private", [False, True])
def test_attrs_local_field_loop_alias_registration(tmp_path: Path, body: str, *, private: bool) -> None:
    path = tmp_path / "case.py"
    name = "__value" if private else "value"
    alias = "__alias" if private else "alias"
    body = body.replace("value", name).replace("alias", alias)
    path.write_text(
        "import attrs\nfrom rowx import query\n@attrs.define\nclass Row:\n"
        f'    {name}: int = attrs.field()\n    {body}\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


@pytest.mark.parametrize("private", [False, True])
def test_attrs_local_loop_alias_shadow_is_unrelated(tmp_path: Path, *, private: bool) -> None:
    path = tmp_path / "case.py"
    source = (
        "import attrs\nfrom rowx import query\n@attrs.define\nclass Row:\n"
        "    value: int = attrs.field()\n"
        "    for alias in (value,):\n        for alias in (attrs.field(),):\n            alias.default(lambda self: 1)\n"
        '    del alias\nquery[Row](t"select 1")\n'
    )
    if private:
        source = source.replace("value", "__value").replace("alias", "__alias")
    path.write_text(source)
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize("private", [False, True])
def test_attrs_local_loop_copy_shadow_is_unrelated(tmp_path: Path, *, private: bool) -> None:
    path = tmp_path / "case.py"
    source = (
        "import attrs\nfrom rowx import query\n@attrs.define\nclass Row:\n"
        "    value: int = attrs.field()\n"
        "    for alias in (value,):\n        copy = alias\n        copy = attrs.field()\n"
        "        copy.default(lambda self: 1)\n    del alias, copy\nquery[Row](t'select 1')\n"
    )
    if private:
        source = source.replace("value", "__value").replace("alias", "__alias").replace("copy", "__copy")
    path.write_text(source)
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize(
    "expression",
    [
        "[spec.default(lambda self: 1) for _ in (1,)]",
        "{spec.default(lambda self: 1) for _ in (1,)}",
        "{0: spec.default(lambda self: 1) for _ in (1,)}",
        "[[alias.default(lambda self: 1) for _ in (1,)] for alias in (global_spec,)]",
    ],
)
def test_attrs_class_comprehension_uses_global_scope(tmp_path: Path, expression: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\nglobal_spec = spec\n"
        f"class Helper:\n    spec = attrs.field()\n    unused = {expression}\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


def test_attrs_class_comprehension_fresh_global_and_eager_iterable(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\nfresh = attrs.field()\n"
        "class Helper:\n    fresh = spec\n    unused = [fresh.default(lambda self: 1) for _ in (1,)]\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize(
    "body",
    [
        "for alias in (value,):\n        if True:\n            copy = alias\n            copy.default(lambda self: 1)\n    del alias, copy",
        "(alias := value)\n    alias.default(lambda self: 1)\n    del alias",
        "(alias := value)\n    alias, copy = attrs.field(), alias\n    copy.default(lambda self: 1)\n    del alias, copy",
        "(alias := value)\n    while flag:\n        alias = attrs.field()\n    alias.default(lambda self: 1)\n    del alias",
        "(alias := value if flag else attrs.field())\n    alias.default(lambda self: 1)\n    del alias",
        "for alias in (value,):\n        if True:\n            copy = alias\n        copy.default(lambda self: 1)\n    del alias, copy",
    ],
)
def test_attrs_local_conditional_and_walrus_alias_registration(tmp_path: Path, body: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\n@attrs.define\nclass Row:\n"
        f'    value: int = attrs.field()\n    {body}\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


def test_attrs_local_walrus_fresh_shadow_and_deferred(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\n@attrs.define\nclass Row:\n"
        "    value: int = attrs.field()\n    (alias := attrs.field())\n    alias.default(lambda self: 1)\n"
        "    (lambda: (other := value))\n    (value.default(lambda self: 1) for _ in ())\n"
        '    del alias\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


def test_attrs_class_comprehension_mangles_global_name(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\n_Helper__spec = attrs.field()\n"
        "class Helper:\n    __spec = attrs.field()\n    unused = [__spec.default(lambda self: 1) for _ in (1,)]\n"
        '@attrs.define\nclass Row:\n    value: int = _Helper__spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


def test_attrs_class_comprehension_first_iterable_uses_class_scope(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        "class Helper:\n    spec = attrs.field()\n    unused = [alias.default(lambda self: 1) for alias in (spec,)]\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


def test_attrs_local_conditional_fresh_rebinding(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\n@attrs.define\nclass Row:\n"
        "    value: int = attrs.field()\n    (alias := value)\n"
        "    if True:\n        alias = attrs.field()\n    else:\n        alias = attrs.field()\n"
        "    alias.default(lambda self: 1)\n    del alias\nquery[Row](t'select 1')\n"
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize("private", [False, True])
def test_attrs_class_comprehension_qualified_private_attribute(tmp_path: Path, *, private: bool) -> None:
    field = "_Helper__spec" if private else "spec"
    use = "__spec" if private else "spec"
    (tmp_path / "fields.py").write_text(f"import attrs\n{field} = attrs.field()\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nimport fields\nfrom rowx import query\n"
        f"class Helper:\n    unused = [fields.{use}.default(lambda self: 1) for _ in (1,)]\n"
        f'@attrs.define\nclass Row:\n    value: int = fields.{field}\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "(alias := attrs.field())\nfor _ in (0, 1):\n    alias.default(lambda self: 1)\n    alias = FIELD",
        "(alias := attrs.field())\ni = 0\nwhile i < 2:\n    alias.default(lambda self: 1)\n    alias = FIELD\n    i += 1",
        "for alias in (next(iter((FIELD,))),):\n    alias.default(lambda self: 1)",
        "for alias in (next(iter((FIELD,))) if True else attrs.field(),):\n    copy = alias\n    copy.default(lambda self: 1)",
        "for (alias,) in ((next(iter((FIELD,))),),):\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    alias = FIELD\n    raise ValueError()\n    (alias := attrs.field())\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\nmatch 1:\n    case 1 if (alias := FIELD) and False: pass\n    case _: alias.default(lambda self: 1)",
        "(alias := attrs.field())\nmatch 1:\n    case 1 if (alias := FIELD) and False: pass\nalias.default(lambda self: 1)",
        "(FIELD,)[0].default(lambda self: 1)",
        "for alias in ((FIELD,)[0],):\n    alias.default(lambda self: 1)",
        "(alias := (False or FIELD))\nalias.default(lambda self: 1)",
        "for _ in (0,):\n    (alias,) = iter((FIELD,))\n    alias.default(lambda self: 1)",
        "(alias := FIELD)\nNone if True else (alias := attrs.field())\nalias.default(lambda self: 1)",
        "for _ in (0,):\n    first, alias = (*(), attrs.field(), FIELD)\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    raise ExceptionGroup('mixed', [ValueError(), TypeError()])\nexcept* ValueError:\n    alias = FIELD\nexcept* TypeError:\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    ((alias := FIELD), int('bad'), (alias := attrs.field()))\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    print((alias := FIELD), int('bad'), (alias := attrs.field()))\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    print(((alias := FIELD), int('bad'), (alias := attrs.field())))\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "first, alias = (*(*(), attrs.field()), FIELD)\nalias.default(lambda self: 1)",
        "(*[FIELD, FIELD], attrs.field())[1].default(lambda self: 1)",
        "(FIELD,)[-1].default(lambda self: 1)",
        "(gate := FIELD)\n(alias := (gate, (gate := attrs.field()))[0])\nalias.default(lambda self: 1)",
        "(gate := attrs.field())\nalias = ((gate := FIELD), gate, (gate := attrs.field()))[1]\nalias.default(lambda self: 1)",
        "(gate := attrs.field())\n((gate := FIELD), gate, (gate := attrs.field()))[1].default(lambda self: 1)",
        "(gate := FIELD)\nfor alias in ((gate, (gate := attrs.field()))[0],):\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    if True:\n        ((alias := FIELD), int('bad'), (alias := attrs.field()))\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    if True:\n        alias = FIELD\n        int('bad')\n        alias = attrs.field()\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(gate := FIELD)\nalias, other = (gate, (gate := attrs.field()))\nalias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    for field_alias in (FIELD,):\n        alias = field_alias\n        int('bad')\n        alias = attrs.field()\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    for field_alias in (FIELD,):\n        alias = field_alias\n        int('bad')\n        alias = attrs.field()\nfinally:\n    alias.default(lambda self: 1)",
        "(gate := FIELD)\n(alias := (gate, (gate := attrs.field()))[index])\nalias.default(lambda self: 1)",
        "first, *rest = (attrs.field(), FIELD)\nalias = rest[0]\nalias.default(lambda self: 1)",
        "(gate := FIELD)\n(alias := attrs.field())\ntry:\n    ((alias := (gate, (gate := attrs.field()))[0]), int('bad'), (alias := attrs.field()))\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := FIELD)\ntry:\n    pass\nfinally:\n    [0, 0][alias is FIELD] = 1\nalias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    pass\nfinally:\n    other = (alias := FIELD)\nalias.default(lambda self: 1)",
        "(alias := attrs.field())\nwith suppress(ValueError):\n    try:\n        (alias := FIELD)\n        int('bad')\n        (alias := attrs.field())\n    finally:\n        pass\nalias.default(lambda self: 1)",
    ],
)
def test_attrs_flow_paths_reject_dynamic_default(tmp_path: Path, body: str, *, shared: bool) -> None:
    path = tmp_path / "case.py"
    if shared:
        source = (
            "spec = attrs.field()\n"
            + body.replace("FIELD", "spec")
            + "\n@attrs.define\nclass Row:\n    value: int = spec\n"
        )
    else:
        if (
            body.startswith(("first, alias =", "first, *rest ="))
            or "\nalias = ((gate :=" in body
            or "\nalias, other =" in body
        ):
            body = "for _ in (0,):\n    " + body.replace("\n", "\n    ")
        statements = body.replace("FIELD", "value").replace("\n", "\n    ")
        source = f"@attrs.define\nclass Row:\n    value: int = attrs.field()\n    {statements}\n"
    path.write_text(
        f'import attrs\nfrom contextlib import suppress\nfrom rowx import query\nindex = 0\n{source}query[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"
    assert "default" in result.diagnostic.message


@pytest.mark.parametrize(
    ("body", "rejected"),
    [
        (
            "alias = attrs.field()\ntry:\n    class Inner:\n        global alias\n        alias = spec\n        int('bad')\nexcept ValueError:\n    alias.default(lambda self: 1)",
            True,
        ),
        (
            "alias = attrs.field()\ntry:\n    class Inner:\n        alias = spec\n        int('bad')\nexcept ValueError:\n    alias.default(lambda self: 1)",
            False,
        ),
        ("first, *rest = (attrs.field(), spec, attrs.field())\nalias = rest[-1]\nalias.default(lambda self: 1)", False),
        (
            "first, *rest = (attrs.field(), attrs.field())\nrest = list((spec,))\nalias = rest[0]\nalias.default(lambda self: 1)",
            True,
        ),
        (
            "first, *rest = (attrs.field(), attrs.field())\nif flag:\n    rest = list((spec,))\nalias = rest[0]\nalias.default(lambda self: 1)",
            True,
        ),
        (
            "(alias := spec)\nwith nullcontext():\n    try:\n        (alias := attrs.field())\n    finally:\n        pass\nalias.default(lambda self: 1)",
            False,
        ),
        (
            "(alias := spec)\ntry:\n    (alias := attrs.field())\nfinally:\n    if flag:\n        alias = alias\nalias.default(lambda self: 1)",
            False,
        ),
    ],
)
def test_attrs_shared_cleanup_and_nested_class_flow(tmp_path: Path, body: str, *, rejected: bool) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom contextlib import nullcontext\nfrom rowx import query\n"
        f"flag = True\nspec = attrs.field()\n{body}\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert (result.diagnostic is not None) == rejected
    if rejected:
        assert result.diagnostic is not None
        assert result.diagnostic.rule == "invalid-row-type"
        assert "default" in result.diagnostic.message


def test_attrs_finally_import_binding(tmp_path: Path) -> None:
    (tmp_path / "fields.py").write_text("import attrs\nspec = attrs.field()\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom fields import spec\nfrom rowx import query\n"
        "alias = attrs.field()\ntry:\n    pass\nfinally:\n    from fields import spec as alias\n"
        "alias.default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"
    assert "default" in result.diagnostic.message


def test_attrs_conditional_finally_import_binding(tmp_path: Path) -> None:
    (tmp_path / "fields.py").write_text("import attrs\nspec = attrs.field()\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom fields import spec\nfrom rowx import query\n"
        "alias = attrs.field()\ntry:\n    pass\nfinally:\n    if True:\n        from fields import spec as alias\n"
        "alias.default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert result.diagnostic.rule == "invalid-row-type"
    assert "default" in result.diagnostic.message


def test_attrs_nested_finally_traversal_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ast.iter_child_nodes
    visits = 0

    def counted(node: ast.AST) -> Iterator[ast.AST]:
        nonlocal visits
        visits += 1
        yield from original(node)

    monkeypatch.setattr(ast, "iter_child_nodes", counted)

    def measure(depth: int) -> int:
        nonlocal visits
        body = "pass"
        for _ in range(depth):
            body = "try:\n    " + body.replace("\n", "\n    ") + "\nfinally:\n    alias = alias"
        path = tmp_path / "case.py"
        path.write_text(
            "import attrs\nfrom rowx import query\nspec = attrs.field()\nalias = attrs.field()\n"
            f'{body}\n@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
        )
        [call] = discover_file(path).calls
        visits = 0
        result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
        assert result.diagnostic is None
        return visits

    shallow, deep = measure(6), measure(12)
    assert deep < shallow * 5


@pytest.mark.parametrize("shared", [False, True])
def test_attrs_failed_guard_without_registration_is_supported(tmp_path: Path, *, shared: bool) -> None:
    path = tmp_path / "case.py"
    body = "(alias := attrs.field())\nmatch 1:\n    case 1 if (alias := FIELD) and False: pass\n    case _: pass\n"
    if shared:
        source = (
            "spec = attrs.field()\n"
            + body.replace("FIELD", "spec")
            + "@attrs.define\nclass Row:\n    value: int = spec\n"
        )
    else:
        source = "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    " + body.replace(
            "FIELD", "value"
        ).replace("\n", "\n    ")
        source += "del alias\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "(gate := FIELD)\n(alias := ((gate := attrs.field()), gate)[1])\nalias.default(lambda self: 1)",
        "(gate := FIELD)\nfor alias in (((gate := attrs.field()), gate)[1],):\n    alias.default(lambda self: 1)",
        "(gate := FIELD)\nalias = ((gate := attrs.field()), gate)[1]\nalias.default(lambda self: 1)",
        "(FIELD, attrs.field())[-1].default(lambda self: 1)",
        "first, alias = (*(*(), FIELD), attrs.field())\nalias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    if False:\n        (alias := FIELD)\n    int('bad')\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    if True:\n        (lambda: (alias := FIELD))\n        int('bad')\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    while False:\n        (alias := FIELD)\n    int('bad')\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(gate := FIELD)\n(alias := attrs.field())\ntry:\n    for gate in (attrs.field(),):\n        alias = gate\n        int('bad')\nexcept ValueError:\n    alias.default(lambda self: 1)",
        "(alias := FIELD)\ntry:\n    alias = attrs.field()\nfinally:\n    pass\nalias.default(lambda self: 1)",
        "(alias := FIELD)\ntry:\n    pass\nfinally:\n    (alias := attrs.field())\nalias.default(lambda self: 1)",
        "(alias := attrs.field())\ntry:\n    (alias := FIELD)\n    int('bad')\n    (alias := attrs.field())\nfinally:\n    def later():\n        alias = FIELD\nalias.default(lambda self: 1)",
        "(alias := (attrs.field(), attrs.field())[int('0')])\nalias.default(lambda self: 1)",
        "(alias := FIELD)\ntry:\n    (alias := attrs.field())\nfinally:\n    alias = alias\nalias.default(lambda self: 1)",
    ],
)
def test_attrs_literal_selection_and_try_fresh_controls(tmp_path: Path, body: str, *, shared: bool) -> None:
    path = tmp_path / "case.py"
    if shared:
        source = (
            "spec = attrs.field()\n"
            + body.replace("FIELD", "spec")
            + "\n@attrs.define\nclass Row:\n    value: int = spec\n"
        )
    else:
        if body.startswith("first, alias =") or "\nalias = ((gate :=" in body:
            body = "for _ in (0,):\n    " + body.replace("\n", "\n    ")
        statements = body.replace("FIELD", "value").replace("\n", "\n    ")
        source = f"@attrs.define\nclass Row:\n    value: int = attrs.field()\n    {statements}\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None


@pytest.mark.parametrize(
    "expression",
    [
        "(1 if True else (alias := spec))",
        "(True or (alias := spec))",
        "(lambda: (alias := spec))",
        "((alias := spec) for _ in ())",
        "((alias := attrs.field()), int('bad'))",
    ],
)
def test_attrs_try_eager_binding_fresh_controls(tmp_path: Path, expression: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\nalias = attrs.field()\n"
        f"try:\n    {expression}\nexcept ValueError:\n    alias.default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("", False),
        ("def object(): return spec\n", True),
        ("from helpers import object\n", True),
        ("def unrelated():\n    object = 1\n", False),
        ("(object := lambda: spec)\n", True),
    ],
)
def test_attrs_builtin_object_shadow(tmp_path: Path, setup: str, *, expected: bool) -> None:
    (tmp_path / "helpers.py").write_text("def object(): return None\n")
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        f"{setup}for alias in (object(),):\n    alias.default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert (result.diagnostic is not None) == expected


def test_attrs_ifexp_keeps_fresh_class_shadow(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\nalias = spec\n"
        "class Helper:\n    alias = attrs.field()\n    1 if flag else 2\n    alias.default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    ("body", "rejected"),
    [
        ("first, *rest = (attrs.field(), FIELD)\nrest[0].default(lambda self: 1)", True),
        ("items = (attrs.field(), FIELD)\nitems[-1].default(lambda self: 1)", True),
        ("items = [attrs.field()]\nitems[0] = FIELD\nitems[0].default(lambda self: 1)", True),
        ("items = [attrs.field()]\nitems.append(FIELD)\nitems[-1].default(lambda self: 1)", True),
        ("items = [attrs.field()]\n(copy := items)\ncopy.append(FIELD)\nitems[-1].default(lambda self: 1)", True),
        ("items = [attrs.field()]\ncopy, = (items,)\ncopy.append(FIELD)\nitems[-1].default(lambda self: 1)", True),
        (
            "items = [FIELD, attrs.field()]\ncopy = items\nitems[1] = attrs.field()\ncopy[-1].default(lambda self: 1)",
            False,
        ),
        ("items = [FIELD]\nitems.clear()\nitems.append(attrs.field())\nitems[0].default(lambda self: 1)", False),
        (
            "items = [FIELD]\ncopy = items\nitems = [attrs.field()]\ncopy.append(FIELD)\nitems[0].default(lambda self: 1)",
            False,
        ),
        ("items = [attrs.field()]\nitems += [FIELD]\nitems[-1].default(lambda self: 1)", True),
        ("items = [attrs.field()]\n(items[0],) = (FIELD,)\nitems[0].default(lambda self: 1)", True),
        ("items = [attrs.field()]\nitems.__setitem__(0, FIELD)\nitems[0].default(lambda self: 1)", True),
        ("items = [attrs.field()]\nitems.extend([FIELD])\nitems[-1].default(lambda self: 1)", True),
        ("items = [attrs.field()]\nitems[:] = [FIELD]\nitems[0].default(lambda self: 1)", True),
        (
            "items = [attrs.field()]\na = items\nb = items\nitems = []\na.append(FIELD)\nb[-1].default(lambda self: 1)",
            True,
        ),
        (
            "items = [attrs.field()]\nmid = items\nother = mid\nmid = []\nitems.append(FIELD)\nother[-1].default(lambda self: 1)",
            True,
        ),
        (
            "items = [attrs.field()]\ngate = FIELD\nitems[0] = (gate, (gate := attrs.field()))[0]\nitems[0].default(lambda self: 1)",
            True,
        ),
        ("items = [FIELD, attrs.field()]\nitems.append(attrs.field())\nitems[1].default(lambda self: 1)", False),
        (
            "items = [FIELD]\nsource = [attrs.field(), FIELD]\nitems[:] = source\nitems[0].default(lambda self: 1)",
            False,
        ),
        ("items = [FIELD]\nsource = [attrs.field(), FIELD]\nitems[:] = source\nitems[1].default(lambda self: 1)", True),
        ("items = [FIELD]\nsource = [attrs.field()]\nitems[:] = [*source]\nitems[0].default(lambda self: 1)", False),
        (
            "items = [FIELD, attrs.field()]\nitems.__setitem__(slice(None), [attrs.field(), FIELD])\nitems[0].default(lambda self: 1)",
            False,
        ),
        (
            "items = [FIELD, attrs.field()]\nitems.__setitem__(slice(None), [attrs.field(), FIELD])\nitems[1].default(lambda self: 1)",
            True,
        ),
        ("*items, = (attrs.field(),)\na = items\nitems += [FIELD]\na[-1].default(lambda self: 1)", True),
        ("items = [[FIELD, attrs.field()]]\nitems[0][0].default(lambda self: 1)", True),
        ("items = [[FIELD, attrs.field()]]\nitems[0][1].default(lambda self: 1)", False),
        (
            "items = [[attrs.field(), attrs.field()]]\na = items[0]\na[0] = FIELD\nitems[0][0].default(lambda self: 1)",
            True,
        ),
        (
            "items = [FIELD]\nold = items\nold, old[0] = [attrs.field()], attrs.field()\nitems[0].default(lambda self: 1)",
            True,
        ),
        (
            "items = [attrs.field()]\nold = items\nold, old[0] = [attrs.field()], FIELD\nold[0].default(lambda self: 1)",
            True,
        ),
        ("items = [attrs.field()]\ncopy = items\ncopy.append(FIELD)\nitems[-1].default(lambda self: 1)", True),
        ("items = (*(), attrs.field(), FIELD)\nitems[1].default(lambda self: 1)", True),
        ("items = (*(), FIELD, attrs.field())\nitems[1].default(lambda self: 1)", False),
        ("items = [FIELD, attrs.field()]\nitems[1] = attrs.field()\nitems[1].default(lambda self: 1)", False),
        ("items = (FIELD, attrs.field())\nitems[-1].default(lambda self: 1)", False),
        ("items = [attrs.field()]\nitems[0] = attrs.field()\nitems[0].default(lambda self: 1)", False),
        (
            "items = [attrs.field()]\na = items\nb = a\nitems = items\na.append(FIELD)\nb[-1].default(lambda self: 1)",
            True,
        ),
        ("items = [attrs.field()]\na = items\nb = a\ndel a\nb.append(FIELD)\nitems[-1].default(lambda self: 1)", True),
        ("items = [FIELD, attrs.field()]\ndel items[0]\nitems[0].default(lambda self: 1)", False),
        ("items = [attrs.field(), FIELD]\ndel items[0]\nitems[0].default(lambda self: 1)", True),
        (
            "items = [attrs.field()]\nold = items\nitems.append(((items := []), FIELD)[1])\nold[-1].default(lambda self: 1)",
            True,
        ),
        (
            "items = [FIELD]\nold = items\nitems.append(((items := []), attrs.field())[1])\nold[-1].default(lambda self: 1)",
            False,
        ),
        ("items = (attrs.field(),)\na = items\nitems += (FIELD,)\na[0].default(lambda self: 1)", False),
        (
            "items = [attrs.field()]\nold = items\nitems += ((items := [attrs.field()]), [FIELD])[1]\nold[-1].default(lambda self: 1)",
            True,
        ),
        (
            "items = [FIELD]\nold = items\nitems += ((items := []), [attrs.field()])[1]\nitems[0].default(lambda self: 1)",
            True,
        ),
        ("items = (FIELD,)\nitems += (attrs.field(),)\nitems[1].default(lambda self: 1)", False),
        ("items = [FIELD]\nitems += [attrs.field()]\nitems[1].default(lambda self: 1)", False),
        ("items = [FIELD, attrs.field()]\nitems.__setitem__(0, FIELD)\nitems[1].default(lambda self: 1)", False),
        ("items = [attrs.field()]\nsource = [FIELD]\nitems[:] = [*source]\nitems[0].default(lambda self: 1)", True),
        ("items = (attrs.field(),)\na = items\nitems += (FIELD,)\nitems[-1].default(lambda self: 1)", True),
        (
            "items = [FIELD, attrs.field()]\nitems.append(attrs.field())\nitems[-2] = attrs.field()\nitems[0].default(lambda self: 1)",
            True,
        ),
        ("items = [attrs.field()]\nitems.extend({FIELD})\nitems[-1].default(lambda self: 1)", True),
        ("items = [attrs.field()]\nitems.extend({FIELD: 1})\nitems[-1].default(lambda self: 1)", True),
        ("items = [attrs.field()]\nitems.extend({attrs.field(): FIELD})\nitems[-1].default(lambda self: 1)", False),
        ("items = [attrs.field()]\nitems.extend({**{FIELD: 1}})\nitems[-1].default(lambda self: 1)", True),
        ("items = [attrs.field()]\nitems.append((items.clear(), FIELD)[1])\nitems[0].default(lambda self: 1)", True),
        ("items = [FIELD]\n(items.clear(), items)[1].append(attrs.field())\nitems[0].default(lambda self: 1)", False),
        (
            "items = [attrs.field(), attrs.field()]\nitems[0], items[1] = FIELD, attrs.field()\nitems[1].default(lambda self: 1)",
            False,
        ),
        (
            "items = [attrs.field(), attrs.field()]\n(items[0], (items[1],)) = (FIELD, (attrs.field(),))\nitems[1].default(lambda self: 1)",
            False,
        ),
        (
            "items = [attrs.field(), attrs.field()]\nitems[:] = [attrs.field(), FIELD]\nitems[0].default(lambda self: 1)",
            False,
        ),
        (
            "items = [attrs.field(), attrs.field()]\nitems[::-1] = [attrs.field(), FIELD]\nitems[0].default(lambda self: 1)",
            True,
        ),
        (
            "items = [FIELD, attrs.field()]\ngate = attrs.field()\nitems[:] = [(gate := FIELD), gate]\nitems[1].default(lambda self: 1)",
            True,
        ),
        (
            "items = [attrs.field()]\nold = items\na, items[0] = old, FIELD\na[0].default(lambda self: 1)",
            True,
        ),
        (
            "items = [FIELD]\nold = items\nsource = ([attrs.field()], attrs.field())\nold, old[0] = source\nitems[0].default(lambda self: 1)",
            True,
        ),
        (
            "items = [attrs.field()]\nold = items\nsource = ([attrs.field()], FIELD)\nold, old[0] = source\nold[0].default(lambda self: 1)",
            True,
        ),
        ("items = [[FIELD]]\nitems[0][0] = attrs.field()\nitems[0][0].default(lambda self: 1)", False),
        ("items = [[FIELD]]\nitems[0].append(attrs.field())\nitems[0][1].default(lambda self: 1)", False),
        ("items = [[attrs.field()]]\nitems[0].append(FIELD)\nitems[0][1].default(lambda self: 1)", True),
        ("items = [[FIELD]]\nitems.append([attrs.field()])\nitems[0][0].default(lambda self: 1)", True),
        (
            "items = [FIELD]\nold = items\nsource = ([attrs.field()], attrs.field())\nold, old[0] = source\nold[0].default(lambda self: 1)",
            False,
        ),
    ],
)
def test_attrs_container_mutation_and_direct_receiver(
    tmp_path: Path, body: str, *, shared: bool, rejected: bool
) -> None:
    path = tmp_path / "case.py"
    if shared:
        source = (
            "spec = attrs.field()\n"
            + body.replace("FIELD", "spec")
            + "\n@attrs.define\nclass Row:\n    value: int = spec\n"
        )
    else:
        statements = body.replace("FIELD", "value").replace("\n", "\n        ")
        source = (
            f"@attrs.define\nclass Row:\n    value: int = attrs.field()\n    for _ in (0,):\n        {statements}\n"
        )
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert (result.diagnostic is not None) == rejected
    if rejected:
        assert result.diagnostic is not None
        assert "default" in result.diagnostic.message or "untraceable attrs collection" in result.diagnostic.message


def test_attrs_annotated_carrier_alias_mutation(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\n"
        "items = [attrs.field()]\ncopy: list = items\ncopy.append(spec)\n"
        "items[-1].default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert "default" in result.diagnostic.message


def test_attrs_global_declaration_nested_in_class_branch(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\nspec = attrs.field()\nalias = attrs.field()\n"
        "try:\n    class Inner:\n        if False:\n            global alias\n"
        "        alias = spec\n        int('bad')\nexcept ValueError:\n    alias.default(lambda self: 1)\n"
        '@attrs.define\nclass Row:\n    value: int = spec\nquery[Row](t"select 1")\n'
    )
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert "default" in result.diagnostic.message


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "(alias := FIELD)\nflag and (alias := attrs.field())\nalias.default(lambda self: 1)",
        "head, *middle, alias = (0, 1, FIELD)\nalias.default(lambda self: 1)",
        "for head, *middle, alias in ((0, 1, FIELD),):\n    alias.default(lambda self: 1)",
    ],
)
def test_attrs_short_circuit_and_starred_alias_reject_registration(tmp_path: Path, body: str, *, shared: bool) -> None:
    path = tmp_path / "case.py"
    if shared:
        source = (
            "spec = attrs.field()\n"
            + body.replace("FIELD", "spec")
            + "\n@attrs.define\nclass Row:\n    value: int = spec\n"
        )
    else:
        statements = body.replace("FIELD", "value").replace("\n", "\n    ")
        source = f"@attrs.define\nclass Row:\n    value: int = attrs.field()\n    {statements}\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert "default" in result.diagnostic.message or (
        not shared and "untraceable attrs collection" in result.diagnostic.message
    )


@pytest.mark.parametrize("shared", [False, True])
def test_attrs_while_header_rechecks_loop_carried_alias(tmp_path: Path, *, shared: bool) -> None:
    path = tmp_path / "case.py"
    body = "(gate := attrs.field())\ni = 0\nwhile (alias := gate) and i < 2:\n    alias.default(lambda self: 42)\n    gate = FIELD\n    i += 1\n"
    if shared:
        source = (
            "spec = attrs.field()\n"
            + body.replace("FIELD", "spec")
            + "@attrs.define\nclass Row:\n    value: int = spec\n"
        )
    else:
        source = (
            "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    "
            + body.replace("FIELD", "value").replace("\n", "\n    ")
            + "del gate, alias, i\n"
        )
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert "default" in result.diagnostic.message


@pytest.mark.parametrize(
    ("shared_loop"),
    [(shared, loop) for shared in (False, True) for loop in ("for _ in range(2)", "while gate")],
)
@pytest.mark.parametrize("operation", ["items.append(FIELD)", "items += [FIELD]", "items.append(attrs.field())"])
@pytest.mark.parametrize(
    "initial_registration",
    [(initial, registration) for initial in ("FIELD", "attrs.field()") for registration in (False, True)],
)
def test_attrs_loop_carrier_growth_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_loop: tuple[bool, str],
    operation: str,
    initial_registration: tuple[str, bool],
) -> None:
    shared, loop = shared_loop
    initial, registration = initial_registration
    path = tmp_path / "case.py"
    body = f"items = [{initial}]\ngate = True\n{loop}:\n    {operation}\n"
    if registration:
        index = 0 if initial == "FIELD" else -1
        body += f"items[{index}].default(lambda self: 42)\n"
    if shared:
        source = "spec = attrs.field()\n" + body.replace("FIELD", "spec")
        source += "@attrs.define\nclass Row:\n    value: int = spec\n"
    else:
        source = "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    for outer in (0,):\n        "
        source += body.replace("FIELD", "value").replace("\n", "\n        ") + "del items, gate\n    del outer\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    visits = 0
    original = _Resolver._attrs_carrier_tracked

    def count(carrier: str, scope: Collection[str]) -> bool:
        nonlocal visits
        visits += 1
        assert visits < 100
        return original(carrier, scope)

    monkeypatch.setattr(_Resolver, "_attrs_carrier_tracked", staticmethod(count))
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    if registration and (initial == "FIELD" or "FIELD" in operation):
        assert result.diagnostic is not None
        assert "default" in result.diagnostic.message
    else:
        assert result.diagnostic is None


@pytest.mark.parametrize("shared", [False, True])
def test_attrs_literal_receiver_survives_later_rebinding(tmp_path: Path, *, shared: bool) -> None:
    path = tmp_path / "case.py"
    body = (
        "items = [attrs.field()]\nold = items\n"
        "(items, (items := []))[0].append(FIELD)\nold[-1].default(lambda self: 42)\n"
    )
    if shared:
        source = "spec = attrs.field()\n" + body.replace("FIELD", "spec")
        source += "@attrs.define\nclass Row:\n    value: int = spec\n"
    else:
        source = "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    for outer in (0,):\n        "
        source += body.replace("FIELD", "value").replace("\n", "\n        ") + "del items, old\n    del outer\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert "default" in result.diagnostic.message


@pytest.mark.parametrize("shared", [False, True])
def test_attrs_for_iterable_bindings_are_frozen(tmp_path: Path, *, shared: bool) -> None:
    path = tmp_path / "case.py"
    body = "(gate := attrs.field())\nfor alias in (gate,):\n    alias.default(lambda self: 42)\n    gate = FIELD\n"
    if shared:
        source = (
            "spec = attrs.field()\n"
            + body.replace("FIELD", "spec")
            + "@attrs.define\nclass Row:\n    value: int = spec\n"
        )
    else:
        source = (
            "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    "
            + body.replace("FIELD", "value").replace("\n", "\n    ")
            + "del gate, alias\n"
        )
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize("shared", [False, True])
def test_attrs_nested_finally_visits_bodies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, shared: bool
) -> None:
    path = tmp_path / "case.py"
    depth = 14
    body = "".join(
        "    " * i + "try:\n" + "    " * (i + 1) + "pass\n" + "    " * i + "finally:\n" for i in range(depth)
    )
    body += "    " * depth + "987654321\n"
    if shared:
        source = "spec = attrs.field()\n" + body + "@attrs.define\nclass Row:\n    value: int = spec\n"
    else:
        source = "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    " + body.replace("\n", "\n    ")
        source += "pass\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    visits = 0
    original = ast.iter_child_nodes

    def count(node: ast.AST) -> Iterator[ast.AST]:
        nonlocal visits
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value == 987654321:
            visits += 1
        return original(node)

    monkeypatch.setattr(ast, "iter_child_nodes", count)
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert 0 < visits <= depth * 10


def test_attrs_nested_carrier_state_grows_linearly() -> None:
    expression = ast.parse("[" * 30 + "field" + "]" * 30, mode="eval").body
    state: set[str] = set()
    _Resolver._attrs_nested_slots("items", expression, state, lambda _: True)
    assert len(state) <= 30 * 6


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("index", ["0", "-1"])
def test_attrs_nested_signed_indices(tmp_path: Path, *, shared: bool, index: str) -> None:
    path = tmp_path / "case.py"
    body = f"items = [[[FIELD]]]\nitems[{index}][{index}][{index}].default(lambda self: 42)\n"
    if shared:
        source = "spec = attrs.field()\n" + body.replace("FIELD", "spec")
        source += "@attrs.define\nclass Row:\n    value: int = spec\n"
    else:
        source = "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    for outer in (0,):\n        "
        source += body.replace("FIELD", "value").replace("\n", "\n        ") + "del items\n    del outer\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert "default" in result.diagnostic.message


def test_attrs_known_empty_carrier_sequence() -> None:
    states: list[dict[str, bool | _Module] | set[str]] = [{"\0carrier:items": True}, {"\0carrier:items"}]
    for state in states:
        result = _Resolver._attrs_sequence_values(
            ast.Name(id="items", ctx=ast.Load()), state, lambda item: item.id, lambda _: True
        )
        assert result == []


@pytest.mark.parametrize("shared", [False, True])
def test_attrs_empty_named_slice_replacement(tmp_path: Path, *, shared: bool) -> None:
    path = tmp_path / "case.py"
    body = (
        "items = [FIELD]\nsource = []\nitems[:] = source\n"
        "items.append(attrs.field())\nitems[0].default(lambda self: 42)\n"
    )
    if shared:
        source = "spec = attrs.field()\n" + body.replace("FIELD", "spec")
        source += "@attrs.define\nclass Row:\n    value: int = spec\n"
    else:
        source = "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    for outer in (0,):\n        "
        source += body.replace("FIELD", "value").replace("\n", "\n        ") + "del items, source\n    del outer\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
    assert result.row_type is not None
    assert result.row_type.fields[0].required


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("cleanup", [False, True])
def test_attrs_repeated_named_child_alias(tmp_path: Path, *, shared: bool, cleanup: bool) -> None:
    path = tmp_path / "case.py"
    body = (
        "inner = [attrs.field()]\nitems = [inner, inner]\nitems[0][0] = FIELD\nitems[1][0].default(lambda self: 42)\n"
    )
    if shared:
        source = "spec = attrs.field()\n" + body.replace("FIELD", "spec")
        source += "@attrs.define\nclass Row:\n    value: int = spec\n"
    else:
        source = "@attrs.define\nclass Row:\n    value: int = attrs.field()\n    for outer in (0,):\n        "
        source += body.replace("FIELD", "value").replace("\n", "\n        ")
        source += "del inner, items\n        " if cleanup else ""
        source += "pass\n    del outer\n"
    path.write_text(f'import attrs\nfrom rowx import query\n{source}query[Row](t"select 1")\n')
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is not None
    assert "default" in result.diagnostic.message


def test_attrs_starred_assignment_loop_state_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        "import attrs\nfrom rowx import query\n@attrs.define\nclass Row:\n"
        "    value: int = attrs.field()\n    for outer in (0,):\n"
        "        items = [value]\n        source = [attrs.field()]\n"
        "        items[:] = [*source]\n        items[0].default(lambda self: 42)\n"
        '    del outer, items, source\nquery[Row](t"select 1")\n'
    )
    calls = 0
    original = _Resolver._attrs_carrier_related

    def related(carrier: str, state: Collection[str]) -> set[str]:
        nonlocal calls
        calls += 1
        assert calls < 500
        assert len(state) < 200
        return original(carrier, state)

    monkeypatch.setattr(_Resolver, "_attrs_carrier_related", staticmethod(related))
    [call] = discover_file(path).calls
    result = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    assert result.diagnostic is None
