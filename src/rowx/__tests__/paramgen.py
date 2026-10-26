import ast
import warnings
from dataclasses import dataclass, field
from string.templatelib import Template
from typing import TYPE_CHECKING, Final

from rowx.check.params import ParamInference, ParamType

if TYPE_CHECKING:
    from random import Random

SCALAR_KINDS: Final[tuple[str, ...]] = (
    "int",
    "float",
    "str",
    "bytes",
    "bool",
    "None",
    "Template",
    "Decimal",
    "UUID",
    "date",
    "datetime",
    "timedelta",
)
LITERALS: Final[dict[str, tuple[str, ...]]] = {
    "int": ("1", "-7", "+3", "0"),
    "float": ("1.5", "-2.5", "+0.5"),
    "str": ("'a'", '"b"', "'é'", "''"),
    "bytes": ("b'a'", "b''"),
    "bool": ("True", "False"),
    "None": ("None",),
    "Template": ("t'a'", "t'a {1}'"),
}
# kind -> (module, class, constructor arguments)
CLASSES: Final[dict[str, tuple[str, str, tuple[str, ...]]]] = {
    "Decimal": ("decimal", "Decimal", ("'1.5'", "2")),
    "UUID": ("uuid", "UUID", ("int=1", "'12345678-1234-5678-1234-567812345678'")),
    "date": ("datetime", "date", ("2024, 1, 2",)),
    "datetime": ("datetime", "datetime", ("2024, 1, 2", "2024, 1, 2, 3, 4")),
    "timedelta": ("datetime", "timedelta", ("days=1", "seconds=5")),
}
HEADER: Final[str] = "import contextlib\n\nctx = contextlib.nullcontext\n\n\ndef q(t):\n    return t\n\n\n"
# a second binding or a binding form the design marks unknown, as statements for the name `v`
POISONS: Final[tuple[str, ...]] = (
    "v += 1",
    "v = 'other'",
    "v = 1\nv = 2",
    "del v",
    "for v in range(1):\n    pass",
    "with ctx() as v:\n    pass",
    "v, w = 1, 2",
    "[v, *w] = [1, 2]",
    "try:\n    pass\nexcept Exception as v:\n    pass",
    "(v := 3)",
    "import os as v",
    "from os import path as v",
    "def v():\n    pass",
    "class v:\n    pass",
    "match 1:\n    case v:\n        pass",
    "v: int",
    "type v = int",
    "global v",
    "nonlocal v",
)
# statements that leave the name `v` alone, or only bind a different scope's `v`
DECOYS: Final[tuple[str, ...]] = (
    "w = 1\nw += 1",
    "for w in range(1):\n    pass",
    "[v for v in range(1)]",
    "(lambda v: v)(1)",
    "class Other:\n    v = 'class body'",
    "def other(v):\n    return v",
    "w = [v] if False else []",
)
# expressions the design never calls evident
UNKNOWN_EXPRESSIONS: Final[tuple[str, ...]] = (
    "[]",
    "()",
    "[1, 'a']",
    "[1, True]",
    "[*[1]]",
    "{1}",
    "{}",
    "1 + 2",
    "'a' + 'b'",
    "-True",
    "-'a'",
    "1j",
    "...",
    "int('1')",
    "str(1)",
    "len('a')",
    "1 if True else 2",
    "f'{1}'",
    "undefined_name",
    "(lambda: 1)()",
    "decimal.Decimal.from_float(1.5)",
)
RUNTIME_NAMES: Final[dict[type, str]] = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
    bytes: "bytes",
    type(None): "None",
    Template: "Template",
}


@dataclass(slots=True, eq=False)
class Imports:
    rng: Random
    lines: list[str] = field(default_factory=list)
    refs: dict[tuple[str, str], str] = field(default_factory=dict)

    # a class reference spelled through a random import form, the same one each time
    def ref(self, module: str, attr: str) -> str:
        if (module, attr) not in self.refs:
            style = self.rng.choice(("plain", "alias", "from", "from_alias"))
            tag = f"{module}_{attr}"
            match style:
                case "plain":
                    self.lines.append(f"import {module}")
                    self.refs[module, attr] = f"{module}.{attr}"
                case "alias":
                    self.lines.append(f"import {module} as m_{tag}")
                    self.refs[module, attr] = f"m_{tag}.{attr}"
                case "from":
                    self.lines.append(f"from {module} import {attr}")
                    self.refs[module, attr] = attr
                case _:
                    self.lines.append(f"from {module} import {attr} as C_{tag}")
                    self.refs[module, attr] = f"C_{tag}"
        return self.refs[module, attr]


@dataclass(slots=True, frozen=True)
class Value:
    expr: str
    # `None` when the checker has no annotation for it
    annotation: str | None
    shown: str


def shown(found: ParamType | None) -> str | None:
    if found is None:
        return None
    return f"list[{shown(found.item)}]" if found.name == "list" else found.name


def runtime_shown(value: object) -> str:
    if isinstance(value, list | tuple):
        names = {runtime_shown(item) for item in value}
        return f"list[{names.pop()}]" if len(names) == 1 else "list[?]"
    return RUNTIME_NAMES.get(type(value)) or type(value).__name__


def value_of(rng: Random, imports: Imports, kind: str, depth: int = 0) -> Value:
    if kind == "list":
        inner = rng.choice((*SCALAR_KINDS, "list") if depth < 2 else SCALAR_KINDS)
        element = value_of(rng, imports, inner, depth + 1)
        count = rng.randint(1, 3)
        items = ", ".join(element.expr for _ in range(count))
        # a one-element tuple needs its trailing comma
        tuple_expr = f"({items},)" if count == 1 else f"({items})"
        expr = f"[{items}]" if rng.random() < 0.5 else tuple_expr
        annotation = None if element.annotation is None else f"list[{element.annotation}]"
        return Value(expr, annotation, f"list[{element.shown}]")
    if kind in LITERALS:
        annotation = None if kind == "Template" else kind
        return Value(rng.choice(LITERALS[kind]), annotation, kind)
    module, attr, arguments = CLASSES[kind]
    ref = imports.ref(module, attr)
    expr = f"{ref}({rng.choice(arguments)})"
    if kind == "datetime" and rng.random() < 0.3:
        expr = f"{ref}.now()"
    return Value(expr, ref, kind)


def annotation_of(rng: Random, annotation: str, *, nullable: bool = True) -> str:
    # `X | None` counts as `X`
    roll = rng.random()
    if annotation == "None" or not nullable or roll < 0.6:
        return annotation
    return f"{annotation} | None" if roll < 0.8 else f"None | {annotation}"


def pick_kind(rng: Random) -> str:
    return rng.choice((*SCALAR_KINDS, "list"))


def _indent(text: str, by: int = 4) -> str:
    return "".join(f"{' ' * by}{line}\n" if line else "\n" for line in text.splitlines())


@dataclass(slots=True, frozen=True)
class Case:
    source: str
    expected: str | None


# the name `v` is used at `result`; `layout` decides where it is bound and where it is read
def build(rng: Random, *, poison: str | None = None, decoy: str | None = None) -> Case:
    imports = Imports(rng)
    kind = pick_kind(rng)
    value = value_of(rng, imports, kind)
    layout = rng.choice(("module", "function", "param", "closure", "shadow", "method"))
    annotation = None if value.annotation is None else annotation_of(rng, value.annotation)
    if annotation is None:
        # the checker reads no annotation for this type, so it can only come from an assignment
        layout = "function" if layout == "param" else layout
    bound = f"v = {value.expr}" if annotation is None or rng.random() < 0.5 else f"v: {annotation} = {value.expr}"
    extras = [line for line in (poison, decoy) if line is not None]
    # the poison goes in the scope that binds `v`; where it lands relative to the binding is random
    scope_text = "\n".join([bound, *extras] if rng.random() < 0.5 else [*extras, bound])
    use = 'q(t"{v}")'
    method = f"class C:\n    v = 'class body'\n\n    def m(self):\n        return {use}\n"
    match layout:
        case "module":
            body = f"{scope_text}\nresult = {use}\n"
        case "function":
            body = f"def probe():\n{_indent(scope_text)}    return {use}\n\n\nresult = probe()\n"
        case "param":
            # the annotation and the argument are the only binding of `v`; a poison is a second one
            lines = "\n".join([*extras, f"return {use}"])
            body = f"def probe(v: {annotation}):\n{_indent(lines)}\n\nresult = probe({value.expr})\n"
        case "closure":
            body = f"{scope_text}\n\n\ndef probe():\n    return {use}\n\n\nresult = probe()\n"
        case "shadow":
            # the function's own `v` hides the module's
            outer = "v = 1" if kind == "str" else "v = 'outer'"
            body = f"{outer}\n\n\ndef probe():\n{_indent(scope_text)}    return {use}\n\n\nresult = probe()\n"
        case _:
            # a class-body `v` is invisible to its methods, so the method reads the module's
            body = f"{scope_text}\n\n\n{method}\n\nresult = C().m()\n"
    return Case(module_header(imports) + body, None if poison is not None else value.shown)


def module_header(imports: Imports) -> str:
    return "\n".join(imports.lines) + ("\n" if imports.lines else "") + HEADER


def direct(rng: Random) -> Case:
    imports = Imports(rng)
    value = value_of(rng, imports, pick_kind(rng))
    return Case(f'{module_header(imports)}result = q(t"{{{value.expr}}}")\n', value.shown)


def unknown_expression(rng: Random) -> Case:
    expr = rng.choice(UNKNOWN_EXPRESSIONS)
    return Case(f'import decimal\n\n{HEADER}result = q(t"{{({expr})}}")\n', None)


def infer(source: str) -> ParamType | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    inference = ParamInference(tree)
    found = [
        inference.infer(part.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "q"
        for arg in node.args
        if isinstance(arg, ast.TemplateStr)
        for part in arg.values
        if isinstance(part, ast.Interpolation)
    ]
    assert len(found) == 1, source
    return found[0]


def run(source: str) -> object:
    namespace: dict[str, object] = {}
    exec(compile(source, "<case>", "exec"), namespace)  # ruff: ignore[exec-builtin]
    template = namespace["result"]
    assert isinstance(template, Template)
    return template.interpolations[0].value
