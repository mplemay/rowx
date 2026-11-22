import ast
import builtins
from contextlib import suppress
from dataclasses import dataclass, field, replace
from io import BytesIO
from keyword import iskeyword
from pathlib import Path
from sys import stdlib_module_names
from tokenize import detect_encoding
from typing import TYPE_CHECKING, Final, Literal

from rowx._backend import backend_for
from rowx.check.model import Diagnostic, QueryCall, Rule, Span
from rowx.check.walk import literal_prefix

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence

    from rowx_core.backend import CheckerBackend

    from rowx.check.config import Config

MAX_HOPS: Final[int] = 16
INIT_FILE: Final[str] = "__init__.py"
TYPING_MODULES: Final[frozenset[str]] = frozenset({"typing", "typing_extensions"})
BUILTIN_NAMES: Final[frozenset[str]] = frozenset(dir(builtins))
LEAVES: Final[dict[str, str]] = {
    "builtins.int": "int",
    "builtins.float": "float",
    "builtins.str": "str",
    "builtins.bool": "bool",
    "builtins.bytes": "bytes",
    "decimal.Decimal": "decimal.Decimal",
    "datetime.date": "datetime.date",
    "datetime.time": "datetime.time",
    "datetime.datetime": "datetime.datetime",
    "datetime.timedelta": "datetime.timedelta",
    "uuid.UUID": "uuid.UUID",
    "ipaddress.IPv4Interface": "ipaddress.IPv4Interface",
    "ipaddress.IPv6Interface": "ipaddress.IPv6Interface",
    "ipaddress.IPv4Network": "ipaddress.IPv4Network",
    "ipaddress.IPv6Network": "ipaddress.IPv6Network",
}
# `dict[str, str | None]` is the one dict leaf (hstore)
HSTORE_LEAF: Final[str] = "dict[str, str | None]"
# a pair of classes is ONE leaf, and a member alone is not a leaf
NETWORK_PAIRS: Final[dict[frozenset[str], str]] = {
    frozenset({"ipaddress.IPv4Interface", "ipaddress.IPv6Interface"}): (
        "ipaddress.IPv4Interface | ipaddress.IPv6Interface"
    ),
    frozenset({"ipaddress.IPv4Network", "ipaddress.IPv6Network"}): "ipaddress.IPv4Network | ipaddress.IPv6Network",
}
NETWORK_MEMBERS: Final[frozenset[str]] = frozenset(member for pair in NETWORK_PAIRS for member in pair)
FUNCTIONAL: Final[frozenset[str]] = frozenset({"typing.NamedTuple", "typing.TypedDict"})
NEUTRAL: Final[frozenset[str]] = frozenset({"typing.Any", "builtins.object"})
# names from these roots that aren't a supported row type or base are `invalid-row-type`; any other root is outside
# the project and can't be looked at, so it is `unresolved-row-type`
KNOWN_ROOTS: Final[frozenset[str]] = stdlib_module_names | {
    "typing_extensions",
    "rowx",
    "rowx_core",
    "pydantic",
    "attrs",
    "attr",
}
FIELD_QUALIFIERS: Final[frozenset[str]] = frozenset({"typing.Required", "typing.NotRequired", "typing.ReadOnly"})
TYPED_DICT_KEYWORDS: Final[frozenset[str]] = frozenset({"total", "closed", "extra_items"})
DECORATOR_HELP: Final[str] = "remove the decorator, or use `@dataclass`"
INIT_HELP: Final[str] = "remove `__init__` and use `__post_init__`"
KEYWORD_HELP: Final[str] = "remove the metaclass or class keyword"
MIXED_HELP: Final[str] = "keep the class and its bases to one row type form, such as only dataclasses or only models"
OUTSIDE_HELP: Final[str] = "define the row type inside the project"
BASE_HELP: Final[str] = "inherit only from project classes and the row-type base"
CONDITIONAL_FIELD: Final[str] = "a field declared inside a conditional block can't be checked"
PYDANTIC_HELP: Final[str] = (
    "annotate the column's Python type (for example `str`) and validate with `Annotated[str, AfterValidator(...)]`"
)
MODEL_BASES: Final[frozenset[str]] = frozenset({"pydantic.BaseModel", "pydantic.main.BaseModel"})
ROOT_MODELS: Final[frozenset[str]] = frozenset({"pydantic.RootModel", "pydantic.root_model.RootModel"})
PYDANTIC_DATACLASSES: Final[frozenset[str]] = frozenset({"pydantic.dataclasses.dataclass"})
CONFIG_DICTS: Final[frozenset[str]] = frozenset({"pydantic.ConfigDict", "pydantic.config.ConfigDict"})
FIELD_CALLS: Final[frozenset[str]] = frozenset({"pydantic.Field", "pydantic.fields.Field"})
MODEL_VALIDATORS: Final[frozenset[str]] = frozenset({
    "pydantic.model_validator",
    "pydantic.functional_validators.model_validator",
})
ALIAS_FORMS: Final[dict[str, str]] = {
    "pydantic.AliasChoices": "AliasChoices",
    "pydantic.aliases.AliasChoices": "AliasChoices",
    "pydantic.AliasPath": "AliasPath",
    "pydantic.aliases.AliasPath": "AliasPath",
}
# the config keys that decide which column names a field accepts
NAME_KEYS: Final[tuple[str, ...]] = ("populate_by_name", "validate_by_name", "validate_by_alias")
CONFIG_KEYS: Final[tuple[str, ...]] = (*NAME_KEYS, "alias_generator")
PYDANTIC_FORMS: Final[frozenset[str]] = frozenset({"model", "pydantic_dataclass"})

SCOPE_TYPES: Final[
    tuple[
        type[ast.FunctionDef],
        type[ast.AsyncFunctionDef],
        type[ast.Lambda],
        type[ast.ClassDef],
        type[ast.ListComp],
        type[ast.SetComp],
        type[ast.DictComp],
        type[ast.GeneratorExp],
    ]
] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

type RowKind = Literal["none", "scalar", "tuple", "named"]
type BindingKind = Literal["import", "from", "alias", "class", "call", "invalid", "other"]
type RowForm = Literal["positional", "keyword", "dict", "validator"]
type Matching = Literal["by_name", "positional"]
type Form = Literal["dataclass", "namedtuple", "typeddict", "model", "pydantic_dataclass", "attrs"]

FORMS: Final[dict[Form, tuple[RowForm, Matching]]] = {
    "dataclass": ("keyword", "by_name"),
    "namedtuple": ("positional", "positional"),
    "typeddict": ("dict", "by_name"),
    "model": ("validator", "by_name"),
    "pydantic_dataclass": ("validator", "by_name"),
    "attrs": ("validator", "by_name"),
}
DIRECT_FORMS: Final[dict[str, Form]] = {"NamedTuple": "namedtuple", "TypedDict": "typeddict", "BaseModel": "model"}
FUNCTIONAL_ARGS: Final[int] = 2
type ClassKey = tuple[Path, int, int]
DEF_TYPES: Final[tuple[type[ast.FunctionDef], type[ast.AsyncFunctionDef], type[ast.ClassDef]]] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)
MIN_ANNOTATED: Final[int] = 2


# `leaf` is a supported leaf, "list" (with `item`), "any" (`Any` and `object`) or "none" (a tuple element `None`)
@dataclass(slots=True, frozen=True)
class Elem:
    leaf: str
    nullable: bool
    item: Elem | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class AliasSite:
    path: Path
    span: Span
    # the first generic alias followed, with the call-site arguments it was given, as a note
    generic: str | None = None

    def notes(self) -> tuple[str, ...]:
        where = f"alias defined at {self.path}:{self.span.line}:{self.span.col}"
        return (where,) if self.generic is None else (where, self.generic)


# `names` is None when the column names a field accepts can't be known statically; `span` is where the field is defined
@dataclass(slots=True, kw_only=True, frozen=True)
class NamedField:
    key: str
    names: tuple[str, ...] | None
    elem: Elem
    required: bool
    path: Path
    span: Span
    # why `names` is None, for the "names not checked" note
    reason: str | None = None
    # the generic class the field was substituted through and its call-site arguments, for a note
    generic: str | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class RowType:
    kind: RowKind
    elements: tuple[Elem, ...] = ()
    # a named row only
    form: RowForm | None = None
    matching: Matching | None = None
    type_name: str | None = None
    fields: tuple[NamedField, ...] = ()
    # the first alias followed while resolving, for the "alias defined at" note
    alias: AliasSite | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class Resolution:
    row_type: RowType | None = None
    diagnostic: Diagnostic | None = None
    help: str | None = None


@dataclass(slots=True, frozen=True)
class _Binding:
    kind: BindingKind
    module: str = ""
    level: int = 0
    name: str = ""
    expr: ast.expr | None = None
    params: tuple[ast.type_param, ...] = ()
    type_stmt: bool = False
    node: ast.stmt | ast.expr | None = None
    site: Span | None = None
    tc: bool = field(default=False, compare=False)


@dataclass(slots=True)
class _Module:
    path: Path
    # set for a package (`__init__.py` or a namespace directory); submodules live beside it
    directory: Path | None
    tree: ast.Module | None = None
    lines: list[bytes] = field(default_factory=list)
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)
    stars: list[tuple[int, str | None]] = field(default_factory=list)
    all_names: frozenset[str] | None = None

    def span(self, node: ast.stmt | ast.expr) -> Span:
        end_line = node.end_lineno or node.lineno
        end_col = node.end_col_offset if node.end_col_offset is not None else node.col_offset
        return Span(
            line=node.lineno,
            col=self._col(node.lineno, node.col_offset),
            end_line=end_line,
            end_col=self._col(end_line, end_col),
        )

    def _col(self, line: int, byte_col: int) -> int:
        if not 1 <= line <= len(self.lines):
            return byte_col + 1
        return len(self.lines[line - 1][:byte_col].decode(errors="replace")) + 1

    def bind(self, name: str, binding: _Binding) -> None:
        self.bindings.setdefault(name, []).append(binding)


@dataclass(slots=True, frozen=True)
class _External:
    qualname: str


@dataclass(slots=True, frozen=True)
class _ModuleRef:
    module: _Module


@dataclass(slots=True, frozen=True)
class _Alias:
    module: _Module
    expr: ast.expr
    site: Span
    chain: tuple[tuple[Path, str], ...]
    # a `type X[T] = …` statement makes a `TypeAliasType`, which is subscripted to substitute its parameters
    type_stmt: bool = False
    name: str = ""
    params: tuple[_TParam, ...] = ()


# a `class` statement, or a functional `NamedTuple(...)` / `TypedDict(...)` call; `name` is its `__name__`
@dataclass(slots=True, frozen=True, eq=False)
class _Class:
    module: _Module
    node: ast.ClassDef | ast.Call
    name: str


# a module-level `TypeVar(...)`, `TypeVarTuple(...)` or `ParamSpec(...)`: `var` is the name it is bound to and `name` the
# one it is given, which a `type_name` shows
@dataclass(slots=True, frozen=True, eq=False)
class _TypeVar:
    module: _Module
    var: str
    name: str
    kind: str
    default: ast.expr | None

    @property
    def key(self) -> str:
        return f"{self.module.path}:{self.var}"


type _Value = _External | _ModuleRef | _Alias | _Class | _TypeVar


@dataclass(slots=True, frozen=True)
class _Tuple:
    elems: tuple[Elem, ...]


# a type parameter of a generic alias or class; `default` is its PEP 696 default, read in `module`, and `key` is what an
# argument is bound under: the name of a PEP 695 parameter, else the identity of the module-level type variable
@dataclass(slots=True, frozen=True, eq=False)
class _TParam:
    name: str
    kind: str
    default: ast.expr | None
    module: _Module
    key: str


# a type argument, read where it was written: the module, the names in scope there, and the hop chain
@dataclass(slots=True, frozen=True, eq=False)
class _Arg:
    expr: ast.expr
    module: _Module
    env: dict[str, _Arg]
    chain: tuple[tuple[Path, str], ...]
    in_field: bool
    in_alias: bool


# where a generic was subscripted: the arguments and what they are read in
@dataclass(slots=True, frozen=True, eq=False)
class _Site:
    args: tuple[ast.expr, ...]
    module: _Module
    env: dict[str, _Arg]
    chain: tuple[tuple[Path, str], ...]
    in_field: bool = False
    in_alias: bool = False
    # `Alias[()]` names no arguments but is a subscript, unlike a bare `Alias`
    explicit: bool = False


# the named class a row's fields belong to: its `type_name`, the type parameters bound at the call site, and the note
# that names its definition and arguments
@dataclass(slots=True, frozen=True, eq=False)
class _Owner:
    klass: _Class
    type_name: str
    typeddict: bool
    env: dict[str, _Arg]
    generic: str | None


# one step from a class to a base it subscripts (`Base[U]`); `args` are read in `module`
@dataclass(slots=True, frozen=True, eq=False)
class _Rebind:
    klass: _Class
    site: _Site


@dataclass(slots=True, frozen=True, eq=False)
class _NamedRef:
    klass: _Class
    # None for a class that wasn't subscripted
    site: _Site | None = None


@dataclass(slots=True, frozen=True)
class _Node:
    members: tuple[Elem | _Tuple | _NamedRef, ...]
    none: bool


# a pydantic field's alias (a literal `validation_alias`, else `alias`), or why its names are unknown
@dataclass(slots=True, frozen=True)
class _PydField:
    alias: str | None = None
    reason: str | None = None


# a field before its annotation is evaluated; `total` is the `total` of the TypedDict class that declares it
@dataclass(slots=True, kw_only=True)
class _Source:
    key: str
    annotation: ast.expr
    module: _Module
    node: ast.stmt | ast.expr
    default: bool = False
    total: bool = True
    pydantic: _PydField | None = None
    attrs_alias: str | None = None
    converter: ast.expr | None = None
    converter_module: _Module | None = None
    # the class that declares the field, whose type parameters its annotation names
    owner: _Class | None = None


# the config keys the checker reads. A bool or None is a literal value; a str is why the key can't be read, which makes
# every field's names unknown. `blocked` is the same for a config that isn't a literal `ConfigDict(...)` and is sticky
# through inheritance.
@dataclass(slots=True, kw_only=True)
class _Config:
    keys: dict[str, bool | str | None] = field(default_factory=dict)
    blocked: str | None = None

    def reason(self) -> str | None:
        return self.blocked or next(
            (value for key in CONFIG_KEYS if isinstance(value := self.keys.get(key), str)), None
        )


# `kind` is the form a decorator makes; `config` is a pydantic dataclass decorator's `config=` expression
@dataclass(slots=True, kw_only=True, frozen=True)
class _Decor:
    kind: Form
    init: bool = True
    config: ast.expr | None = None


# a name a class body leaves as a class attribute; `value` is None for a `def`, a `class` or a destructuring target
@dataclass(slots=True, frozen=True, eq=False)
class _Attr:
    module: _Module
    value: ast.expr | None


# `fields` maps a name to its source, or to None for a name that is declared but is not a field (a dataclass
# `ClassVar` or `init=False`), which still shadows the base's field of that name; for a dataclass it is what
# `__dataclass_fields__` would be. `attrs` are the class attributes left once the decorator has run.
@dataclass(slots=True, kw_only=True)
class _Info:
    form: Form | None
    fields: dict[str, _Source | None] = field(default_factory=dict)
    init: bool = True
    decorated: bool = False
    attrs: dict[str, _Attr] = field(default_factory=dict)
    # a pydantic form only: the merged config, and why a before or wrap validator makes every name unknown
    config: _Config = field(default_factory=_Config)
    rewrites: str | None = None


class RejectError(Exception):
    def __init__(
        self,
        rule: Rule,
        message: str,
        *,
        help_: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.rule: Rule = rule
        self.message = message
        self.help = help_
        self.notes = notes


def _invalid(message: str, *, help_: str | None = None) -> RejectError:
    return RejectError("invalid-row-type", message, help_=help_)


def _unresolved(message: str, *, help_: str | None = None) -> RejectError:
    return RejectError("unresolved-row-type", message, help_=help_)


def _external_reject(
    qualname: str, *, in_field: bool = False, known_roots: frozenset[str] = KNOWN_ROOTS
) -> RejectError:
    root = qualname.split(".", maxsplit=1)[0]
    if root in known_roots:
        # a pydantic-only type or constraint alias in a field is fixed by annotating the column's Python type
        help_ = PYDANTIC_HELP if in_field and root == "pydantic" else None
        return _invalid(f"`{qualname}` isn't a supported row type", help_=help_)
    return _unresolved(f"`{qualname}` is defined outside the project", help_=OUTSIDE_HELP)


def _is_literal(expr: ast.expr) -> bool:
    try:
        ast.literal_eval(expr)
    except ValueError, TypeError, SyntaxError:
        return False
    return True


def _is_type_checking(test: ast.expr) -> bool:
    match test:
        case ast.Name(id="TYPE_CHECKING") | ast.Attribute(attr="TYPE_CHECKING"):
            return True
    return False


def _is_type_alias_annotation(annotation: ast.expr) -> bool:
    match annotation:
        case ast.Name(id="TypeAlias") | ast.Attribute(attr="TypeAlias"):
            return True
    return False


def _stored_names(node: ast.AST) -> Iterable[str]:
    return (child.id for child in ast.walk(node) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store))


def _literal_names(value: ast.expr) -> frozenset[str] | None:
    if not isinstance(value, ast.List | ast.Tuple):
        return None
    names: list[str] = []
    for element in value.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        names.append(element.value)
    return frozenset(names)


def _collect_import(module: _Module, stmt: ast.Import | ast.ImportFrom, *, tc: bool) -> None:
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            if alias.asname is not None:
                module.bind(alias.asname, _Binding("import", module=alias.name, tc=tc))
            else:
                root = alias.name.split(".")[0]
                module.bind(root, _Binding("import", module=root, tc=tc))
        return
    for alias in stmt.names:
        if alias.name == "*":
            module.stars.append((stmt.level, stmt.module))
        else:
            binding = _Binding("from", module=stmt.module or "", level=stmt.level, name=alias.name, tc=tc)
            module.bind(alias.asname or alias.name, binding)


# a call, lambda or walrus makes an assignment no type alias, except in the metadata of an `Annotated[...]`, which
# is never evaluated as a type (`Id = Annotated[int, Field(alias="id")]`)
def _is_annotated(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Tuple) and node.slice.elts):
        return False
    head = node.value
    return (isinstance(head, ast.Name) and head.id == "Annotated") or (
        isinstance(head, ast.Attribute) and head.attr == "Annotated"
    )


def _impure(value: ast.expr) -> bool:
    pending: list[ast.AST] = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Call | ast.Lambda | ast.NamedExpr):
            return True
        if isinstance(node, ast.Subscript) and _is_annotated(node) and isinstance(node.slice, ast.Tuple):
            pending.extend([node.value, node.slice.elts[0]])
        else:
            pending.extend(ast.iter_child_nodes(node))
    return False


# a call may be a functional `NamedTuple(...)` or `TypedDict(...)`, which only the resolver can tell
def _assignment(name: str, value: ast.expr, site: Span, *, tc: bool) -> _Binding:
    if isinstance(value, ast.Call):
        return _Binding("call", name=name, node=value, site=site, tc=tc)
    return _Binding("invalid" if _impure(value) else "alias", name=name, expr=value, site=site, tc=tc)


def _collect_definition(module: _Module, stmt: ast.stmt, *, tc: bool) -> None:
    site = module.span(stmt)
    match stmt:
        case ast.TypeAlias(name=ast.Name(id=name)):
            module.bind(
                name,
                _Binding("alias", expr=stmt.value, params=tuple(stmt.type_params), type_stmt=True, site=site, tc=tc),
            )
        case ast.AnnAssign(target=ast.Name(id=name), value=value) if value is not None and _is_type_alias_annotation(
            stmt.annotation,
        ):
            module.bind(name, _Binding("alias", expr=value, site=site, tc=tc))
        case ast.Assign(targets=[ast.Name(id="__all__")], value=value):
            module.all_names = _literal_names(value)
            module.bind("__all__", _Binding("other", tc=tc))
        case ast.Assign(targets=[ast.Name(id=name)], value=value):
            module.bind(name, _assignment(name, value, site, tc=tc))
        case ast.ClassDef():
            module.bind(stmt.name, _Binding("class", name=stmt.name, node=stmt, site=site, tc=tc))
        case ast.FunctionDef() | ast.AsyncFunctionDef():
            module.bind(stmt.name, _Binding("invalid", name=stmt.name, node=stmt, site=site, tc=tc))
        case ast.Assign() | ast.AnnAssign() | ast.AugAssign():
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                for name in _stored_names(target):
                    module.bind(name, _Binding("other", tc=tc))


def _collect_nested(module: _Module, stmt: ast.stmt, *, tc: bool) -> None:
    match stmt:
        case ast.If():
            inner = tc or _is_type_checking(stmt.test)
            _collect(module, stmt.body, tc=inner)
            _collect(module, stmt.orelse, tc=inner)
        case ast.Try() | ast.TryStar():
            _collect(module, stmt.body, tc=tc)
            for handler in stmt.handlers:
                if handler.name is not None:
                    module.bind(handler.name, _Binding("other", tc=tc))
                _collect(module, handler.body, tc=tc)
            _collect(module, stmt.orelse, tc=tc)
            _collect(module, stmt.finalbody, tc=tc)
        case ast.With() | ast.AsyncWith() | ast.For() | ast.AsyncFor() | ast.While():
            _collect_loop(module, stmt, tc=tc)


def _collect_loop(
    module: _Module,
    stmt: ast.With | ast.AsyncWith | ast.For | ast.AsyncFor | ast.While,
    *,
    tc: bool,
) -> None:
    match stmt:
        case ast.With() | ast.AsyncWith():
            for item in stmt.items:
                if item.optional_vars is not None:
                    for name in _stored_names(item.optional_vars):
                        module.bind(name, _Binding("other", tc=tc))
        case ast.For() | ast.AsyncFor():
            for name in _stored_names(stmt.target):
                module.bind(name, _Binding("other", tc=tc))
    _collect(module, stmt.body, tc=tc)
    if not isinstance(stmt, ast.With | ast.AsyncWith):
        _collect(module, stmt.orelse, tc=tc)


def _collect(module: _Module, body: Iterable[ast.stmt], *, tc: bool) -> None:
    for stmt in body:
        if isinstance(stmt, ast.Import | ast.ImportFrom):
            _collect_import(module, stmt, tc=tc)
        else:
            _collect_definition(module, stmt, tc=tc)
            _collect_nested(module, stmt, tc=tc)


def _parse(path: Path, *, directory: Path | None) -> _Module | None:
    try:
        data = path.read_bytes()
        encoding, _ = detect_encoding(BytesIO(data).readline)
        text = data.decode(encoding)
        tree = ast.parse(text, str(path))
    except OSError, SyntaxError, UnicodeDecodeError, LookupError, ValueError:
        return None
    module = _Module(path=path, tree=tree, directory=directory, lines=text.encode().splitlines(keepends=True))
    _collect(module, tree.body, tc=False)
    return module


def _same_node(left: ast.AST, right: ast.AST) -> bool:
    keys = ("lineno", "col_offset", "end_lineno", "end_col_offset")
    return type(left) is type(right) and all(getattr(left, key, None) == getattr(right, key, None) for key in keys)


def _enclosing_scopes(node: ast.AST, target: ast.expr, stack: tuple[ast.AST, ...]) -> tuple[ast.AST, ...] | None:
    if _same_node(node, target):
        return stack
    if isinstance(node, SCOPE_TYPES):
        stack = (*stack, node)
    for child in ast.iter_child_nodes(node):
        if (found := _enclosing_scopes(child, target, stack)) is not None:
            return found
    return None


def _local_names(scope: ast.AST) -> set[str]:
    names: set[str] = set()
    body: list[ast.stmt] = []
    match scope:
        case ast.ListComp() | ast.SetComp() | ast.DictComp() | ast.GeneratorExp():
            names = {name for generator in scope.generators for name in _stored_names(generator.target)}
        case ast.ClassDef():
            body = scope.body
        case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.Lambda():
            arguments = scope.args
            every = (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg)
            names = {arg.arg for arg in every if arg is not None}
            body = [] if isinstance(scope, ast.Lambda) else scope.body
    holder = _Module(path=Path(), directory=None)
    _collect(holder, body, tc=False)
    return names | set(holder.bindings)


# only module-level bindings are followed, so a name rebound in a scope around the call can't be trusted
def _reject_shadowed(tree: ast.Module, row_type: ast.expr) -> None:
    scopes = _enclosing_scopes(tree, row_type, ()) or ()
    used = {child.id for child in ast.walk(row_type) if isinstance(child, ast.Name)}
    for index, scope in enumerate(reversed(scopes)):
        # a class body is visible only from its own scope
        if isinstance(scope, ast.ClassDef) and index > 0:
            continue
        if shadowed := used & _local_names(scope):
            msg = f"`{min(shadowed)}` is rebound in a scope around the query, and only module-level names are followed"
            raise _unresolved(msg)


def roots_for(config: Config, path: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for entry in config.include:
        root = (config.root / literal_prefix(entry)).resolve()
        if root.is_file():
            root = root.parent
        if root.is_dir():
            roots.append(root)
    # a src layout: the directory containing the file's top-level package is also a root
    directory = path.resolve().parent
    start = directory
    while (directory / INIT_FILE).is_file():
        directory = directory.parent
    if directory != start:
        roots.append(directory)
    return tuple(dict.fromkeys(roots))


# user code is parsed, never imported or executed, and `sys.path` is never read
class ModuleGraph:
    def __init__(self, boundary: Path | None = None) -> None:
        self._modules: dict[Path, _Module | None] = {}
        self._boundary = boundary

    def load(self, path: Path) -> _Module | None:
        resolved = path.resolve()
        if resolved not in self._modules:
            directory = resolved.parent if resolved.name == INIT_FILE else None
            self._modules[resolved] = _parse(resolved, directory=directory)
        return self._modules[resolved]

    def _namespace(self, directory: Path) -> _Module:
        if (existing := self._modules.get(directory)) is None:
            existing = _Module(path=directory, directory=directory)
            self._modules[directory] = existing
        return existing

    def find_in(self, base: Path, parts: list[str]) -> _Module | None:
        target = base.joinpath(*parts)
        if self._boundary is not None and not target.resolve().is_relative_to(self._boundary):
            return None
        if parts and (candidate := target.with_name(parts[-1] + ".py")).is_file():
            return self.load(candidate)
        if (candidate := target / INIT_FILE).is_file():
            return self.load(candidate)
        if target.is_dir():
            return self._namespace(target.resolve())
        return None

    def find_absolute(self, dotted: str, roots: tuple[Path, ...]) -> _Module | None:
        parts = dotted.split(".")
        if parts[0] in KNOWN_ROOTS:
            return None
        return next((found for root in roots if (found := self.find_in(root, parts)) is not None), None)


def _union(left: _Node, right: _Node) -> _Node:
    return _Node(tuple(dict.fromkeys((*left.members, *right.members))), left.none or right.none)


def _network_pair(node: _Node) -> Elem | None:
    names = frozenset(m.leaf for m in node.members if isinstance(m, Elem) and m.item is None)
    if len(names) != len(node.members) or (pair := NETWORK_PAIRS.get(names)) is None:
        return None
    return Elem(pair, nullable=node.none)


def _pair_of(leaf: str) -> str:
    return next(" | ".join(sorted(pair)) for pair in NETWORK_PAIRS if leaf in pair)


def _to_elem(node: _Node, *, allow_none: bool) -> Elem:
    if not node.members:
        if allow_none:
            return Elem("none", nullable=True)
        msg = "`None` is only valid as a tuple element or as the whole row type"
        raise _invalid(msg)
    if len(node.members) > 1:
        if (pair := _network_pair(node)) is not None:
            return pair
        msg = "a union may have only one non-`None` member"
        raise _invalid(msg)
    (member,) = node.members
    if isinstance(member, _Tuple):
        msg = "a tuple is only valid as the whole row type"
        raise _invalid(msg)
    if isinstance(member, _NamedRef):
        msg = "a named row type is only valid as the whole row type, not inside a tuple, list, union or field"
        raise _invalid(msg)
    if member.leaf == "any":
        return Elem("any", nullable=True)
    if member.leaf in NETWORK_MEMBERS:
        msg = f"`{member.leaf}` is only valid as the pair `{_pair_of(member.leaf)}`"
        raise _invalid(msg)
    return Elem(member.leaf, nullable=node.none, item=member.item)


def _class_key(klass: _Class) -> ClassKey:
    return (klass.module.path, klass.node.lineno, klass.node.col_offset)


# `__x` in a class body is stored as `_Class__x`, which is the key the runtime sees
def _mangle(class_name: str, name: str) -> str:
    stripped = class_name.lstrip("_")
    if name.startswith("__") and not name.endswith("__") and stripped:
        return f"_{stripped}{name}"
    return name


# the statements a class body runs: the body of `if TYPE_CHECKING:` never does, and its `else:` always does
def _plain_statements(body: list[ast.stmt]) -> Iterable[ast.stmt]:
    for stmt in body:
        if isinstance(stmt, ast.If) and _is_type_checking(stmt.test):
            yield from _plain_statements(stmt.orelse)
        else:
            yield stmt


def _nested_statements(node: ast.AST) -> Iterable[ast.stmt]:
    if isinstance(node, ast.If) and _is_type_checking(node.test):
        for stmt in _plain_statements(node.orelse):
            yield stmt
            if not isinstance(stmt, DEF_TYPES):
                yield from _nested_statements(stmt)
        return
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            yield child
            if not isinstance(child, DEF_TYPES):
                yield from _nested_statements(child)
        elif isinstance(child, ast.ExceptHandler | ast.match_case):
            yield from _nested_statements(child)


def _c3_merge(sequences: list[list[ClassKey]]) -> list[ClassKey] | None:
    merged: list[ClassKey] = []
    pending = [seq for seq in sequences if seq]
    while pending:
        head = next((seq[0] for seq in pending if not any(seq[0] in other[1:] for other in pending)), None)
        if head is None:
            return None
        merged.append(head)
        pending = [seq for seq in ([item for item in seq if item != head] for seq in pending) if seq]
    return merged


class _Resolver:
    def __init__(
        self,
        graph: ModuleGraph,
        roots: tuple[Path, ...],
        backend: CheckerBackend | None = None,
        dsn: str | None = None,
    ) -> None:
        self._graph = graph
        self._roots = roots
        self._backend = backend
        self._dsn = dsn
        self.alias: AliasSite | None = None
        # set while a named type's field annotation is evaluated: a string is parsed, and a name imported only under
        # `TYPE_CHECKING` is rejected, because `get_type_hints` would fail on it
        self._in_field = False
        # set while an alias body is evaluated: a string there stays unsupported, but a `TYPE_CHECKING` name is still
        # rejected, because a `TypeAliasType` evaluates its value when the field is read
        self._in_alias = False
        self._attrs_annotation = False
        # the type parameters in scope while a generic's body is evaluated, each bound to its argument
        self._env: dict[str, _Arg] = {}
        self._infos: dict[ClassKey, _Info] = {}
        self._free: set[tuple[Path, str]] = set()
        self._paths: dict[tuple[ClassKey, ClassKey], tuple[_Rebind, ...]] = {}
        self._mros: dict[ClassKey, list[_Class]] = {}
        self._attrs_object_shadows: dict[tuple[Path, int], bool] = {}
        self._attrs_exception_states: list[dict[str, bool | _Module]] = []

    def _selected(self) -> CheckerBackend | None:
        if self._backend is not None:
            return self._backend
        # a blank checker URL is not a scheme; a cached miss would block a later backend
        if self._dsn is None or not self._dsn.strip():
            return None
        # a failure is not stored, so a later lookup tries again
        selected = backend_for(self._dsn).checker()
        self._backend = selected
        return selected

    def _known_roots(self) -> frozenset[str]:
        selected = self._selected()
        if selected is None:
            return KNOWN_ROOTS
        return KNOWN_ROOTS | {name.partition(".")[0] for name in selected.value_classes}

    def _find(self, module: _Module, level: int, dotted: str | None) -> _Module | None:
        if level == 0:
            if dotted is not None and dotted.partition(".")[0] in self._known_roots():
                return None
            return None if dotted is None else self._graph.find_absolute(dotted, self._roots)
        parts = [] if dotted is None else dotted.split(".")
        base = module.directory or module.path.parent
        for _ in range(level - 1):
            base = base.parent
        return self._graph.find_in(base, parts)

    def _exports(self, module: _Module, visiting: frozenset[Path]) -> frozenset[str]:
        if module.all_names is not None:
            return module.all_names
        if module.path in visiting:
            return frozenset[str]()
        names = {name for name in module.bindings if not name.startswith("_")}
        for level, dotted in module.stars:
            if (target := self._find(module, level, dotted)) is not None:
                names |= self._exports(target, visiting | {module.path})
        return frozenset(names)

    def _lookup(
        self,
        module: _Module,
        name: str,
        chain: tuple[tuple[Path, str], ...],
        *,
        allow_builtin: bool,
    ) -> _Value:
        key = (module.path, name)
        if key in chain:
            msg = f"`{name}` is defined in terms of itself"
            raise _unresolved(msg)
        # a plain `X = ...` alias is evaluated by Python when the module runs, so it is no hop of a chain
        over = sum(1 for hop in chain if hop not in self._free) >= MAX_HOPS
        chain = (*chain, key)
        if (bindings := module.bindings.get(name)) is not None:
            self._check_hops(name, bindings, over=over)
            return self._follow(module, name, bindings, chain)
        providers = [
            target
            for level, dotted in module.stars
            if (target := self._find(module, level, dotted)) is not None and name in self._exports(target, frozenset())
        ]
        if len({provider.path for provider in providers}) > 1:
            msg = f"`{name}` is provided by more than one star import"
            raise _unresolved(msg)
        if providers:
            self._check_hops(name, [], over=over)
            return self._lookup(providers[0], name, chain, allow_builtin=False)
        # a builtin ends a chain, so it is not a hop of its own
        if allow_builtin and name in BUILTIN_NAMES:
            return _External(f"builtins.{name}")
        msg = f"`{name}` can't be resolved statically"
        raise _unresolved(msg)

    @staticmethod
    def _check_hops(name: str, bindings: list[_Binding], *, over: bool) -> None:
        if not over:
            return
        msg = f"`{name}` is more than {MAX_HOPS} hops from its definition"
        # a substitution that deep is a bad generic; any other chain is just too long to follow
        generic = any(
            binding.params or (isinstance(binding.node, ast.ClassDef) and binding.node.type_params)
            for binding in bindings
        )
        raise _invalid(msg) if generic else _unresolved(msg)

    def _follow(
        self,
        module: _Module,
        name: str,
        bindings: list[_Binding],
        chain: tuple[tuple[Path, str], ...],
    ) -> _Value:
        unique = list(dict.fromkeys(bindings))
        # a `TYPE_CHECKING` binding is the one a type checker sees, so the first one wins
        if tc := [binding for binding in unique if binding.tc]:
            if (self._in_field or self._in_alias) and len(tc) == len(unique):
                msg = f"`{name}` is imported only under `TYPE_CHECKING`"
                raise _invalid(msg, help_=f"import `{name}` at runtime")
            unique = tc[:1]
        if len(unique) > 1:
            msg = f"`{name}` is bound more than once at module level"
            raise _unresolved(msg)
        return self._binding_value(module, name, unique[0], chain)

    def _binding_value(
        self,
        module: _Module,
        name: str,
        binding: _Binding,
        chain: tuple[tuple[Path, str], ...],
    ) -> _Value:
        match binding.kind:
            case "import":
                return self._module_value(binding.module, module)
            case "from":
                return self._from_value(module, binding, chain)
            case "alias":
                if binding.expr is None or binding.site is None:
                    msg = f"`{name}` has no value"
                    raise _unresolved(msg)
                if not binding.type_stmt:
                    self._free.add((module.path, name))
                params = self._type_params(binding.params, module)
                return _Alias(
                    module, binding.expr, binding.site, chain, type_stmt=binding.type_stmt, name=name, params=params
                )
            case "class" if isinstance(binding.node, ast.ClassDef):
                return _Class(module, binding.node, binding.node.name)
            case "call":
                if (typevar := self._typevar(module, name, binding)) is not None:
                    self._free.add((module.path, name))
                    return typevar
                return self._functional(module, name, binding)
            case "other":
                msg = f"`{name}` is not bound by an import or a type alias"
                raise _unresolved(msg)
        msg = f"`{name}` is not a type expression"
        raise _invalid(msg)

    # `X = NamedTuple("X", ...)` and `X = TypedDict("X", ...)` are classes; any other call isn't a type expression
    def _functional(self, module: _Module, name: str, binding: _Binding) -> _Value:
        call = binding.node
        if not isinstance(call, ast.Call):
            msg = f"`{name}` is not a type expression"
            raise _invalid(msg)
        try:
            func = self._resolve_ref(call.func, module, ())
        except RejectError:
            func = None
        if not isinstance(func, _External) or self._normalize(func.qualname) not in FUNCTIONAL:
            msg = f"`{name}` is not a type expression"
            raise _invalid(msg)
        match call.args:
            case [ast.Constant(value=str() as class_name), *_]:
                return _Class(module, call, class_name)
        msg = f"`{func.qualname}(...)` needs a literal class name"
        raise _unresolved(msg)

    def _module_value(self, dotted: str, module: _Module) -> _Value:
        found = self._find(module, 0, dotted)
        return _ModuleRef(found) if found is not None else _External(dotted)

    def _from_value(self, module: _Module, binding: _Binding, chain: tuple[tuple[Path, str], ...]) -> _Value:
        target = self._find(module, binding.level, binding.module)
        if target is None:
            if binding.level:
                msg = f"the relative import of `{binding.name}` doesn't resolve to a project module"
                raise _unresolved(msg)
            return _External(f"{binding.module}.{binding.name}")
        if binding.name in target.bindings or binding.name in self._exports(target, frozenset()):
            return self._lookup(target, binding.name, chain, allow_builtin=False)
        return self._submodule(target, binding.name)

    def _submodule(self, module: _Module, name: str) -> _ModuleRef:
        if module.directory is not None and (found := self._graph.find_in(module.directory, [name])) is not None:
            return _ModuleRef(found)
        msg = f"`{name}` isn't defined in a project module"
        raise _unresolved(msg)

    def _resolve_ref(self, expr: ast.expr, module: _Module, chain: tuple[tuple[Path, str], ...]) -> _Value:
        match expr:
            case ast.Name(id=name):
                return self._lookup(module, name, chain, allow_builtin=True)
            case ast.Attribute(value=base, attr=attr):
                match self._resolve_ref(base, module, chain):
                    case _External(qualname):
                        return _External(f"{qualname}.{attr}")
                    case _ModuleRef(module=target):
                        if attr in target.bindings or attr in self._exports(target, frozenset()):
                            return self._lookup(target, attr, chain, allow_builtin=False)
                        return self._submodule(target, attr)
                    case _:
                        msg = "an attribute of a type alias isn't a row type"
                        raise _invalid(msg)
        msg = "the row type isn't a name"
        raise _invalid(msg)

    def _eval(self, expr: ast.expr, module: _Module, chain: tuple[tuple[Path, str], ...]) -> _Node:
        match expr:
            case ast.Constant(value=None):
                return _Node((), none=True)
            case ast.Constant(value=str() as text):
                if not self._in_field or self._in_alias:
                    msg = "a quoted type isn't supported"
                    raise _invalid(msg)
                return self._eval(self._parse_quoted(text), module, chain)
            case ast.Name(id=name) if name in self._env:
                return self._eval_arg(self._env[name])
            case ast.Name() | ast.Attribute():
                return self._eval_value(self._resolve_ref(expr, module, chain))
            case ast.Subscript():
                return self._eval_subscript(expr, module, chain)
            case ast.BinOp(op=ast.BitOr(), left=left, right=right):
                return _union(self._eval(left, module, chain), self._eval(right, module, chain))
            case ast.Call(func=func) if self._in_field and (qualname := self._external_name(func, module)) is not None:
                if qualname.split(".", maxsplit=1)[0] == "pydantic":
                    msg = f"`{qualname}(...)` isn't a supported row type"
                    raise _invalid(msg, help_=PYDANTIC_HELP)
        msg = "the row type isn't derivable from the row-type grammar"
        raise _invalid(msg)

    @staticmethod
    def _parse_quoted(text: str) -> ast.expr:
        try:
            return ast.parse(text.strip(), mode="eval").body
        except SyntaxError:
            msg = f"the annotation {text!r} can't be parsed"
            raise _invalid(msg) from None

    def _eval_value(self, value: _Value) -> _Node:
        match value:
            case _Alias():
                # a bare generic alias takes its defaults, or is rejected
                return self._expand_alias(value, _Site((), value.module, {}, value.chain))
            case _External(qualname):
                normalized = self._normalize(qualname)
                if (leaf := LEAVES.get(normalized)) is not None:
                    return _Node((Elem(leaf, nullable=False),), none=False)
                if normalized in NEUTRAL:
                    leaf = normalized if self._attrs_annotation else "any"
                    return _Node((Elem(leaf, nullable=False),), none=False)
                if (selected := self._selected()) is not None and (
                    leaf := selected.value_leaves.get(normalized)
                ) is not None:
                    return _Node((Elem(leaf, nullable=False),), none=False)
                raise _external_reject(qualname, in_field=self._in_field, known_roots=self._known_roots())
            case _Class():
                return _Node((_NamedRef(value),), none=False)
            case _TypeVar():
                if value.kind == "TypeVar" and value.key in self._env:
                    return self._eval_arg(self._env[value.key])
                msg = f"`{value.name}` is a type variable, which isn't a row type"
                raise _invalid(msg)
            case _ModuleRef():
                msg = "a module isn't a row type"
                raise _invalid(msg)

    def _scoped(
        self, expr: ast.expr, module: _Module, chain: tuple[tuple[Path, str], ...], env: dict[str, _Arg]
    ) -> _Node:
        saved, self._env = self._env, env
        try:
            return self._eval(expr, module, chain)
        finally:
            self._env = saved

    def _eval_arg(self, arg: _Arg) -> _Node:
        flags = (self._in_field, self._in_alias)
        self._in_field, self._in_alias = arg.in_field, arg.in_alias
        try:
            return self._scoped(arg.expr, arg.module, arg.chain, arg.env)
        finally:
            self._in_field, self._in_alias = flags

    def _expand_alias(self, alias: _Alias, site: _Site) -> _Node:
        alias = replace(alias, params=self._alias_params(alias))
        # Python gives a bare `X = tuple[T, str]` no defaults: it is the generic itself
        if not alias.type_stmt and alias.params and not site.args and not site.explicit:
            msg = f"the type parameter `{alias.params[0].name}` of `{alias.name}` is not bound"
            raise _invalid(msg, help_=f"subscript the alias, for example `{alias.name}[int]`")
        env = self._bind(alias.name, alias.params, site)
        if self.alias is None:
            self.alias = AliasSite(path=alias.module.path, span=alias.site)
        if alias.params and self.alias.generic is None:
            arguments = ", ".join(f"{p.name} = {self._text(env[p.key])}" for p in alias.params)
            where = f"{alias.module.path}:{alias.site.line}:{alias.site.col}"
            generic = f"generic alias `{alias.name}` defined at {where}, with {arguments}"
            self.alias = replace(self.alias, generic=generic)
        in_alias, self._in_alias = self._in_alias, True
        try:
            # the body is read in the module that defines it, with only its own type parameters in scope
            return self._scoped(alias.expr, alias.module, alias.chain, env)
        finally:
            self._in_alias = in_alias

    # each type parameter bound to its argument, or else to its PEP 696 default, which may name the parameters before it
    def _bind(self, name: str, params: tuple[_TParam, ...], site: _Site) -> dict[str, _Arg]:
        for param in params:
            if param.kind != "TypeVar":
                msg = f"`{name}` has the {param.kind} `{param.name}`, which is not supported"
                raise _invalid(msg)
        required = sum(1 for param in params if param.default is None)
        if not site.args and required:
            first = next(param for param in params if param.default is None)
            msg = f"the type parameter `{first.name}` of `{name}` is not bound"
            raise _invalid(msg, help_=f"subscript the class or alias, for example `{name}[int]`")
        if not required <= len(site.args) <= len(params):
            takes = str(len(params)) if required == len(params) else f"{required} to {len(params)}"
            msg = f"`{name}` takes {takes} type arguments, but {len(site.args)} were given"
            raise _invalid(msg)
        env: dict[str, _Arg] = {}
        for index, param in enumerate(params):
            if index < len(site.args):
                env[param.key] = _Arg(site.args[index], site.module, site.env, site.chain, site.in_field, site.in_alias)
            elif param.default is not None:
                env[param.key] = _Arg(param.default, param.module, dict(env), (), self._in_field, self._in_alias)
        return env

    # the text of an argument as `type_name` shows it, which is the runtime's: names bound by an enclosing generic are
    # replaced by their own arguments, a plain alias by its target, a dotted name is shortened to its last part as
    # `__name__` is, `Optional[X]` and `Union[A, B]` are `X | None` and `A | B`, and `Annotated` shows only its type
    def _text(self, arg: _Arg) -> str:
        return " | ".join(self._parts(arg.expr, arg.module, arg.env, 0))

    # the members of a union, flattened and without repeats, or the single text of anything else
    def _parts(self, node: ast.expr, module: _Module, env: dict[str, _Arg], depth: int) -> list[str]:
        if depth > MAX_HOPS:
            return [ast.unparse(node)]
        match node:
            case ast.Name(id=name) if name in env:
                return self._parts(env[name].expr, env[name].module, env[name].env, depth + 1)
            case ast.Name() | ast.Attribute():
                return self._ref_parts(node, module, env, depth)
            case ast.BinOp(op=ast.BitOr(), left=left, right=right):
                members = [left, right]
            case ast.Subscript(value=head, slice=inner):
                parts = inner.elts if isinstance(inner, ast.Tuple) else [inner]
                match self._external_name(head, module):
                    case "typing.Optional" if len(parts) == 1:
                        members = [parts[0], ast.Constant(value=None)]
                    case "typing.Union":
                        members = parts
                    case "typing.Annotated" if len(parts) >= MIN_ANNOTATED:
                        return self._parts(parts[0], module, env, depth)
                    case _:
                        members = []
            case _:
                members = []
        if not members:
            return [self._render(node, module, env, depth)]
        return list(dict.fromkeys(text for member in members for text in self._parts(member, module, env, depth)))

    def _render(self, node: ast.expr, module: _Module, env: dict[str, _Arg], depth: int) -> str:
        if depth > MAX_HOPS:
            return ast.unparse(node)
        match node:
            case ast.Name() | ast.Attribute():
                return " | ".join(self._ref_parts(node, module, env, depth))
            case ast.Subscript(value=value, slice=inner):
                parts = inner.elts if isinstance(inner, ast.Tuple) else [inner]
                if (plain := self._render_plain_alias(value, parts, module, env, depth)) is not None:
                    return plain
                shown = ", ".join(" | ".join(self._parts(part, module, env, depth + 1)) for part in parts)
                return f"{self._render(value, module, env, depth + 1)}[{shown}]"
            case ast.BinOp(op=ast.BitOr()):
                return " | ".join(self._parts(node, module, env, depth + 1))
        return ast.unparse(node)

    def _ref_parts(
        self, node: ast.Name | ast.Attribute, module: _Module, env: dict[str, _Arg], depth: int
    ) -> list[str]:
        fallback = [node.id if isinstance(node, ast.Name) else node.attr]
        try:
            value = self._resolve_ref(node, module, ())
        except RejectError:
            return fallback
        match value:
            case _TypeVar() if value.key in env:
                arg = env[value.key]
                return self._parts(arg.expr, arg.module, arg.env, depth + 1)
            case _TypeVar():
                return [value.name]
            case _Class():
                return [value.name]
            case _Alias(type_stmt=False) if not self._alias_params(value):
                return self._parts(value.expr, value.module, {}, depth + 1)
        return fallback

    # `Row[int]` of a plain `Row = tuple[T, str]` is `tuple[int, str]`
    def _render_plain_alias(
        self, head: ast.expr, parts: list[ast.expr], module: _Module, env: dict[str, _Arg], depth: int
    ) -> str | None:
        try:
            value = self._constructor(self._resolve_ref(head, module, ()))
            if not (isinstance(value, _Alias) and not value.type_stmt and (params := self._alias_params(value))):
                return None
            site = _Site(tuple(parts), module, env, ())
            inner = self._bind(value.name, params, site)
        except RejectError:
            return None
        return " | ".join(self._parts(value.expr, value.module, inner, depth + 1))

    @staticmethod
    def _normalize(qualname: str) -> str:
        module, _, name = qualname.rpartition(".")
        return f"typing.{name}" if module in TYPING_MODULES else qualname

    def _constructor(self, value: _Value) -> _Value:
        # `L = list` is just `list` at runtime, so `L[int]` is `list[int]`
        while isinstance(value, _Alias) and not value.type_stmt:
            if self.alias is None:
                self.alias = AliasSite(path=value.module.path, span=value.site)
            if not isinstance(value.expr, ast.Name | ast.Attribute):
                break
            value = self._resolve_ref(value.expr, value.module, value.chain)
        return value

    # a subscripted generic alias is substituted (a plain `X = tuple[T, str]` is one when it uses a type variable), and a
    # subscripted class is a named row to be substituted
    def _eval_generic(self, base: _Value, site: _Site) -> _Node | None:
        if isinstance(base, _Alias) and (base.type_stmt or self._alias_params(base)):
            return self._expand_alias(base, site)
        if isinstance(base, _Class):
            return _Node((_NamedRef(base, site),), none=False)
        return None

    def _eval_subscript(self, expr: ast.Subscript, module: _Module, chain: tuple[tuple[Path, str], ...]) -> _Node:
        base = self._constructor(self._resolve_ref(expr.value, module, chain))
        slice_ = expr.slice
        args = tuple(slice_.elts) if isinstance(slice_, ast.Tuple) else (slice_,)
        site = _Site(
            args, module, self._env, chain, self._in_field, self._in_alias, explicit=isinstance(slice_, ast.Tuple)
        )
        if (generic := self._eval_generic(base, site)) is not None:
            return generic
        if not isinstance(base, _External):
            msg = "only `tuple`, `list`, `Optional`, `Union` and `Annotated` can be subscripted"
            raise _invalid(msg)
        slice_ = expr.slice
        elts = slice_.elts if isinstance(slice_, ast.Tuple) else [slice_]
        qualname = self._normalize(base.qualname)
        match qualname:
            case "typing.Optional" if len(elts) == 1 and not isinstance(slice_, ast.Tuple):
                return _union(self._eval(slice_, module, chain), _Node((), none=True))
            case "typing.Union" if elts:
                nodes = [self._eval(element, module, chain) for element in elts]
                result = nodes[0]
                for node in nodes[1:]:
                    result = _union(result, node)
                return result
            case "typing.Annotated" if len(elts) >= 2 and isinstance(slice_, ast.Tuple):  # ruff: ignore[magic-value-comparison]
                return self._eval(elts[0], module, chain)
        if (node := self._eval_container(qualname, slice_, module, chain)) is not None:
            return node
        if (
            (selected := self._selected()) is not None
            and qualname in selected.range_classes
            and len(elts) == 1
            and not isinstance(slice_, ast.Tuple)
        ):
            ranged = self._eval_range(slice_, module, chain)
            if ranged is not None:
                return ranged
        msg = f"`{base.qualname}` can't be subscripted this way in a row type"
        raise _invalid(msg, help_=PYDANTIC_HELP if self._in_field and qualname.startswith("pydantic.") else None)

    def _eval_container(
        self, qualname: str, slice_: ast.expr, module: _Module, chain: tuple[tuple[Path, str], ...]
    ) -> _Node | None:
        elts = slice_.elts if isinstance(slice_, ast.Tuple) else [slice_]
        match qualname:
            case "builtins.list" if len(elts) == 1 and not isinstance(slice_, ast.Tuple):
                item = _to_elem(self._eval(slice_, module, chain), allow_none=False)
                return _Node((Elem("list", nullable=False, item=item),), none=False)
            case "builtins.dict" if len(elts) == 2 and isinstance(slice_, ast.Tuple):  # ruff: ignore[magic-value-comparison]
                return self._eval_hstore(elts, module, chain)
            case "builtins.tuple" if elts:
                elems = tuple(_to_elem(self._eval(element, module, chain), allow_none=True) for element in elts)
                return _Node((_Tuple(elems),), none=False)
        return None

    def _eval_hstore(self, elts: list[ast.expr], module: _Module, chain: tuple[tuple[Path, str], ...]) -> _Node:
        key = _to_elem(self._eval(elts[0], module, chain), allow_none=False)
        value = _to_elem(self._eval(elts[1], module, chain), allow_none=False)
        if key != Elem("str", nullable=False) or value != Elem("str", nullable=True):
            msg = "a dict row type must be `dict[str, str | None]`, the hstore leaf"
            raise _invalid(msg)
        return _Node((Elem(HSTORE_LEAF, nullable=False),), none=False)

    def _eval_range(self, subtype: ast.expr, module: _Module, chain: tuple[tuple[Path, str], ...]) -> _Node | None:
        # no vocabulary is a miss: the subscript is an ordinary external type
        if (selected := self._selected()) is None:
            return None
        inner = _to_elem(self._eval(subtype, module, chain), allow_none=False)
        leaves = selected.range_leaves
        if inner.nullable or inner.leaf not in leaves:
            listed = ", ".join(f"`{leaf}`" for leaf in leaves)
            msg = f"`Range[{inner.leaf}]` isn't a range subtype; use one of {listed}"
            raise _invalid(msg)
        return _Node((Elem(leaves[inner.leaf], nullable=False),), none=False)

    # ---- named row types ----

    def _ref(self, expr: ast.expr, module: _Module) -> _Value:
        return self._constructor(self._resolve_ref(expr, module, ()))

    def _external_name(self, expr: ast.expr, module: _Module) -> str | None:
        # the normalized qualified name of `expr`, or None when it is not an import-resolved name
        try:
            value = self._resolve_ref(expr, module, ())
        except RejectError:
            return None
        return self._normalize(value.qualname) if isinstance(value, _External) else None

    def _base(self, expr: ast.expr, klass: _Class, stack: tuple[ClassKey, ...]) -> _Info | str:
        subscripted = isinstance(expr, ast.Subscript)
        target = expr.value if isinstance(expr, ast.Subscript) else expr
        if not isinstance(target, ast.Name | ast.Attribute):
            msg = f"a base of `{klass.name}` isn't a class name"
            raise _invalid(msg)
        match self._ref(target, klass.module):
            case _External(qualname):
                return self._external_base(self._normalize(qualname), qualname, subscripted=subscripted)
            case _Class() as base:
                return self._info(base, (*stack, _class_key(klass)))
        msg = f"a base of `{klass.name}` isn't a class"
        raise _invalid(msg)

    def _external_base(self, normalized: str, qualname: str, *, subscripted: bool) -> str:
        if normalized == "typing.Protocol":
            msg = "a Protocol is not a row type"
            raise _invalid(msg)
        # `Generic[T]` only declares the type parameters, which `_params` reads
        if normalized == "typing.Generic":
            return "object"
        if (selected := self._selected()) is not None and (value := selected.value_classes.get(qualname)) is not None:
            public_name = min(
                (name for name, leaf in selected.value_classes.items() if leaf == value),
                key=lambda name: (name.count("."), name),
            )
            msg = f"a subclass of {public_name} is not a row type"
            raise _invalid(msg)
        if qualname == "pydantic.v1" or qualname.startswith("pydantic.v1."):
            msg = "a pydantic v1 model is not supported"
            raise _invalid(msg)
        if normalized in ROOT_MODELS:
            msg = "a pydantic RootModel is not a row type"
            raise _invalid(msg)
        if subscripted:
            msg = f"`{qualname}` can't be subscripted as a base of a row type"
            raise _invalid(msg)
        if normalized in MODEL_BASES:
            return "BaseModel"
        if normalized in {"typing.NamedTuple", "typing.TypedDict", "builtins.object"}:
            return normalized.rsplit(".", maxsplit=1)[-1]
        if qualname.split(".", maxsplit=1)[0] in self._known_roots():
            msg = f"`{qualname}` isn't a supported row type"
            raise _invalid(msg, help_=BASE_HELP)
        raise _external_reject(qualname, known_roots=self._known_roots())

    @staticmethod
    def _literal_bool(keyword: ast.keyword, what: str) -> bool:
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool):
            return keyword.value.value
        msg = f"{what} isn't a literal"
        raise _unresolved(msg)

    @staticmethod
    def _attrs_bool(keyword: ast.keyword, what: str) -> bool:
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool):
            return keyword.value.value
        msg = f"{what} isn't a literal boolean"
        raise _invalid(msg)

    @staticmethod
    def _attrs_decorator_option(keyword: ast.keyword, klass: _Class) -> None:
        if keyword.arg == "collect_by_mro" and (
            not isinstance(keyword.value, ast.Constant) or keyword.value.value is not True
        ):
            msg = f"the `collect_by_mro` option of `{klass.name}` is unsupported"
            raise _invalid(msg)
        if keyword.arg in {"these", "field_transformer", "auto_attribs"}:
            if not isinstance(keyword.value, ast.Constant) or keyword.value.value is not None:
                msg = f"the `{keyword.arg}` option of `{klass.name}` is unsupported"
                raise _invalid(msg)
        elif keyword.arg not in {
            "init",
            "kw_only",
            "slots",
            "frozen",
            "repr",
            "eq",
            "order",
            "hash",
            "weakref_slot",
            "auto_exc",
            "getstate_setstate",
            "on_setattr",
            "collect_by_mro",
            "match_args",
            "unsafe_hash",
            "force_kw_only",
        }:
            msg = f"the `{keyword.arg}` option of `{klass.name}` is unsupported"
            raise _invalid(msg)
        elif keyword.arg != "on_setattr" and not isinstance(keyword.value, ast.Constant):
            msg = f"the `{keyword.arg}` option of `{klass.name}` is dynamic"
            raise _invalid(msg)

    # None for a decorator that is ignored (`final`), else the form a dataclass decorator makes and its `init`
    def _decorator(self, decorator: ast.expr, klass: _Class) -> _Decor | None:
        call = decorator if isinstance(decorator, ast.Call) else None
        func = call.func if call is not None else decorator
        try:
            value = self._resolve_ref(func, klass.module, ())
        except RejectError as reject:
            if reject.rule != "invalid-row-type":
                raise
            value = None
        name = self._normalize(value.qualname) if isinstance(value, _External) else None
        if name == "typing.final":
            return None
        kind: Form = "pydantic_dataclass" if name in PYDANTIC_DATACLASSES else "dataclass"
        if name in {"attrs.define", "attrs.frozen", "attr.define", "attr.frozen"}:
            kind = "attrs"
        if name != "dataclasses.dataclass" and kind not in {"pydantic_dataclass", "attrs"}:
            msg = f"the decorator `{ast.unparse(func)}` on `{klass.name}` is unknown, so its effect on the constructor is not checked"
            raise _invalid(msg, help_=DECORATOR_HELP)
        if kind == "attrs" and call is not None and call.args:
            msg = f"the attrs decorator on `{klass.name}` has unsupported positional arguments"
            raise _invalid(msg)
        init, config = self._decorator_options(call, klass, kind)
        # pydantic's decorator only takes `init=False`, which keeps its own validating `__init__`
        return _Decor(kind=kind, init=True if kind == "pydantic_dataclass" else init, config=config)

    def _decorator_options(self, call: ast.Call | None, klass: _Class, kind: Form) -> tuple[bool, ast.expr | None]:
        init, config = True, None
        for keyword in call.keywords if call is not None else ():
            if keyword.arg is None:
                msg = f"the dataclass decorator on `{klass.name}` takes `**` arguments"
                raise _invalid(msg) if kind == "attrs" else _unresolved(msg)
            if keyword.arg == "config":
                config = keyword.value
            if kind == "attrs":
                self._attrs_decorator_option(keyword, klass)
            if keyword.arg in {"init", "kw_only"}:
                literal_bool = self._attrs_bool if kind == "attrs" else self._literal_bool
                literal = literal_bool(keyword, f"the `{keyword.arg}` of the dataclass decorator on `{klass.name}`")
                init = literal if keyword.arg == "init" else init
        return init, config

    # the form of the dataclass decorator on the class, if there is one
    def _decorators(self, node: ast.ClassDef, klass: _Class) -> _Decor | None:
        found: _Decor | None = None
        for decorator in node.decorator_list:
            if (decor := self._decorator(decorator, klass)) is not None:
                if found is not None and found.kind != decor.kind:
                    msg = f"`{klass.name}` mixes row type forms"
                    raise _invalid(msg, help_=MIXED_HELP)
                found = decor
        return found

    def _total(self, node: ast.ClassDef) -> bool:
        total = True
        for keyword in node.keywords:
            if keyword.arg == "total":
                total = self._literal_bool(keyword, f"the `total` of `{node.name}`")
        return total

    @staticmethod
    def _check_keywords(node: ast.ClassDef, *, known: frozenset[str]) -> None:
        for keyword in node.keywords:
            if keyword.arg is None:
                msg = f"`{node.name}` takes `**` class keywords"
                raise _unresolved(msg)
            if keyword.arg not in known:
                what = "the metaclass" if keyword.arg == "metaclass" else f"the class keyword `{keyword.arg}`"
                msg = f"{what} of `{node.name}` is unknown, so its effect on the constructor is not checked"
                raise _invalid(msg, help_=KEYWORD_HELP)

    def _bases(self, node: ast.ClassDef, klass: _Class, stack: tuple[ClassKey, ...]) -> tuple[list[_Info], set[str]]:
        bases = [self._base(base, klass, stack) for base in node.bases]
        direct = {base for base in bases if isinstance(base, str)} - {"object"}
        if len(direct) > 1:
            msg = f"`{klass.name}` can't combine {' and '.join(sorted(direct))}"
            raise _invalid(msg)
        return [base for base in bases if isinstance(base, _Info)], direct

    @staticmethod
    def _form(klass: _Class, infos: list[_Info], direct: set[str], *, decorated: Form | None) -> Form | None:
        forms: set[Form] = {info.form for info in infos if info.form is not None}
        forms |= {DIRECT_FORMS[name] for name in direct}
        if decorated is not None:
            forms.add(decorated)
        if len(forms) > 1:
            msg = f"`{klass.name}` mixes row type forms"
            raise _invalid(msg, help_=MIXED_HELP)
        return next(iter(forms), None)

    def _info(self, klass: _Class, stack: tuple[ClassKey, ...]) -> _Info:
        key = _class_key(klass)
        if key in stack:
            msg = f"`{klass.name}` inherits from itself"
            raise _unresolved(msg)
        if (cached := self._infos.get(key)) is None:
            cached = self._infos[key] = self._build_info(klass, stack)
        return cached

    def _build_info(self, klass: _Class, stack: tuple[ClassKey, ...]) -> _Info:
        node = klass.node
        if isinstance(node, ast.Call):
            return self._functional_info(klass, node)
        infos, direct = self._bases(node, klass, stack)
        decor = self._decorators(node, klass)
        form = self._form(klass, infos, direct, decorated=decor.kind if decor is not None else None)
        if form != "model":
            self._check_keywords(node, known=TYPED_DICT_KEYWORDS if form == "typeddict" else frozenset())
        decorated = decor is not None
        info = _Info(form=form, decorated=decorated, init=decor.init if decor is not None else True)
        if form == "attrs":
            self._attrs_check_bases(klass, infos)
        raw = self._raw_attrs(node, klass)
        match form:
            case "dataclass" | "pydantic_dataclass":
                self._dataclass_fields(info, node, klass, raw)
            case "attrs":
                self._attrs_fields(info, node, klass)
            case "model":
                self._model_fields(info, node, klass, infos)
            case "namedtuple":
                self._namedtuple_fields(info, node, klass, infos, direct=bool(direct))
            case "typeddict":
                self._typeddict_fields(info, node, klass, infos)
        if form in PYDANTIC_FORMS:
            self._pydantic_info(info, node, klass, decor, infos)
        info.attrs = self._final_attrs(node, klass, raw, decorated=decorated, pydantic=form == "pydantic_dataclass")
        return info

    # ---- generics ----

    @staticmethod
    def _type_params(nodes: Iterable[ast.type_param], module: _Module) -> tuple[_TParam, ...]:
        found: list[_TParam] = []
        for node in nodes:
            match node:
                case ast.TypeVar():
                    found.append(_TParam(node.name, "TypeVar", node.default_value, module, node.name))
                case ast.TypeVarTuple():
                    found.append(_TParam(node.name, "TypeVarTuple", node.default_value, module, node.name))
                case ast.ParamSpec():
                    found.append(_TParam(node.name, "ParamSpec", node.default_value, module, node.name))
        return tuple(found)

    # `T = TypeVar("T")`, `TypeVarTuple("Ts")` or `ParamSpec("P")`, from `typing` or `typing_extensions`
    def _typevar(self, module: _Module, var: str, binding: _Binding) -> _TypeVar | None:
        call = binding.node
        if not isinstance(call, ast.Call):
            return None
        kind = (self._external_name(call.func, module) or "").removeprefix("typing.")
        if kind not in {"TypeVar", "TypeVarTuple", "ParamSpec"}:
            return None
        default = next((kw.value for kw in call.keywords if kw.arg == "default"), None)
        match call.args:
            case [ast.Constant(value=str() as name), *_]:
                return _TypeVar(module, var, name, kind, default)
        return _TypeVar(module, var, var, kind, default)

    @staticmethod
    def _tparam(typevar: _TypeVar) -> _TParam:
        return _TParam(typevar.name, typevar.kind, typevar.default, typevar.module, typevar.key)

    # the type variables in `expr`, depth first in order of first appearance, as `typing` collects a generic's
    # `__parameters__`: a quoted name and the metadata of an `Annotated` are not collected
    def _collect(self, expr: ast.AST, module: _Module, found: dict[str, _TypeVar]) -> None:
        match expr:
            case ast.Constant():
                return
            case ast.Name() | ast.Attribute():
                try:
                    value = self._resolve_ref(expr, module, ())
                except RejectError:
                    return
                if isinstance(value, _TypeVar):
                    found.setdefault(value.key, value)
                return
            case ast.Subscript(value=head, slice=ast.Tuple(elts=[first, _, *_])) if (
                self._external_name(head, module) == "typing.Annotated"
            ):
                self._collect(first, module, found)
                return
        for child in ast.iter_child_nodes(expr):
            self._collect(child, module, found)

    # the parameters of a plain `X = tuple[T, str]` alias, which are the type variables it uses
    def _alias_params(self, alias: _Alias) -> tuple[_TParam, ...]:
        if alias.type_stmt:
            return alias.params
        found: dict[str, _TypeVar] = {}
        self._collect(alias.expr, alias.module, found)
        return tuple(self._tparam(typevar) for typevar in found.values())

    # the type parameters of a class: its `type_params`, else the `Generic[...]` base, else the type variables its bases
    # are subscripted with, in the order `typing` collects them
    def _params(self, klass: _Class) -> tuple[_TParam, ...]:
        node = klass.node
        if not isinstance(node, ast.ClassDef):
            return ()
        if node.type_params:
            return self._type_params(node.type_params, klass.module)
        generic = next(
            (
                base
                for base in node.bases
                if isinstance(base, ast.Subscript) and self._external_name(base.value, klass.module) == "typing.Generic"
            ),
            None,
        )
        spans = (
            [generic.slice] if generic is not None else [b.slice for b in node.bases if isinstance(b, ast.Subscript)]
        )
        found: dict[str, _TypeVar] = {}
        for span in spans:
            self._collect(span, klass.module, found)
        return tuple(self._tparam(typevar) for typevar in found.values())

    # the steps from `root` to `owner` along the bases that are subscripted or not, so a field's own type parameters can
    # be bound through each base's arguments (`Child[U](Base[U])`)
    def _path(self, root: _Class, owner: _Class) -> tuple[_Rebind, ...]:
        key = (_class_key(root), _class_key(owner))
        if (cached := self._paths.get(key)) is not None:
            return cached
        pending: list[tuple[_Class, tuple[_Rebind, ...]]] = [(root, ())]
        seen = {_class_key(root)}
        found: tuple[_Rebind, ...] = ()
        while pending:
            klass, path = pending.pop(0)
            if _class_key(klass) == key[1]:
                found = path
                break
            if not isinstance(klass.node, ast.ClassDef):
                continue
            for base in klass.node.bases:
                target = base.value if isinstance(base, ast.Subscript) else base
                if not (
                    isinstance(target, ast.Name | ast.Attribute)
                    and isinstance(value := self._ref(target, klass.module), _Class)
                ):
                    continue
                if _class_key(value) in seen:
                    continue
                seen.add(_class_key(value))
                args = ()
                if isinstance(base, ast.Subscript):
                    args = tuple(base.slice.elts) if isinstance(base.slice, ast.Tuple) else (base.slice,)
                step = _Rebind(value, _Site(args, klass.module, {}, ()))
                pending.append((value, (*path, step)))
        self._paths[key] = found
        return found

    # the runtime binds every generic base of the row's class, whether a field comes from it or not, so a chain of more
    # than `MAX_HOPS` subscripted bases fails however deep the fields are
    def _check_depth(self, root: _Class) -> None:
        pending = [(root, 0)]
        seen = {_class_key(root)}
        while pending:
            klass, depth = pending.pop(0)
            if not isinstance(klass.node, ast.ClassDef):
                continue
            for base in klass.node.bases:
                target = base.value if isinstance(base, ast.Subscript) else base
                if (
                    not (
                        isinstance(target, ast.Name | ast.Attribute)
                        and isinstance(value := self._ref(target, klass.module), _Class)
                    )
                    or _class_key(value) in seen
                ):
                    continue
                seen.add(_class_key(value))
                subscripted = isinstance(base, ast.Subscript)
                if subscripted and depth >= MAX_HOPS:
                    msg = f"`{root.name}` has generic bases deeper than {MAX_HOPS}"
                    raise _invalid(msg)
                pending.append((value, depth + int(subscripted)))

    def _project_bases(self, klass: _Class) -> list[_Class]:
        if not isinstance(klass.node, ast.ClassDef):
            return []
        found: list[_Class] = []
        for base in klass.node.bases:
            target = base.value if isinstance(base, ast.Subscript) else base
            if isinstance(target, ast.Name | ast.Attribute) and isinstance(
                value := self._ref(target, klass.module), _Class
            ):
                found.append(value)
        return found

    # the C3 linearization over the project classes; a base outside the project is already rejected
    def _mro(self, klass: _Class) -> list[_Class]:
        key = _class_key(klass)
        if (cached := self._mros.get(key)) is not None:
            return cached
        bases = self._project_bases(klass)
        linearized = [self._mro(base) for base in bases]
        by_key = {_class_key(member): member for seq in linearized for member in seq}
        merged = _c3_merge([[_class_key(m) for m in seq] for seq in linearized] + [[_class_key(b) for b in bases]])
        if merged is None:
            msg = f"`{klass.name}` has no consistent method resolution order"
            raise _unresolved(msg)
        self._mros[key] = result = [klass, *(by_key[member] for member in merged)]
        return result

    # the names the class body leaves as class attributes, before the dataclass decorator runs
    @staticmethod
    def _raw_attrs(node: ast.ClassDef, klass: _Class) -> dict[str, _Attr]:
        attrs: dict[str, _Attr] = {}
        body = list(_plain_statements(node.body))
        statements = [*body, *(nested for stmt in body for nested in _nested_statements(stmt))]
        for stmt in statements:
            match stmt:
                case ast.Assign(targets=targets, value=value):
                    for target in targets:
                        for name in _stored_names(target):
                            direct = isinstance(target, ast.Name)
                            attrs[_mangle(node.name, name)] = _Attr(klass.module, value if direct else None)
                case ast.AnnAssign(target=ast.Name(id=name), value=value) if value is not None:
                    attrs[_mangle(node.name, name)] = _Attr(klass.module, value)
                case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                    attrs[_mangle(node.name, stmt.name)] = _Attr(klass.module, None)
        return attrs

    # a decorated dataclass deletes the attribute of a `field(...)` without a `default`, and replaces the others
    # with their default
    def _final_attrs(
        self, node: ast.ClassDef, klass: _Class, raw: dict[str, _Attr], *, decorated: bool, pydantic: bool
    ) -> dict[str, _Attr]:
        if not decorated:
            return raw
        attrs = dict(raw)
        calls = {"dataclasses.field", *FIELD_CALLS} if pydantic else {"dataclasses.field"}
        for stmt in _plain_statements(node.body):
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            key = _mangle(node.name, stmt.target.id)
            value = stmt.value
            if isinstance(value, ast.Call) and (name := self._external_name(value.func, klass.module)) in calls:
                if name in FIELD_CALLS:
                    # pydantic keeps a `Field(...)` without a default as the class attribute, so a redeclaring subclass
                    # merges it, and turns one with a default into that plain default
                    kwargs: dict[str, ast.expr] = {}
                    self._field_keywords(kwargs, value, klass)
                    if "default" in kwargs:
                        attrs[key] = _Attr(klass.module, None)
                elif any(keyword.arg == "default" for keyword in value.keywords):
                    attrs[key] = _Attr(klass.module, None)
                else:
                    attrs.pop(key, None)
        return attrs

    def _namedtuple_fields(
        self, info: _Info, node: ast.ClassDef, klass: _Class, infos: list[_Info], *, direct: bool
    ) -> None:
        if not direct:
            info.fields = dict(next(base.fields for base in infos if base.form == "namedtuple"))
            return
        for stmt, key in self._annotated(node):
            info.fields[key] = self._source(stmt, key, klass, default=stmt.value is not None)

    def _typeddict_fields(self, info: _Info, node: ast.ClassDef, klass: _Class, infos: list[_Info]) -> None:
        for base in infos:
            if base.form == "typeddict":
                info.fields.update(base.fields)
        total = self._total(node)
        for stmt, key in self._annotated(node):
            info.fields[key] = self._source(stmt, key, klass, total=total)

    @staticmethod
    def _annotated(node: ast.ClassDef) -> list[tuple[ast.AnnAssign, str]]:
        body = list(_plain_statements(node.body))
        for stmt in body:
            for nested in _nested_statements(stmt) if not isinstance(stmt, DEF_TYPES) else ():
                if isinstance(nested, ast.AnnAssign) and isinstance(nested.target, ast.Name):
                    raise _invalid(CONDITIONAL_FIELD)
        return [
            (stmt, _mangle(node.name, stmt.target.id))
            for stmt in body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        ]

    @staticmethod
    def _source(stmt: ast.AnnAssign, key: str, klass: _Class, *, default: bool = False, total: bool = True) -> _Source:
        return _Source(
            key=key,
            annotation=stmt.annotation,
            module=klass.module,
            node=stmt,
            default=default,
            total=total,
            owner=klass,
        )

    # the expression a name stands for once its `X = ...` and `X: TypeAlias = ...` aliases are followed, and the module
    # it is read in; a `type X = ...` is no alias here, because pydantic reads nothing from it
    def _unalias(self, expr: ast.expr, module: _Module) -> tuple[ast.expr, _Module]:
        for _ in range(MAX_HOPS + 1):
            if not isinstance(expr, ast.Name | ast.Attribute):
                break
            try:
                value = self._resolve_ref(expr, module, ())
            except RejectError:
                break
            if not isinstance(value, _Alias) or value.type_stmt:
                break
            expr, module = value.expr, value.module
        return expr, module

    # `ClassVar`, `InitVar` or `KW_ONLY`, which the dataclass module tells apart by the head of the annotation, found
    # behind an alias; pydantic also looks through `Annotated` for a `ClassVar`
    def _special(self, annotation: ast.expr, module: _Module, *, pydantic: bool = False) -> str | None:
        specials = {"typing.ClassVar": "ClassVar", "dataclasses.InitVar": "InitVar", "dataclasses.KW_ONLY": "KW_ONLY"}
        expr = annotation
        for _ in range(MAX_HOPS + 1):
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                try:
                    expr = ast.parse(expr.value.strip(), mode="eval").body
                except SyntaxError:
                    return None
            if isinstance(expr, ast.Subscript):
                slice_ = expr.slice
                head, head_module = self._unalias(expr.value, module)
                if (
                    pydantic
                    and isinstance(slice_, ast.Tuple)
                    and len(slice_.elts) >= MIN_ANNOTATED
                    and self._external_name(head, head_module) == "typing.Annotated"
                    and self._special(slice_.elts[0], module, pydantic=True) == "ClassVar"
                ):
                    # pydantic unwraps `Annotated` only to find a `ClassVar`; any other head counts as written
                    return "ClassVar"
            else:
                head, head_module = self._unalias(expr, module)
                if head is not expr and not isinstance(head, ast.Name | ast.Attribute):
                    expr, module = head, head_module
                    continue
            if not isinstance(head, ast.Name | ast.Attribute):
                return None
            return specials.get(self._external_name(head, head_module) or "")
        return None

    def _dataclass_fields(self, info: _Info, node: ast.ClassDef, klass: _Class, raw: dict[str, _Attr]) -> None:
        # the infos of the project classes after this one, in method resolution order
        rest = [self._info(member, ()) for member in self._mro(klass)[1:]]
        pydantic = info.form == "pydantic_dataclass"
        if not info.decorated:
            if pydantic:
                # the runtime sees a plain dataclass here, because pydantic marks only the decorated class
                msg = f"`{klass.name}` subclasses a pydantic dataclass without the pydantic decorator"
                raise _invalid(msg)
            # `__dataclass_fields__` is read as an attribute, so the first dataclass in the MRO supplies it
            if (first := next((base for base in rest if base.decorated), None)) is not None:
                info.fields = dict(first.fields)
                info.init = first.init
            return
        # the library collects the bases from the last to the first, and a redeclared field keeps its place
        for base in reversed(rest):
            info.fields.update(base.fields)
        # a pydantic dataclass runs its validator through `__init__`, so a hand-written one is allowed
        if not pydantic and any(isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__" for stmt in node.body):
            msg = f"`{klass.name}` defines `__init__`, so its effect on the constructor is not checked"
            raise _invalid(msg, help_=INIT_HELP)
        inherited = [raw, *(base.attrs for base in rest)]
        for stmt, key in self._annotated(node):
            match self._special(stmt.annotation, klass.module, pydantic=pydantic):
                case "ClassVar":
                    info.fields[key] = None
                case "KW_ONLY":
                    pass
                case "InitVar":
                    msg = f"`{klass.name}` has the InitVar field `{key}`, which is not supported"
                    raise _invalid(msg)
                case _:
                    info.fields[key] = (
                        self._pydantic_dataclass_field(stmt, key, klass, inherited)
                        if pydantic
                        else self._dataclass_field(stmt, key, klass, inherited)
                    )

    def _attrs_fields(self, info: _Info, node: ast.ClassDef, klass: _Class) -> None:
        rest = [self._info(member, ()) for member in self._mro(klass)[1:]]
        if any(
            isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and stmt.name == "__init__" for stmt in node.body
        ):
            msg = f"`{klass.name}` defines a custom constructor"
            raise _invalid(msg)
        if not info.decorated:
            if (first := next((base for base in rest if base.decorated), None)) is not None:
                info.fields = dict(first.fields)
                info.init = first.init
            return
        self._attrs_unannotated_fields(node, klass)
        for base in reversed(rest):
            for key, source in base.fields.items():
                info.fields.pop(key, None)
                info.fields[key] = source
        for stmt, key in self._annotated(node):
            info.fields.pop(key, None)
            info.fields[key] = self._attrs_field(stmt, key, klass)
        self._attrs_default_hooks(info, node, klass)

    def _attrs_field_call(self, expr: ast.expr, module: _Module) -> tuple[ast.Call, _Module] | None:
        field, modules = self._attrs_trace_field(expr, module)
        if field is not None and not isinstance(expr, ast.Call):
            for origin in modules:
                if self._attrs_mutated_field(field, origin):
                    msg = "an attrs shared field has unsupported post-construction default metadata"
                    raise _invalid(msg)
        return field

    def _attrs_trace_field(
        self, expr: ast.expr, module: _Module
    ) -> tuple[tuple[ast.Call, _Module] | None, list[_Module]]:
        seen: set[tuple[Path, str]] = set()
        modules = [module]
        value = expr
        for _ in range(MAX_HOPS + 1):
            if isinstance(value, ast.Call):
                func, func_module = self._unalias(value.func, module)
                if self._external_name(func, func_module) in {"attrs.field", "attr.field", "attr.ib", "attr.attrib"}:
                    return (value, module), modules
                break
            next_value = self._attrs_field_binding(value, module, seen)
            if next_value is None:
                break
            value, module = next_value
            if all(item.path != module.path for item in modules):
                modules.append(module)
        return None, modules

    def _attrs_mutated_field(self, field: tuple[ast.Call, _Module], module: _Module) -> bool:
        if module.tree is None:
            return False
        globals_: dict[str, bool | _Module] = {}
        return self._attrs_scan_body(module.tree.body, globals_, field, module, globals_)

    def _attrs_is_shared(
        self,
        expr: ast.expr,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Tuple | ast.List | ast.Name):
            return bool(self._attrs_indexed_value(expr, scope, field, module, globals_))
        alternatives = self._attrs_expression_branches(expr)
        if alternatives != [expr]:
            return any(self._attrs_is_shared(value, scope, field, module, globals_) for value in alternatives)
        if isinstance(expr, ast.Attribute):
            class_name = next((key.removeprefix("\0private:") for key in globals_ if key.startswith("\0private:")), "")
            if class_name:
                expr = ast.Attribute(value=expr.value, attr=_mangle(class_name, expr.attr), ctx=expr.ctx)
        if isinstance(expr, ast.Name):
            if expr.id in scope:
                return scope[expr.id] is True
            if expr.id in globals_:
                return globals_[expr.id] is True
        if (
            isinstance(expr, ast.Attribute)
            and isinstance(expr.value, ast.Name)
            and (expr.value.id in scope or expr.value.id in globals_)
        ):
            binding = scope.get(expr.value.id, globals_.get(expr.value.id, False))
            if not isinstance(binding, _Module):
                return binding
            expr, module = ast.Name(id=expr.attr, ctx=ast.Load()), binding
        target, _ = self._attrs_trace_field(expr, module)
        return target is not None and target[0] is field[0] and target[1].path == field[1].path

    @staticmethod
    def _attrs_join_scope(target: dict[str, bool | _Module], source: dict[str, bool | _Module]) -> None:
        for key in target.keys() | source.keys():
            if key.startswith("\0slot:"):
                name = key.split(":", 2)[1]
                if (key not in source and bool(source.get(name))) or (key not in target and bool(target.get(name))):
                    target[key] = True
        for name, value in source.items():
            if value:
                prior = target.get(name, False)
                target[name] = value if not prior or prior == value else True

    def _attrs_record_exception_state(self, scope: dict[str, bool | _Module]) -> None:
        for possible in self._attrs_exception_states:
            self._attrs_join_scope(possible, scope)

    @staticmethod
    def _attrs_forget_carrier(name: str, scope: dict[str, bool | _Module]) -> None:
        for key in list(scope):
            if (
                key.startswith(f"\0slot:{name}:")
                or key == f"\0carrier:{name}"
                or (key.startswith("\0alias:") and name in key.split(":")[1:])
            ):
                del scope[key]

    @staticmethod
    def _attrs_mutate_carrier(name: str, scope: dict[str, bool | _Module], *, value: bool) -> None:
        related = {name}
        while True:
            before = related.copy()
            for key in scope:
                if key.startswith("\0alias:"):
                    _, left, right = key.split(":", 2)
                    if left in related or right in related:
                        related.update((left, right))
            if related == before:
                break
        possible = value or any(scope.get(item) is True for item in related)
        for item in related:
            for key in list(scope):
                if key.startswith(f"\0slot:{item}:"):
                    del scope[key]
            scope[item] = possible

    def _attrs_scan_eager(
        self,
        node: ast.AST,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        if isinstance(node, ast.GeneratorExp):
            node = node.generators[0].iter
        elif isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp):
            return self._attrs_scan_comprehension(node, scope, field, module, globals_)
        if isinstance(node, ast.BoolOp | ast.IfExp):
            return (
                self._attrs_scan_bool(node, scope, field, module, globals_)
                if isinstance(node, ast.BoolOp)
                else self._attrs_scan_ifexp(node, scope, field, module, globals_)
            )
        if isinstance(node, ast.NamedExpr):
            earlier = (
                self._attrs_class_value(node.value, scope, field, module, globals_)
                if isinstance(node.value, ast.Subscript)
                else False
            )
            if self._attrs_scan_eager(node.value, scope, field, module, globals_):
                return True
            self._attrs_forget_carrier(node.target.id, scope)
            scope[node.target.id] = (
                earlier
                if isinstance(node.value, ast.Subscript)
                else self._attrs_class_value(node.value, scope, field, module, globals_)
            )
            self._attrs_record_exception_state(scope)
            node = ast.Constant(value=None)
        if isinstance(node, ast.ClassDef):
            return self._attrs_scan_nested_class(node, scope, field, module, globals_)
        if isinstance(node, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
            # Function decorators and defaults execute now; the body does not.
            decorators = [] if isinstance(node, ast.Lambda) else node.decorator_list
            header = [*decorators, *node.args.defaults, *node.args.kw_defaults]
            return any(
                self._attrs_scan_eager(item, scope, field, module, globals_) for item in header if item is not None
            )
        self._attrs_scan_mutation_call(node, scope, field, module, globals_)
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "default"
            and self._attrs_is_shared(node.value, scope, field, module, globals_)
        ) or any(self._attrs_scan_eager(item, scope, field, module, globals_) for item in ast.iter_child_nodes(node))

    @staticmethod
    def _attrs_carrier_tracked(carrier: str, scope: Collection[str]) -> bool:
        return any(
            key.startswith((f"\0slot:{carrier}:", f"\0alias:{carrier}:")) or key == f"\0carrier:{carrier}"
            for key in scope
        )

    @staticmethod
    def _attrs_written_slots(target: ast.expr, keys: Collection[str]) -> list[str]:
        if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
            return []
        try:
            index = ast.literal_eval(target.slice)
        except ValueError, TypeError:
            return []
        if not isinstance(index, int):
            return []
        prefix = f"\0slot:{target.value.id}:"
        positions = {key.removeprefix(prefix).removesuffix(":known") for key in keys if key.startswith(prefix)}
        positive = {int(item) for item in positions if item.isdigit()}
        if not positive or not -len(positive) <= index < len(positive):
            return []
        normalized = index % len(positive)
        return [f"{prefix}{normalized}", f"{prefix}{normalized - len(positive)}"]

    def _attrs_scan_mutation_call(
        self,
        node: ast.AST,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> None:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in {"append", "extend", "insert", "pop", "remove", "clear", "reverse", "sort"}
        ):
            carrier = node.func.value.id
            if self._attrs_carrier_tracked(carrier, scope):
                possible = any(
                    self._attrs_class_value(arg, scope, field, module, globals_) is True for arg in node.args
                )
                self._attrs_mutate_carrier(carrier, scope, value=possible)

    def _attrs_scan_nested_class(
        self,
        node: ast.ClassDef,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        # Nested classes do not close over the enclosing class namespace.
        header = [*node.decorator_list, *node.bases, *node.keywords]
        if any(self._attrs_scan_eager(item, scope, field, module, globals_) for item in header):
            return True
        local: dict[str, bool | _Module] = {"\0class": False}
        nested_globals = self._attrs_class_globals(node, field, module, globals_)
        global_names: set[str] = set()
        pending: list[ast.AST] = list(node.body)
        while pending:
            child = pending.pop()
            if isinstance(child, ast.Global):
                global_names.update(child.names)
            elif not isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                pending.extend(ast.iter_child_nodes(child))
        for statement in node.body:
            saved, self._attrs_exception_states = self._attrs_exception_states, []
            try:
                if self._attrs_scan_body([statement], local, field, module, nested_globals):
                    return True
            finally:
                self._attrs_exception_states = saved
            for name in global_names:
                if name in local:
                    nested_globals[name] = local.pop(name)
                    globals_[name] = nested_globals[name]
                    if scope is globals_:
                        scope[name] = nested_globals[name]
            self._attrs_record_exception_state(scope)
        return False

    def _attrs_scan_ifexp(
        self,
        node: ast.IfExp,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        if self._attrs_scan_eager(node.test, scope, field, module, globals_):
            return True
        states = []
        for branch in self._attrs_expression_branches(node):
            state = scope.copy()
            branch_globals = state if scope is globals_ else globals_
            if self._attrs_scan_eager(branch, state, field, module, branch_globals):
                return True
            states.append(state)
        scope.clear()
        scope.update(states[0])
        for state in states[1:]:
            self._attrs_join_scope(scope, state)
        return False

    def _attrs_scan_bool(
        self,
        node: ast.BoolOp,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        exits: list[dict[str, bool | _Module]] = []
        for value in node.values:
            if self._attrs_scan_eager(value, scope, field, module, globals_):
                return True
            if isinstance(value, ast.Constant):
                if bool(value.value) == isinstance(node.op, ast.Or):
                    break
            else:
                exits.append(scope.copy())
        for earlier in exits:
            self._attrs_join_scope(scope, earlier)
        return False

    def _attrs_class_globals(
        self, node: ast.ClassDef, field: tuple[ast.Call, _Module], module: _Module, globals_: dict[str, bool | _Module]
    ) -> dict[str, bool | _Module]:
        result = {key: value for key, value in globals_.items() if not key.startswith("\0private:")}
        result[f"\0private:{node.name}"] = False
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and (mangled := _mangle(node.name, child.id)) != child.id:
                result[child.id] = self._attrs_class_value(
                    ast.Name(id=mangled, ctx=ast.Load()), globals_, field, module, globals_
                )
        return result

    def _attrs_scan_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        # A comprehension closes over a surrounding comprehension, but not a
        # class namespace. Its first iterable still executes in that namespace.
        local = {} if "\0class" in scope else scope.copy()
        for index, generator in enumerate(node.generators):
            current = scope if index == 0 else local
            before = current.copy()
            before_globals = before if current is globals_ else globals_
            if self._attrs_scan_eager(generator.iter, current, field, module, globals_):
                return True
            targets = [target.id for target in ast.walk(generator.target) if isinstance(target, ast.Name)]
            local.update(dict.fromkeys(targets, False))
            literal = isinstance(generator.iter, ast.Tuple | ast.List | ast.Set)
            values = generator.iter.elts if literal else []
            expressions = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            if (
                not literal
                or (
                    isinstance(generator.target, ast.Tuple | ast.List)
                    and any(not isinstance(value, ast.Tuple | ast.List) for value in values)
                )
            ) and self._attrs_loop_default([*expressions, *generator.ifs], set(targets)):
                msg = "an attrs default registration has an indeterminate comprehension binding"
                raise _invalid(msg)
            for value in values:
                bindings = before.copy()
                self._attrs_bind_target((generator.target, value), bindings, field, module, globals_=before_globals)
                for target in ast.walk(generator.target):
                    if isinstance(target, ast.Name) and bool(bindings.get(target.id)):
                        local[target.id] = bindings[target.id]
                self._attrs_scan_eager(value, before, field, module, before_globals)
            if any(self._attrs_scan_eager(test, local, field, module, globals_) for test in generator.ifs):
                return True
        values = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        found = any(self._attrs_scan_eager(value, local, field, module, globals_) for value in values)
        self._attrs_merge_walrus(node, scope, local)
        return found

    @staticmethod
    def _attrs_merge_walrus(node: ast.AST, scope: dict[str, bool | _Module], local: dict[str, bool | _Module]) -> None:
        # Assignment expressions in eager comprehensions bind outside their loop scope.
        pending = [node]
        while pending:
            item = pending.pop()
            if (
                isinstance(item, ast.NamedExpr)
                and (binding := local.get(item.target.id)) is not None
                and binding is not False
            ):
                scope[item.target.id] = binding
            if isinstance(item, ast.GeneratorExp):
                pending.append(item.generators[0].iter)
            elif not isinstance(item, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
                pending.extend(ast.iter_child_nodes(item))

    def _attrs_scan_body(
        self,
        body: list[ast.stmt],
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        for node in body:
            self._attrs_record_exception_state(scope)
            if isinstance(node, ast.If):
                if self._attrs_scan_if(node, scope, field, module, globals_):
                    return True
            elif isinstance(
                node, ast.For | ast.AsyncFor | ast.While | ast.With | ast.AsyncWith | ast.Try | ast.TryStar | ast.Match
            ):
                if self._attrs_scan_compound(node, scope, field, module, globals_):
                    return True
            elif self._attrs_scan_simple(node, scope, field, module, globals_):
                return True
            self._attrs_record_exception_state(scope)
        return False

    def _attrs_scan_simple(
        self,
        node: ast.stmt,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        captured = (
            self._attrs_capture_assignment(node, scope, field, module, globals_) if isinstance(node, ast.Assign) else {}
        )
        earlier = None
        if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Subscript):
            earlier = self._attrs_class_value(node.value, scope, field, module, globals_)
        if self._attrs_scan_eager(node, scope, field, module, globals_):
            return True
        targets = (
            node.targets
            if isinstance(node, ast.Assign | ast.Delete)
            else [node.target]
            if isinstance(node, ast.AnnAssign | ast.AugAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                carrier = target.value.id
                if self._attrs_carrier_tracked(carrier, scope):
                    value = node.value if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign) else None
                    possible = (
                        value is not None and self._attrs_class_value(value, scope, field, module, globals_) is True
                    )
                    written = self._attrs_written_slots(target, scope) if isinstance(node, ast.Assign) else []
                    self._attrs_mutate_carrier(carrier, scope, value=possible)
                    for slot in written:
                        scope[slot] = possible
        self._attrs_bind_class(node, scope, field, module, globals_)
        scope.update(captured)
        if earlier is not None and isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    scope[target.id] = earlier
        return False

    def _attrs_capture_assignment(
        self,
        node: ast.Assign,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> dict[str, bool | _Module]:
        captured: dict[str, bool | _Module] = {}
        if isinstance(node.value, ast.Tuple | ast.List):
            state = scope.copy()
            state_globals = state if scope is globals_ else globals_
            values: dict[str, bool | _Module] = {}

            def capture(expr: ast.expr) -> ast.expr:
                if isinstance(expr, ast.Tuple | ast.List):
                    return type(expr)(elts=[capture(item) for item in expr.elts], ctx=expr.ctx)
                if isinstance(expr, ast.Starred):
                    return ast.Starred(value=capture(expr.value), ctx=expr.ctx)
                key = f"\0value:{len(values)}"
                values[key] = self._attrs_class_value(expr, state, field, module, state_globals)
                self._attrs_scan_eager(expr, state, field, module, state_globals)
                return ast.Name(id=key, ctx=ast.Load())

            saved, self._attrs_exception_states = self._attrs_exception_states, []
            try:
                value = capture(node.value)
            finally:
                self._attrs_exception_states = saved
            sources = scope | values
            for target in node.targets:
                bindings = sources.copy()
                self._attrs_bind_target((target, value), bindings, field, module, globals_=globals_)
                captured.update({
                    item.id: bindings[item.id]
                    for item in ast.walk(target)
                    if isinstance(item, ast.Name) and item.id in bindings
                })
                for item in ast.walk(target):
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                        captured.update({
                            key: value
                            for key, value in bindings.items()
                            if key.startswith(f"\0slot:{item.id}:") or key == f"\0carrier:{item.id}"
                        })
        return captured

    def _attrs_scan_if(
        self,
        node: ast.If,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        if self._attrs_scan_eager(node.test, scope, field, module, globals_):
            return True
        if isinstance(node.test, ast.Constant):
            return self._attrs_scan_body(
                node.body if bool(node.test.value) else node.orelse, scope, field, module, globals_
            )
        left, right = scope.copy(), scope.copy()
        left_globals = left if scope is globals_ else globals_
        right_globals = right if scope is globals_ else globals_
        if self._attrs_scan_body(node.body, left, field, module, left_globals) or self._attrs_scan_body(
            node.orelse, right, field, module, right_globals
        ):
            return True
        for name in left.keys() | right.keys():
            fallback = self._attrs_is_shared(ast.Name(id=name, ctx=ast.Load()), scope, field, module, globals_)
            if name.startswith("\0slot:"):
                container = name.split(":", 2)[1]
                left_value = left.get(name, left.get(container, False))
                right_value = right.get(name, right.get(container, False))
                scope[name] = left_value if left_value == right_value else bool(left_value or right_value)
                continue
            left_value, right_value = left.get(name, fallback), right.get(name, fallback)
            scope[name] = left_value if left_value == right_value else bool(left_value or right_value)
        return False

    def _attrs_scan_compound(
        self,
        node: ast.stmt,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        # Compound statements may execute zero or multiple times. Keep all possibly
        # shared bindings when control flow makes a class-local name uncertain.
        if isinstance(node, ast.For | ast.AsyncFor | ast.While):
            return self._attrs_scan_loop(node, scope, field, module, globals_)
        if isinstance(node, ast.Match):
            return self._attrs_scan_match(node, scope, field, module, globals_)
        if isinstance(node, ast.With | ast.AsyncWith):
            return self._attrs_scan_with(node, scope, field, module, globals_)
        if isinstance(node, ast.Try | ast.TryStar):
            return self._attrs_scan_try(node, scope, field, module, globals_)
        return False

    def _attrs_scan_try(
        self,
        node: ast.Try | ast.TryStar,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        main, possible = scope.copy(), scope.copy()
        self._attrs_exception_states.append(possible)
        try:
            current_globals = main if scope is globals_ else globals_
            if self._attrs_scan_body(node.body, main, field, module, current_globals):
                return True
        finally:
            self._attrs_exception_states.pop()
        current_globals = main if scope is globals_ else globals_
        if self._attrs_scan_body(node.orelse, main, field, module, current_globals):
            return True
        branches = [main]
        for handler in node.handlers:
            branch = possible.copy()
            current_globals = branch if scope is globals_ else globals_
            if self._attrs_scan_handler(handler, branch, field, module, current_globals):
                return True
            branches.append(branch)
            if isinstance(node, ast.TryStar):
                self._attrs_join_scope(possible, branch)
        joined = possible.copy()
        for branch in branches:
            self._attrs_join_scope(joined, branch)
        current_globals = joined if scope is globals_ else globals_
        if self._attrs_scan_body(node.finalbody, joined, field, module, current_globals):
            return True
        self._attrs_apply_final_bindings(scope, branches, joined, node.finalbody)
        self._attrs_continue_finally((node.finalbody, branches), scope, field, module, globals_=globals_)
        return False

    def _attrs_continue_finally(
        self,
        paths: tuple[list[ast.stmt], list[dict[str, bool | _Module]]],
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        *,
        globals_: dict[str, bool | _Module],
    ) -> None:
        body, branches = paths
        identity = self._attrs_identity_writes(body)
        for name in identity:
            scope[name] = False
            for branch in branches:
                self._attrs_join_scope(scope, {name: branch.get(name, False)})
        if all(
            isinstance(statement, ast.Assign | ast.AnnAssign | ast.Import | ast.ImportFrom | ast.Pass)
            and not any(self._attrs_eager_bindings(statement))
            for statement in body
        ):
            scope.clear()
            scope.update(branches[0])
            for branch in branches[1:]:
                self._attrs_join_scope(scope, branch)
            current_globals = scope if scope is globals_ else globals_
            for statement in body:
                self._attrs_bind_class(statement, scope, field, module, current_globals)

    def _attrs_identity_writes(self, body: list[ast.stmt]) -> set[str]:
        written, _ = self._attrs_written_names(body)
        candidates = written.copy()
        pending: list[ast.AST] = list(body)
        while pending:
            item = pending.pop()
            if isinstance(item, ast.Import | ast.ImportFrom):
                candidates -= {
                    alias.asname or (alias.name.split(".")[0] if isinstance(item, ast.Import) else alias.name)
                    for alias in item.names
                }
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                candidates.discard(item.name)
            if isinstance(item, ast.Assign) and isinstance(item.value, ast.Name):
                names = {
                    target.id for target in item.targets if isinstance(target, ast.Name) and target.id == item.value.id
                }
                candidates -= {target.id for target in item.targets if isinstance(target, ast.Name)} - names
                continue
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store | ast.Del):
                candidates.discard(item.id)
            pending.extend(self._attrs_eager_children(item))
        return candidates

    def _attrs_apply_final_bindings(
        self,
        scope: dict[str, bool | _Module],
        branches: list[dict[str, bool | _Module]],
        joined: dict[str, bool | _Module],
        body: list[ast.stmt],
    ) -> None:
        scope.clear()
        scope.update(branches[0])
        for branch in branches[1:]:
            self._attrs_join_scope(scope, branch)
        written, definite = self._attrs_written_names(body)
        for name in written:
            if name in definite:
                scope[name] = joined.get(name, False)
            elif name in joined:
                self._attrs_join_scope(scope, {name: joined[name]})

    def _attrs_written_names(self, body: list[ast.stmt]) -> tuple[set[str], set[str]]:
        written: set[str] = set()

        def visit(item: ast.AST) -> None:
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store | ast.Del):
                written.add(item.id)
            if isinstance(item, ast.Import | ast.ImportFrom):
                written.update(
                    alias.asname or (alias.name.split(".")[0] if isinstance(item, ast.Import) else alias.name)
                    for alias in item.names
                )
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                written.add(item.name)
            if isinstance(item, ast.comprehension):
                for child in [item.iter, *item.ifs]:
                    visit(child)
            else:
                for child in self._attrs_eager_children(item):
                    visit(child)

        for statement in body:
            visit(statement)
        return written, self._attrs_definite_writes(body)

    @staticmethod
    def _attrs_definite_writes(body: list[ast.stmt]) -> set[str]:
        definite: set[str] = set()
        for statement in body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    definite.update(
                        item.id
                        for item in ast.walk(target)
                        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                    )
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                definite.update(
                    item.id
                    for item in ast.walk(statement.target)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                )
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.NamedExpr):
                definite.add(statement.value.target.id)
            elif isinstance(statement, ast.Delete):
                definite.update(
                    item.id
                    for item in ast.walk(statement)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Del)
                )
            elif isinstance(statement, ast.Import | ast.ImportFrom):
                definite.update(
                    alias.asname or (alias.name.split(".")[0] if isinstance(statement, ast.Import) else alias.name)
                    for alias in statement.names
                )
            elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                definite.add(statement.name)
        return definite

    def _attrs_eager_bindings(self, item: ast.AST) -> Iterable[ast.NamedExpr]:
        for child in self._attrs_eager_children(item):
            yield from self._attrs_eager_bindings(child)
        if isinstance(item, ast.NamedExpr):
            yield item

    # Scope and short-circuit cases must each choose their own eager children.
    def _attrs_eager_children(self, item: ast.AST) -> list[ast.AST]:  # ruff: ignore[complex-structure, too-many-return-statements]
        if isinstance(item, ast.GeneratorExp):
            return [item.generators[0].iter]
        if isinstance(item, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
            header: list[ast.AST] = [
                *item.args.defaults,
                *(value for value in item.args.kw_defaults if value is not None),
            ]
            if not isinstance(item, ast.Lambda):
                header.extend(item.decorator_list)
            return header
        if isinstance(item, ast.ClassDef):
            return [*item.decorator_list, *item.bases, *item.keywords]
        if isinstance(item, ast.IfExp):
            return [item.test, *self._attrs_expression_branches(item)]
        if isinstance(item, ast.BoolOp):
            values: list[ast.AST] = []
            for value in item.values:
                values.append(value)
                if isinstance(value, ast.Constant) and bool(value.value) == isinstance(item.op, ast.Or):
                    break
            return values
        if isinstance(item, ast.If):
            if isinstance(item.test, ast.Constant):
                return [item.test, *(item.body if bool(item.test.value) else item.orelse)]
            return [item.test, *item.body, *item.orelse]
        if isinstance(item, ast.While) and isinstance(item.test, ast.Constant) and not bool(item.test.value):
            return [item.test, *item.orelse]
        return (
            []
            if isinstance(item, ast.stmt)
            and not isinstance(
                item,
                ast.Expr
                | ast.Assign
                | ast.AnnAssign
                | ast.For
                | ast.AsyncFor
                | ast.While
                | ast.With
                | ast.AsyncWith
                | ast.Try
                | ast.TryStar
                | ast.Match,
            )
            else list(ast.iter_child_nodes(item))
        )

    def _attrs_scan_handler(
        self,
        handler: ast.ExceptHandler,
        branch: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        if handler.type is not None and self._attrs_scan_eager(handler.type, branch, field, module, globals_):
            return True
        if handler.name is not None:
            branch[handler.name] = False
        found = self._attrs_scan_body(handler.body, branch, field, module, globals_)
        if handler.name is not None:
            branch[handler.name] = False
        return found

    def _attrs_scan_loop(
        self,
        node: ast.For | ast.AsyncFor | ast.While,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        header = node.iter if isinstance(node, ast.For | ast.AsyncFor) else node.test
        before = scope.copy()
        loop_bindings = scope.copy()
        if isinstance(node, ast.For | ast.AsyncFor):
            before_globals = before if scope is globals_ else globals_
            self._attrs_bind_loop(node, (before, loop_bindings), field, module, globals_=before_globals)
        if self._attrs_scan_eager(header, scope, field, module, globals_):
            return True
        if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and not bool(node.test.value):
            return self._attrs_scan_body(node.orelse, scope, field, module, globals_)
        branch = scope.copy()
        while True:
            previous = branch.copy()
            iteration = branch.copy()
            branch_globals = iteration if scope is globals_ else globals_
            if isinstance(node, ast.For | ast.AsyncFor):
                iteration.update({
                    target.id: loop_bindings[target.id]
                    for target in ast.walk(node.target)
                    if isinstance(target, ast.Name)
                })
            else:
                if self._attrs_scan_eager(node.test, iteration, field, module, branch_globals):
                    return True
                self._attrs_join_scope(branch, iteration)
            self._attrs_record_exception_state(iteration)
            if self._attrs_scan_body(node.body, iteration, field, module, branch_globals):
                return True
            self._attrs_join_scope(branch, iteration)
            if branch == previous:
                break
        branch_globals = branch if scope is globals_ else globals_
        if self._attrs_scan_body(node.orelse, branch, field, module, branch_globals):
            return True
        self._attrs_join_scope(scope, branch)
        return False

    def _attrs_scan_match(
        self,
        node: ast.Match,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        if self._attrs_scan_eager(node.subject, scope, field, module, globals_):
            return True
        branches = []
        failed = scope.copy()
        for case in node.cases:
            branch = failed.copy()
            capture = not self._attrs_known_fresh(node.subject, module)
            for pattern in ast.walk(case.pattern):
                if isinstance(pattern, ast.MatchAs | ast.MatchStar) and pattern.name is not None:
                    branch[pattern.name] = capture
                elif isinstance(pattern, ast.MatchMapping) and pattern.rest is not None:
                    branch[pattern.rest] = capture
            branch_globals = branch if scope is globals_ else globals_
            if case.guard is not None:
                if self._attrs_scan_eager(case.guard, branch, field, module, branch_globals):
                    return True
                self._attrs_join_scope(failed, branch)
            if self._attrs_scan_body(case.body, branch, field, module, branch_globals):
                return True
            branches.append(branch)
        for branch in [failed, *branches]:
            self._attrs_join_scope(scope, branch)
        return False

    def _attrs_scan_with(
        self,
        node: ast.With | ast.AsyncWith,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool:
        for context in node.items:
            if self._attrs_scan_eager(context.context_expr, scope, field, module, globals_):
                return True
            if context.optional_vars is not None:
                for target in ast.walk(context.optional_vars):
                    if isinstance(target, ast.Name):
                        scope[target.id] = True
        possible = scope.copy()
        self._attrs_exception_states.append(possible)
        try:
            found = self._attrs_scan_body(node.body, scope, field, module, globals_)
        finally:
            self._attrs_exception_states.pop()
        if not all(
            isinstance(item.context_expr, ast.Call)
            and self._external_name(item.context_expr.func, module) == "contextlib.nullcontext"
            for item in node.items
        ):
            self._attrs_join_scope(scope, possible)
        return found

    def _attrs_bind_loop(
        self,
        node: ast.For | ast.AsyncFor,
        scopes: tuple[dict[str, bool | _Module], dict[str, bool | _Module]],
        field: tuple[ast.Call, _Module],
        module: _Module,
        *,
        globals_: dict[str, bool | _Module],
    ) -> None:
        scope, branch_scope = scopes
        literal = isinstance(node.iter, ast.List | ast.Tuple | ast.Set)
        values = node.iter.elts if literal else [node.iter]
        targets = {item.id for item in ast.walk(node.target) if isinstance(item, ast.Name)}
        branch_scope.update(dict.fromkeys(targets, False))
        if not literal or (
            isinstance(node.target, ast.Tuple | ast.List)
            and any(not isinstance(value, ast.Tuple | ast.List) for value in values)
        ):
            branch_scope.update(dict.fromkeys(targets, True))
            return
        for value in values:
            bindings = scope.copy()
            self._attrs_bind_target((node.target, value), bindings, field, module, globals_=globals_)
            for target in ast.walk(node.target):
                if isinstance(target, ast.Name) and bool(bindings.get(target.id)):
                    branch_scope[target.id] = bindings[target.id]
            self._attrs_scan_eager(value, scope, field, module, globals_)

    @staticmethod
    def _attrs_loop_default(body: Sequence[ast.AST], targets: set[str]) -> bool:
        pending: list[ast.AST] = list(body)
        while pending:
            item = pending.pop()
            if (
                isinstance(item, ast.Attribute)
                and item.attr == "default"
                and isinstance(item.value, ast.Name)
                and item.value.id in targets
            ):
                return True
            if isinstance(item, ast.GeneratorExp):
                pending.append(item.generators[0].iter)
            elif isinstance(item, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
                pending.extend([] if isinstance(item, ast.Lambda) else item.decorator_list)
                pending.extend(item.args.defaults)
                pending.extend(value for value in item.args.kw_defaults if value is not None)
            else:
                pending.extend(ast.iter_child_nodes(item))
        return False

    def _attrs_class_value(
        self,
        expr: ast.expr,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool | _Module:
        while isinstance(expr, ast.NamedExpr):
            expr = expr.value
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Tuple | ast.List | ast.Name):
            return self._attrs_indexed_value(expr, scope, field, module, globals_)
        alternatives = self._attrs_expression_branches(expr)
        if isinstance(expr, ast.Starred) or alternatives != [expr]:
            return isinstance(expr, ast.Starred) or any(
                self._attrs_class_value(value, scope, field, module, globals_) for value in alternatives
            )
        if isinstance(expr, ast.Name) and (expr.id in scope or expr.id in globals_):
            return scope.get(expr.id, globals_.get(expr.id, False))
        if self._attrs_is_shared(expr, scope, field, module, globals_) or (
            isinstance(expr, ast.Call) and not self._attrs_known_fresh(expr, module)
        ):
            return True
        try:
            ref = self._ref(expr, module)
        except RejectError:
            return False
        return ref.module if isinstance(ref, _ModuleRef) else False

    def _attrs_indexed_value(
        self,
        expr: ast.Subscript,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool | _Module:
        if not isinstance(expr.value, ast.Name):
            return self._attrs_subscript_value(expr, scope, field, module, globals_)
        try:
            index = ast.literal_eval(expr.slice)
        except ValueError, TypeError:
            index = None
        key = f"\0slot:{expr.value.id}:{index}"
        return scope.get(key, scope.get(expr.value.id, globals_.get(expr.value.id, False)))

    def _attrs_subscript_value(
        self,
        expr: ast.Subscript,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> bool | _Module:
        selected = self._attrs_literal_slot(expr)
        if selected is None:
            values = (
                self._attrs_flatten_values(expr.value.elts) if isinstance(expr.value, ast.Tuple | ast.List) else None
            )
            if values is None:
                return True
            state = scope.copy()
            state_globals = state if scope is globals_ else globals_
            result: dict[str, bool | _Module] = {}
            for value in values:
                self._attrs_join_scope(
                    result, {"value": self._attrs_class_value(value, state, field, module, state_globals)}
                )
                self._attrs_scan_eager(value, state, field, module, state_globals)
            return result.get("value", False)
        values, index = selected
        state = scope.copy()
        state_globals = state if scope is globals_ else globals_
        for value in values[:index]:
            self._attrs_scan_eager(value, state, field, module, state_globals)
        return self._attrs_class_value(values[index], state, field, module, state_globals)

    def _attrs_bind_import(
        self,
        node: ast.Import | ast.ImportFrom,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
    ) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                scope[alias.asname or alias.name.split(".")[0]] = self._find(module, 0, alias.name) or False
        else:
            origin = self._find(module, node.level, node.module)
            for alias in node.names:
                name = alias.asname or alias.name
                scope[name] = origin is not None and self._attrs_is_shared(
                    ast.Name(id=alias.name, ctx=ast.Load()), {}, field, origin, {}
                )
                if origin is not None and not scope[name]:
                    with suppress(RejectError):
                        ref = self._ref(ast.Name(id=alias.name, ctx=ast.Load()), origin)
                        if isinstance(ref, _ModuleRef):
                            scope[name] = ref.module
                    if alias.name not in origin.bindings:
                        with suppress(RejectError):
                            scope[name] = self._submodule(origin, alias.name).module

    def _attrs_bind_class(
        self,
        node: ast.stmt,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> None:
        if isinstance(node, ast.Import | ast.ImportFrom):
            self._attrs_bind_import(node, scope, field, module)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scope.pop(target.id, None)
                    self._attrs_forget_carrier(target.id, scope)
        elif isinstance(node, ast.Assign):
            self._attrs_bind_assignment(node, scope, field, module, globals_)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            self._attrs_forget_carrier(node.target.id, scope)
            scope[node.target.id] = self._attrs_class_value(node.value, scope, field, module, globals_)
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            scope[node.name] = False

    def _attrs_bind_assignment(
        self,
        node: ast.Assign,
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        globals_: dict[str, bool | _Module],
    ) -> None:
        sources = scope.copy()
        for target in node.targets:
            bindings = sources.copy()
            self._attrs_bind_target((target, node.value), bindings, field, module, globals_=globals_)
            for item in ast.walk(target):
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store) and item.id in bindings:
                    self._attrs_copy_binding(item.id, node.value, scope, sources, bindings)

    @staticmethod
    def _attrs_copy_binding(
        dest: str,
        expression: ast.expr,
        scope: dict[str, bool | _Module],
        sources: dict[str, bool | _Module],
        bindings: dict[str, bool | _Module],
    ) -> None:
        _Resolver._attrs_forget_carrier(dest, scope)
        scope[dest] = bindings[dest]
        if isinstance(expression, ast.Name) and expression.id != dest:
            source = expression.id
            for key, value in sources.items():
                if key.startswith(f"\0slot:{source}:"):
                    scope[key.replace(f"\0slot:{source}:", f"\0slot:{dest}:", 1)] = value
            if any(key.startswith(f"\0slot:{source}:") for key in sources) or f"\0carrier:{source}" in sources:
                scope[f"\0alias:{dest}:{source}"] = True
                scope[f"\0carrier:{dest}"] = True
        scope.update({
            key: value
            for key, value in bindings.items()
            if key.startswith(f"\0slot:{dest}:") or key == f"\0carrier:{dest}"
        })

    @staticmethod
    def _attrs_flatten_values(values: list[ast.expr]) -> list[ast.expr] | None:
        flattened: list[ast.expr] = []
        for value in values:
            if isinstance(value, ast.Starred):
                if not isinstance(value.value, ast.Tuple | ast.List):
                    return None
                nested = _Resolver._attrs_flatten_values(value.value.elts)
                if nested is None:
                    return None
                flattened.extend(nested)
            else:
                flattened.append(value)
        return flattened

    @staticmethod
    def _attrs_literal_slot(expr: ast.Subscript) -> tuple[list[ast.expr], int] | None:
        if not isinstance(expr.value, ast.Tuple | ast.List):
            return None
        values = _Resolver._attrs_flatten_values(expr.value.elts)
        try:
            index = ast.literal_eval(expr.slice)
        except ValueError, TypeError:
            return None
        if values is None or not isinstance(index, int) or not -len(values) <= index < len(values):
            return None
        return values, index % len(values)

    @staticmethod
    def _attrs_unpack_pairs(targets: list[ast.expr], values: list[ast.expr]) -> list[tuple[ast.expr, ast.expr]]:
        flattened = _Resolver._attrs_flatten_values(values)
        if flattened is None:
            uncertain = ast.Starred(value=ast.Constant(value=None), ctx=ast.Load())
            return [(target, uncertain) for target in targets]
        values = flattened
        star = next((index for index, item in enumerate(targets) if isinstance(item, ast.Starred)), None)
        if star is None:
            return list(zip(targets, values, strict=False))
        suffix = len(targets) - star - 1
        end = len(values) - suffix
        starred = targets[star]
        if not isinstance(starred, ast.Starred):
            return []
        return [
            *zip(targets[:star], values[:star], strict=False),
            (starred.value, ast.Tuple(elts=values[star:end], ctx=ast.Load())),
            *zip(targets[star + 1 :], values[end:], strict=False),
        ]

    def _attrs_bind_target(
        self,
        pair: tuple[ast.expr, ast.expr],
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        *,
        globals_: dict[str, bool | _Module],
    ) -> None:
        target, value = pair
        sources = scope.copy()
        if isinstance(target, ast.Name):
            self._attrs_bind_name((target, value), scope, field, module, globals_=globals_)
        elif isinstance(target, ast.Tuple | ast.List) and not isinstance(value, ast.Tuple | ast.List):
            if not self._attrs_known_fresh(value, module):
                scope.update({item.id: True for item in ast.walk(target) if isinstance(item, ast.Name)})
        elif isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Tuple | ast.List):
            for item, source in self._attrs_unpack_pairs(target.elts, value.elts):
                bindings = sources.copy()
                self._attrs_bind_target((item, source), bindings, field, module, globals_=globals_)
                for name in ast.walk(item):
                    if isinstance(name, ast.Name) and name.id in bindings:
                        scope[name.id] = bindings[name.id]
                        scope.update({
                            key: value
                            for key, value in bindings.items()
                            if key.startswith(f"\0slot:{name.id}:") or key == f"\0carrier:{name.id}"
                        })

    def _attrs_bind_name(
        self,
        pair: tuple[ast.Name, ast.expr],
        scope: dict[str, bool | _Module],
        field: tuple[ast.Call, _Module],
        module: _Module,
        *,
        globals_: dict[str, bool | _Module],
    ) -> None:
        target, value = pair
        sources = scope.copy()
        for key in list(scope):
            if key.startswith(f"\0slot:{target.id}:"):
                del scope[key]
        if not isinstance(value, ast.Tuple | ast.List):
            scope[target.id] = self._attrs_class_value(value, sources, field, module, globals_)
            return
        scope[f"\0carrier:{target.id}"] = True
        values = self._attrs_flatten_values(value.elts)
        scope[target.id] = values is None or any(
            self._attrs_class_value(item, sources, field, module, globals_) for item in values
        )
        for index, item in enumerate(values or []):
            binding = self._attrs_class_value(item, sources, field, module, globals_)
            scope[f"\0slot:{target.id}:{index}"] = binding
            scope[f"\0slot:{target.id}:{index - len(values or [])}"] = binding

    def _attrs_field_binding(
        self, value: ast.expr, module: _Module, seen: set[tuple[Path, str]]
    ) -> tuple[ast.expr, _Module] | None:
        if isinstance(value, ast.Attribute):
            try:
                target = self._ref(value.value, module)
            except RejectError:
                return None
            if not isinstance(target, _ModuleRef):
                return None
            module, value = target.module, ast.Name(id=value.attr, ctx=ast.Load())
        if not isinstance(value, ast.Name):
            return None
        key = (module.path, value.id)
        bindings = module.bindings.get(value.id, [])
        if key in seen or len(bindings) != 1 or bindings[0].tc:
            return None
        seen.add(key)
        binding = bindings[0]
        result: tuple[ast.expr, _Module] | None = None
        if binding.kind == "from":
            target = self._find(module, binding.level, binding.module)
            if target is not None:
                result = ast.Name(id=binding.name, ctx=ast.Load()), target
        elif binding.kind == "call" and isinstance(binding.node, ast.Call):
            result = binding.node, module
        elif binding.kind == "alias" and binding.expr is not None and not binding.type_stmt:
            result = binding.expr, module
        return result

    def _attrs_unannotated_fields(self, node: ast.ClassDef, klass: _Class) -> None:
        for stmt in _plain_statements(node.body):
            if not isinstance(stmt, ast.Assign):
                continue
            field = self._attrs_field_call(stmt.value, klass.module)
            if field is None:
                try:
                    ast.literal_eval(stmt.value)
                except ValueError, TypeError, SyntaxError:
                    msg = f"`{klass.name}` has an untraceable attrs collection declaration"
                    raise _invalid(msg) from None
                continue
            call, _ = field
            init = next((option for option in call.keywords if option.arg == "init"), None)
            if init is not None and not self._attrs_bool(init, "unannotated field init"):
                self._attrs_check_fallback(node, klass)
                continue
            msg = f"`{klass.name}` has an unannotated attrs field"
            raise _invalid(msg)

    def _attrs_check_fallback(self, node: ast.ClassDef, klass: _Class) -> None:
        # An unannotated field switches attrs to explicit-field collection, even when init=False.
        for stmt, _ in self._annotated(node):
            if self._special(stmt.annotation, klass.module) == "ClassVar":
                continue
            if stmt.value is not None and self._attrs_field_call(stmt.value, klass.module) is not None:
                continue
            msg = f"`{klass.name}` has an attrs collection fallback with implicit fields"
            raise _invalid(msg)

    @staticmethod
    def _attrs_check_bases(klass: _Class, bases: list[_Info]) -> None:
        owners: dict[str, _Class | None] = {}
        for base in bases:
            for key, source in base.fields.items():
                if source is None:
                    continue
                if key in owners and owners[key] is not source.owner:
                    msg = f"`{klass.name}` inherits ambiguous attrs field `{key}`"
                    raise _invalid(msg)
                owners[key] = source.owner

    def _attrs_field(self, stmt: ast.AnnAssign, key: str, klass: _Class) -> _Source | None:
        if self._special(stmt.annotation, klass.module) == "ClassVar":
            return None
        source = self._source(stmt, key, klass)
        source.attrs_alias = key.lstrip("_")
        value = stmt.value
        field = self._attrs_field_call(value, klass.module) if value is not None else None
        if field is not None:
            call, module = field
            if call.args:
                msg = f"field `{key}` has unsupported positional options"
                raise _invalid(msg)
            for option in call.keywords:
                if not self._attrs_field_option(source, option, module):
                    return None
        elif value is not None:
            source.default = self._attrs_default(value, klass.module)
        return source

    def _attrs_field_option(self, source: _Source, option: ast.keyword, module: _Module) -> bool:
        key = source.key
        if option.arg is None:
            msg = f"field `{key}` uses dynamic options"
            raise _invalid(msg)
        if option.arg == "init":
            return self._attrs_bool(option, f"field `{key}` init")
        if option.arg == "default":
            source.default |= self._attrs_default(option.value, module)
        elif option.arg == "factory":
            source.default |= self._attrs_factory(option.value, module)
        elif option.arg == "alias":
            if isinstance(option.value, ast.Constant) and option.value.value is None:
                return True
            if not isinstance(option.value, ast.Constant) or not isinstance(option.value.value, str):
                msg = f"field `{key}` has a dynamic alias"
                raise _invalid(msg)
            source.attrs_alias = option.value.value
        elif option.arg == "converter":
            if not isinstance(option.value, ast.Constant) or option.value.value is not None:
                source.converter = option.value
                source.converter_module = module
        else:
            self._attrs_field_other_option(option, key)
        return True

    def _attrs_factory(self, expr: ast.expr, module: _Module) -> bool:
        if isinstance(expr, ast.Constant) and expr.value is None:
            return False
        if isinstance(expr, ast.Lambda):
            return True
        if self._attrs_converter_node(expr, module) is not None:
            return True
        try:
            target = self._ref(expr, module)
        except RejectError:
            target = None
        if isinstance(target, _Class) and self._attrs_factory_class(target, set()):
            return True
        if (
            isinstance(target, _External)
            and target.qualname.startswith("builtins.")
            and callable(getattr(builtins, target.qualname.removeprefix("builtins."), None))
        ):
            return True
        msg = "an attrs field has an indeterminate factory"
        raise _invalid(msg)

    def _attrs_factory_class(self, klass: _Class, seen: set[ClassKey]) -> bool:
        key = _class_key(klass)
        if key in seen or not isinstance(klass.node, ast.ClassDef):
            return False
        node = klass.node
        if node.decorator_list or node.keywords:
            return False
        seen.add(key)
        for base in node.bases:
            try:
                target = self._ref(base, klass.module)
            except RejectError:
                return False
            if isinstance(target, _Class):
                if not self._attrs_factory_class(target, seen):
                    return False
            elif not isinstance(target, _External) or target.qualname != "builtins.object":
                return False
        seen.remove(key)
        return True

    @staticmethod
    def _attrs_field_other_option(option: ast.keyword, key: str) -> None:
        if option.arg not in {"validator", "repr", "hash", "metadata", "kw_only", "eq", "order", "on_setattr"}:
            msg = f"field `{key}` has an unsupported option `{option.arg}`"
            raise _invalid(msg)
        if option.arg in {"kw_only", "repr", "hash", "eq", "order"} and not isinstance(option.value, ast.Constant):
            msg = f"field `{key}` has a dynamic `{option.arg}` option"
            raise _invalid(msg)

    def _attrs_default_hooks(self, info: _Info, node: ast.ClassDef, klass: _Class) -> None:
        _Resolver._attrs_reject_nested_hooks(info, node, klass)
        self._attrs_reject_local_loop_aliases(info, node, klass)
        for stmt in node.body:
            if not isinstance(stmt, ast.FunctionDef):
                continue
            for decorator in stmt.decorator_list:
                if (
                    isinstance(decorator, ast.Attribute)
                    and decorator.attr == "default"
                    and isinstance(decorator.value, ast.Name)
                ):
                    source = info.fields.get(_mangle(node.name, decorator.value.id))
                    if source is None:
                        msg = f"`{klass.name}` has an untraceable attrs default"
                        raise _invalid(msg)
                    info.fields[source.key] = replace(source, default=True)

    @staticmethod
    def _attrs_reject_nested_hooks(info: _Info, node: ast.ClassDef, klass: _Class) -> None:
        pending: list[ast.AST] = []
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                pending.extend(
                    decorator
                    for decorator in stmt.decorator_list
                    if not (
                        isinstance(stmt, ast.FunctionDef)
                        and isinstance(decorator, ast.Attribute)
                        and decorator.attr == "default"
                        and isinstance(decorator.value, ast.Name)
                    )
                )
                pending.extend(stmt.args.defaults)
                pending.extend(item for item in stmt.args.kw_defaults if item is not None)
            else:
                pending.append(stmt)
        while pending:
            item = pending.pop()
            if (
                isinstance(item, ast.Attribute)
                and item.attr == "default"
                and isinstance(item.value, ast.Name)
                and _mangle(node.name, item.value.id) in info.fields
            ):
                msg = f"`{klass.name}` has an unsupported attrs default registration"
                raise _invalid(msg)
            if isinstance(item, ast.GeneratorExp):
                pending.append(item.generators[0].iter)
            elif isinstance(item, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
                decorators = [] if isinstance(item, ast.Lambda) else item.decorator_list
                pending.extend(decorators)
                pending.extend(item.args.defaults)
                pending.extend(value for value in item.args.kw_defaults if value is not None)
            elif isinstance(item, ast.ClassDef):
                pending.extend([*item.decorator_list, *item.bases, *item.keywords])
            else:
                pending.extend(ast.iter_child_nodes(item))

    def _attrs_known_fresh(self, expr: ast.expr, module: _Module) -> bool:
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "object":
            return not self._attrs_object_shadowed(expr, module)
        return isinstance(expr, ast.Call) and (
            self._external_name(expr.func, module)
            in {
                "builtins.object",
                "attrs.field",
                "attr.field",
                "attr.ib",
                "attr.attrib",
            }
        )

    def _attrs_object_shadowed(self, expr: ast.expr, module: _Module) -> bool:
        key = (module.path, id(expr))
        if key in self._attrs_object_shadows:
            return self._attrs_object_shadows[key]
        shadowed = "object" in module.bindings
        if module.tree is not None and not shadowed:
            shadowed = any(
                binding.target.id == "object"
                for statement in module.tree.body
                for binding in self._attrs_eager_bindings(statement)
            )
        if module.tree is not None and not shadowed:
            parents = {child: parent for parent in ast.walk(module.tree) for child in ast.iter_child_nodes(parent)}
            current: ast.AST = expr
            while current in parents:
                current = parents[current]
                if isinstance(current, ast.ClassDef):
                    pending: list[ast.AST] = list(current.body)
                    while pending and not shadowed:
                        item = pending.pop()
                        shadowed = (
                            (isinstance(item, ast.Name) and item.id == "object" and isinstance(item.ctx, ast.Store))
                            or (
                                isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                                and item.name == "object"
                            )
                            or (isinstance(item, ast.alias) and (item.asname or item.name.split(".")[0]) == "object")
                        )
                        if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
                            pending.extend(ast.iter_child_nodes(item))
                    break
        self._attrs_object_shadows[key] = shadowed
        return shadowed

    @staticmethod
    # The expression forms have different static branch selection rules.
    def _attrs_expression_branches(expr: ast.expr) -> list[ast.expr]:  # ruff: ignore[complex-structure, too-many-return-statements]
        if isinstance(expr, ast.NamedExpr):
            return [expr.value]
        if isinstance(expr, ast.IfExp):
            if isinstance(expr.test, ast.Constant):
                return [expr.body if bool(expr.test.value) else expr.orelse]
            return [expr.body, expr.orelse]
        if isinstance(expr, ast.BoolOp):
            values = []
            for value in expr.values:
                if not isinstance(value, ast.Constant) or bool(value.value) == isinstance(expr.op, ast.Or):
                    values.append(value)
                if isinstance(value, ast.Constant) and bool(value.value) == isinstance(expr.op, ast.Or):
                    break
            return values or [expr.values[-1]]
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Tuple | ast.List):
            values = _Resolver._attrs_flatten_values(expr.value.elts)
            try:
                index = ast.literal_eval(expr.slice)
            except ValueError, TypeError:
                index = None
            if values is not None and isinstance(index, int):
                with suppress(IndexError):
                    return [values[index]]
            if values is None:
                return [ast.Starred(value=expr.value, ctx=ast.Load())]
        return [expr]

    def _attrs_local_loop_bindings(
        self, target: ast.expr, iterable: ast.expr, aliases: set[str], class_name: str, module: _Module
    ) -> set[str]:
        bound: set[str] = set()
        if not isinstance(iterable, ast.Tuple | ast.List | ast.Set):
            return {_mangle(class_name, item.id) for item in ast.walk(target) if isinstance(item, ast.Name)}
        values = iterable.elts
        pairs = [(target, value) for value in values]

        def shared(source: ast.expr) -> bool:
            alternatives = self._attrs_expression_branches(source)
            if alternatives != [source]:
                return any(shared(value) for value in alternatives)
            if isinstance(source, ast.Starred):
                return True
            return (isinstance(source, ast.Name) and _mangle(class_name, source.id) in aliases) or (
                isinstance(source, ast.Call) and not self._attrs_known_fresh(source, module)
            )

        while pairs:
            dest, source = pairs.pop()
            if isinstance(dest, ast.Name) and shared(source):
                bound.add(_mangle(class_name, dest.id))
            elif isinstance(dest, ast.Tuple | ast.List):
                if isinstance(source, ast.Tuple | ast.List):
                    pairs.extend(self._attrs_unpack_pairs(dest.elts, source.elts))
                elif not self._attrs_known_fresh(source, module):
                    bound.update(_mangle(class_name, item.id) for item in ast.walk(dest) if isinstance(item, ast.Name))
        return bound

    # These bounded cases share binding state and must be processed in evaluation order.
    def _attrs_reject_local_loop_aliases(self, info: _Info, node: ast.ClassDef, klass: _Class) -> None:  # ruff: ignore[complex-structure, too-many-statements]
        fields = set(info.fields)
        class_name = node.name
        exception_states: list[set[str]] = []

        def name(expr: ast.expr) -> str | None:
            return _mangle(class_name, expr.id) if isinstance(expr, ast.Name) else None

        def shared(expr: ast.expr, aliases: set[str]) -> bool:  # ruff: ignore[complex-structure, too-many-return-statements]
            if isinstance(expr, ast.Tuple | ast.List):
                return any(shared(value, aliases) for value in expr.elts)
            if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
                try:
                    index = ast.literal_eval(expr.slice)
                except ValueError, TypeError:
                    index = None
                slot = f"\0slot:{name(expr.value)}:{index}"
                if f"{slot}:known" in aliases:
                    return slot in aliases
                return name(expr.value) in aliases
            if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Tuple | ast.List):
                selected = self._attrs_literal_slot(expr)
                if selected is None:
                    values = self._attrs_flatten_values(expr.value.elts)
                    if values is None:
                        return True
                    state = aliases.copy()
                    result = False
                    for value in values:
                        result |= shared(value, state)
                        scan(value, state)
                    return result
                values, index = selected
                state = aliases.copy()
                for value in values[:index]:
                    scan(value, state)
                return shared(values[index], state)
            alternatives = self._attrs_expression_branches(expr)
            if alternatives != [expr]:
                return any(shared(value, aliases) for value in alternatives)
            if isinstance(expr, ast.Starred):
                return True
            return name(expr) in aliases or (
                isinstance(expr, ast.Call) and not self._attrs_known_fresh(expr, klass.module)
            )

        def mutate(carrier: str, aliases: set[str], *, value: bool) -> None:
            related = {carrier}
            while True:
                before = related.copy()
                for key in aliases:
                    if key.startswith("\0alias:"):
                        _, left, right = key.split(":", 2)
                        if left in related or right in related:
                            related.update((left, right))
                if before == related:
                    break
            possible = value or bool(related & aliases)
            for current in related:
                aliases.difference_update(key for key in aliases.copy() if key.startswith(f"\0slot:{current}:"))
                if possible:
                    aliases.add(current)

        def copy_carrier(dest: str, value: ast.expr, aliases: set[str], sources: set[str]) -> None:
            if isinstance(value, ast.Name) and (source := name(value)) != dest and source is not None:
                for key in sources:
                    if key.startswith(f"\0slot:{source}:"):
                        aliases.add(key.replace(f"\0slot:{source}:", f"\0slot:{dest}:", 1))
                if self._attrs_carrier_tracked(source, sources):
                    aliases.add(f"\0alias:{dest}:{source}")
                    aliases.add(f"\0carrier:{dest}")

        def bind_slots(dest: str, value: ast.expr, aliases: set[str], sources: set[str]) -> None:
            if isinstance(value, ast.Tuple | ast.List):
                aliases.add(f"\0carrier:{dest}")
                values = self._attrs_flatten_values(value.elts)
                if values is None:
                    aliases.add(dest)
                for index, item in enumerate(values or []):
                    for offset in (index, index - len(values or [])):
                        slot = f"\0slot:{dest}:{offset}"
                        aliases.add(f"{slot}:known")
                        if shared(item, sources):
                            aliases.add(slot)

        def bind(target: ast.expr, value: ast.expr, aliases: set[str], sources: set[str]) -> None:
            if isinstance(target, ast.Name):
                dest = _mangle(class_name, target.id)
                is_shared = shared(value, sources)
                aliases.difference_update(key for key in aliases.copy() if key.startswith(f"\0slot:{dest}:"))
                aliases.difference_update(
                    key for key in aliases.copy() if key.startswith("\0alias:") and dest in key.split(":")[1:]
                )
                aliases.discard(dest)
                aliases.discard(f"\0carrier:{dest}")
                copy_carrier(dest, value, aliases, sources)
                if is_shared:
                    aliases.add(dest)
                bind_slots(dest, value, aliases, sources)
            elif isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Tuple | ast.List):
                for dest, source in self._attrs_unpack_pairs(target.elts, value.elts):
                    bind(dest, source, aliases, sources)
            elif isinstance(target, ast.Tuple | ast.List) and not self._attrs_known_fresh(value, klass.module):
                aliases.update(_mangle(class_name, item.id) for item in ast.walk(target) if isinstance(item, ast.Name))

        def scan_ifexp(item: ast.IfExp, aliases: set[str]) -> None:
            scan(item.test, aliases)
            branches = []
            for branch in self._attrs_expression_branches(item):
                state = aliases.copy()
                scan(branch, state)
                branches.append(state)
            aliases.clear()
            aliases.update(*branches)

        def loop_bindings(target: ast.expr, iterable: ast.expr, aliases: set[str]) -> set[str]:
            before = aliases.copy()
            if not isinstance(iterable, ast.Tuple | ast.List | ast.Set):
                return self._attrs_local_loop_bindings(target, iterable, before, class_name, klass.module)
            bound: set[str] = set()
            for value in iterable.elts:
                if isinstance(target, ast.Name):
                    if shared(value, before):
                        bound.add(_mangle(class_name, target.id))
                else:
                    one = ast.Tuple(elts=[value], ctx=ast.Load())
                    bound.update(self._attrs_local_loop_bindings(target, one, before, class_name, klass.module))
                scan(value, before)
            return bound

        def scan(item: ast.AST, aliases: set[str]) -> None:  # ruff: ignore[complex-structure, too-many-return-statements, too-many-branches, too-many-statements, too-many-locals]
            if isinstance(item, ast.GeneratorExp):
                scan(item.generators[0].iter, aliases)
                return
            if isinstance(item, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
                header = [*item.args.defaults, *(value for value in item.args.kw_defaults if value is not None)]
                if not isinstance(item, ast.Lambda):
                    header.extend(item.decorator_list)
                for child in header:
                    scan(child, aliases)
                return
            if isinstance(item, ast.ClassDef):
                for child in [*item.decorator_list, *item.bases, *item.keywords]:
                    scan(child, aliases)
                return
            if isinstance(item, ast.BoolOp):
                exits: list[set[str]] = []
                for value in item.values:
                    scan(value, aliases)
                    if isinstance(value, ast.Constant):
                        if bool(value.value) == isinstance(item.op, ast.Or):
                            break
                    else:
                        exits.append(aliases.copy())
                aliases.update(*exits)
                return
            if isinstance(item, ast.IfExp):
                scan_ifexp(item, aliases)
                return
            if isinstance(item, ast.NamedExpr):
                earlier = shared(item.value, aliases) if isinstance(item.value, ast.Subscript) else False
                scan(item.value, aliases)
                bind(item.target, item.value, aliases, aliases.copy())
                if isinstance(item.value, ast.Subscript):
                    aliases.discard(_mangle(class_name, item.target.id))
                    if earlier:
                        aliases.add(_mangle(class_name, item.target.id))
                for possible in exception_states:
                    possible.update(aliases)
                return
            if (
                isinstance(item, ast.Attribute)
                and item.attr == "default"
                and shared(item.value, aliases - fields if isinstance(item.value, ast.Name) else aliases)
            ):
                msg = f"`{klass.name}` has an unsupported attrs default registration"
                raise _invalid(msg)
            if isinstance(item, ast.ListComp | ast.SetComp | ast.DictComp):
                local = aliases.copy()
                for generator in item.generators:
                    bound = loop_bindings(generator.target, generator.iter, local)
                    scan(generator.iter, local)
                    local.difference_update(
                        _mangle(class_name, target.id)
                        for target in ast.walk(generator.target)
                        if isinstance(target, ast.Name)
                    )
                    local.update(bound)
                    for test in generator.ifs:
                        scan(test, local)
                for child in [item.key, item.value] if isinstance(item, ast.DictComp) else [item.elt]:
                    scan(child, local)
                return
            if isinstance(item, ast.For | ast.AsyncFor):
                bound = loop_bindings(item.target, item.iter, aliases)
                scan(item.iter, aliases)
                local = aliases.copy()
                while True:
                    previous = local.copy()
                    iteration = local.copy()
                    iteration.difference_update(
                        _mangle(class_name, target.id)
                        for target in ast.walk(item.target)
                        if isinstance(target, ast.Name)
                    )
                    iteration.update(bound)
                    for possible in exception_states:
                        possible.update(iteration)
                    body(item.body, iteration)
                    local.update(iteration)
                    if local == previous:
                        break
                body(item.orelse, local)
                aliases.update(local)
                return
            if isinstance(item, ast.While):
                scan(item.test, aliases)
                if isinstance(item.test, ast.Constant) and not bool(item.test.value):
                    body(item.orelse, aliases)
                    return
                local = aliases.copy()
                while True:
                    previous = local.copy()
                    iteration = local.copy()
                    scan(item.test, iteration)
                    local.update(iteration)
                    body(item.body, iteration)
                    local.update(iteration)
                    if local == previous:
                        break
                body(item.orelse, local)
                aliases.update(local)
                return
            if isinstance(item, ast.With | ast.AsyncWith):
                for context in item.items:
                    scan(context.context_expr, aliases)
                    if context.optional_vars is not None:
                        aliases.update(
                            _mangle(class_name, target.id)
                            for target in ast.walk(context.optional_vars)
                            if isinstance(target, ast.Name)
                        )
                possible = aliases.copy()
                exception_states.append(possible)
                try:
                    body(item.body, aliases)
                finally:
                    exception_states.pop()
                if not all(
                    isinstance(context.context_expr, ast.Call)
                    and self._external_name(context.context_expr.func, klass.module) == "contextlib.nullcontext"
                    for context in item.items
                ):
                    aliases.update(possible)
                return
            if isinstance(item, ast.Match):
                scan(item.subject, aliases)
                failed = aliases.copy()
                branches = []
                for case in item.cases:
                    branch = failed.copy()
                    capture = not self._attrs_known_fresh(item.subject, klass.module)
                    for pattern in ast.walk(case.pattern):
                        if isinstance(pattern, ast.MatchAs | ast.MatchStar) and pattern.name is not None:
                            key = _mangle(class_name, pattern.name)
                            branch.discard(key)
                            if capture:
                                branch.add(key)
                        elif isinstance(pattern, ast.MatchMapping) and pattern.rest is not None:
                            key = _mangle(class_name, pattern.rest)
                            branch.discard(key)
                            if capture:
                                branch.add(key)
                    if case.guard is not None:
                        scan(case.guard, branch)
                        failed.update(branch)
                    body(case.body, branch)
                    branches.append(branch)
                aliases.clear()
                aliases.update(failed, *branches)
                return
            if isinstance(item, ast.If):
                scan(item.test, aliases)
                if isinstance(item.test, ast.Constant):
                    body(item.body if bool(item.test.value) else item.orelse, aliases)
                    return
                left, right = aliases.copy(), aliases.copy()
                body(item.body, left)
                body(item.orelse, right)
                for key in left ^ right:
                    if key.startswith("\0slot:") and key.endswith(":known"):
                        left.discard(key)
                        right.discard(key)
                aliases.clear()
                aliases.update(left | right)
                return
            if isinstance(item, ast.Try | ast.TryStar):
                main, possible = aliases.copy(), aliases.copy()
                exception_states.append(possible)
                try:
                    body(item.body, main)
                finally:
                    exception_states.pop()
                body(item.orelse, main)
                branches = [main, *(possible.copy() for _ in item.handlers)]
                for handler, branch in zip(item.handlers, branches[1:], strict=True):
                    if isinstance(item, ast.TryStar):
                        branch.update(possible)
                    if handler.type is not None:
                        scan(handler.type, branch)
                    if handler.name is not None:
                        branch.discard(_mangle(class_name, handler.name))
                    body(handler.body, branch)
                    if handler.name is not None:
                        branch.discard(_mangle(class_name, handler.name))
                    if isinstance(item, ast.TryStar):
                        possible.update(branch)
                joined = possible.union(*branches)
                body(item.finalbody, joined)
                aliases.clear()
                aliases.update(*branches)
                written, definite = self._attrs_written_names(item.finalbody)
                written = {_mangle(class_name, key) for key in written}
                definite = {_mangle(class_name, key) for key in definite}
                aliases.difference_update(definite)
                aliases.update(joined & written)
                if all(
                    isinstance(statement, ast.Assign | ast.AnnAssign | ast.Pass)
                    and not any(self._attrs_eager_bindings(statement))
                    for statement in item.finalbody
                ):
                    aliases.clear()
                    aliases.update(*branches)
                    for statement in item.finalbody:
                        if isinstance(statement, ast.Assign):
                            for target in statement.targets:
                                bind(target, statement.value, aliases, aliases.copy())
                        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                            bind(statement.target, statement.value, aliases, aliases.copy())
                return
            if (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and isinstance(item.func.value, ast.Name)
                and item.func.attr in {"append", "extend", "insert", "pop", "remove", "clear", "reverse", "sort"}
            ):
                carrier = name(item.func.value)
                if carrier is not None and self._attrs_carrier_tracked(carrier, aliases):
                    mutate(carrier, aliases, value=any(shared(arg, aliases) for arg in item.args))
            if isinstance(item, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.Delete):
                targets = item.targets if isinstance(item, ast.Assign | ast.Delete) else [item.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and (carrier := name(target.value)) is not None
                        and self._attrs_carrier_tracked(carrier, aliases)
                    ):
                        value = item.value if isinstance(item, ast.Assign | ast.AnnAssign | ast.AugAssign) else None
                        possible = value is not None and shared(value, aliases)
                        written_target = ast.Subscript(
                            value=ast.Name(id=carrier, ctx=ast.Load()), slice=target.slice, ctx=target.ctx
                        )
                        written = (
                            self._attrs_written_slots(written_target, aliases) if isinstance(item, ast.Assign) else []
                        )
                        mutate(carrier, aliases, value=possible)
                        for slot in written:
                            aliases.add(f"{slot}:known")
                            if possible:
                                aliases.add(slot)
            if isinstance(item, ast.Assign):
                earlier = shared(item.value, aliases) if isinstance(item.value, ast.Subscript) else None
                captured: ast.expr = item.value
                values: set[str] = set()
                if isinstance(item.value, ast.Tuple | ast.List):
                    state = aliases.copy()
                    counter = 0

                    def capture_value(expr: ast.expr) -> ast.expr:
                        nonlocal counter
                        if isinstance(expr, ast.Tuple | ast.List):
                            return type(expr)(elts=[capture_value(child) for child in expr.elts], ctx=expr.ctx)
                        if isinstance(expr, ast.Starred):
                            return ast.Starred(value=capture_value(expr.value), ctx=expr.ctx)
                        key = f"\0value:{counter}"
                        counter += 1
                        if shared(expr, state):
                            values.add(key)
                        scan(expr, state)
                        return ast.Name(id=key, ctx=ast.Load())

                    saved = exception_states[:]
                    exception_states.clear()
                    try:
                        captured = capture_value(item.value)
                    finally:
                        exception_states.extend(saved)
                scan(item.value, aliases)
                sources = aliases | values
                for target in item.targets:
                    bind(target, captured, aliases, sources)
                    if earlier is not None and isinstance(target, ast.Name):
                        key = _mangle(class_name, target.id)
                        aliases.discard(key)
                        if earlier:
                            aliases.add(key)
                return
            if isinstance(item, ast.AnnAssign):
                if item.value is not None:
                    earlier = shared(item.value, aliases) if isinstance(item.value, ast.Subscript) else None
                    scan(item.value, aliases)
                    if name(item.target) not in fields:
                        bind(item.target, item.value, aliases, aliases.copy())
                        if earlier is not None and (key := name(item.target)) is not None:
                            aliases.discard(key)
                            if earlier:
                                aliases.add(key)
                return
            if isinstance(item, ast.Delete):
                for target in item.targets:
                    if (key := name(target)) is not None:
                        aliases.discard(key)
                        aliases.discard(f"\0carrier:{key}")
                        aliases.difference_update(
                            item
                            for item in aliases.copy()
                            if item.startswith(f"\0slot:{key}:")
                            or (item.startswith("\0alias:") and key in item.split(":")[1:])
                        )
                return
            for child in ast.iter_child_nodes(item):
                scan(child, aliases)

        def body(statements: list[ast.stmt], aliases: set[str]) -> None:
            for stmt in statements:
                for possible in exception_states:
                    possible.update(aliases)
                scan(stmt, aliases)
                for possible in exception_states:
                    possible.update(aliases)

        body(node.body, fields.copy())

    def _attrs_default(self, expr: ast.expr, module: _Module) -> bool:
        if self._external_name(expr, module) in {"attrs.NOTHING", "attr.NOTHING"}:
            return False
        if isinstance(expr, ast.Call) and self._external_name(expr.func, module) in {"attrs.Factory", "attr.Factory"}:
            if len(expr.args) != 1 or any(
                item.arg != "takes_self"
                or not isinstance(item.value, ast.Constant)
                or not isinstance(item.value.value, bool)
                for item in expr.keywords
            ):
                msg = "an attrs Factory has dynamic or unsupported options"
                raise _invalid(msg)
            return True
        try:
            ast.literal_eval(expr)
        except ValueError, TypeError, SyntaxError:
            msg = "an attrs field has a dynamic default"
            raise _invalid(msg) from None
        return True

    # a pydantic dataclass field reads its `Field(...)` like a model's, and takes a base's default the same way
    def _pydantic_dataclass_field(
        self, stmt: ast.AnnAssign, key: str, klass: _Class, inherited: list[dict[str, _Attr]]
    ) -> _Source | None:
        value: tuple[ast.expr, _Module] | None = (stmt.value, klass.module) if stmt.value is not None else None
        inherited_default = False
        if value is None and (hit := next((attrs[key] for attrs in inherited if key in attrs), None)) is not None:
            if hit.value is None:
                inherited_default = True
            else:
                value = (hit.value, hit.module)
        source = self._pydantic_source(stmt, key, klass, value, dataclass=True)
        if source is not None and inherited_default:
            source.default = True
        return source

    # a field with no value in the body takes its default from the first class attribute of that name in the MRO
    def _dataclass_field(
        self, stmt: ast.AnnAssign, key: str, klass: _Class, inherited: list[dict[str, _Attr]]
    ) -> _Source | None:
        value, module = stmt.value, klass.module
        if value is None:
            hit = next((attrs[key] for attrs in inherited if key in attrs), None)
            if hit is None:
                return self._source(stmt, key, klass)
            if hit.value is None:
                return self._source(stmt, key, klass, default=True)
            value, module = hit.value, hit.module
        if not (isinstance(value, ast.Call) and self._external_name(value.func, module) == "dataclasses.field"):
            return self._source(stmt, key, klass, default=True)
        default = False
        for keyword in value.keywords:
            if keyword.arg is None:
                msg = f"a `field(...)` in `{klass.name}` takes `**` arguments"
                raise _unresolved(msg)
            if keyword.arg == "init" and not self._literal_bool(
                keyword, f"the `init` of a `field(...)` in `{klass.name}`"
            ):
                return None
            default = default or keyword.arg in {"default", "default_factory"}
        return self._source(stmt, key, klass, default=default)

    # ---- pydantic ----

    def _is_field_call(self, expr: ast.expr, module: _Module) -> bool:
        return isinstance(expr, ast.Call) and self._external_name(expr.func, module) in {
            *FIELD_CALLS,
            "dataclasses.field",
        }

    # the `Field(...)` calls in the `Annotated` layers of a field's annotation, innermost first, found behind the
    # aliases that pydantic reads them through
    def _field_metadata(self, annotation: ast.expr, module: _Module, depth: int = 0) -> list[ast.Call]:
        expr = annotation
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                expr = ast.parse(expr.value.strip(), mode="eval").body
            except SyntaxError:
                return []
        if depth > MAX_HOPS:
            return []
        if isinstance(expr, ast.Name | ast.Attribute):
            target, target_module = self._unalias(expr, module)
            return [] if target is expr else self._field_metadata(target, target_module, depth + 1)
        slice_ = expr.slice if isinstance(expr, ast.Subscript) else None
        if (
            not isinstance(expr, ast.Subscript)
            or not (isinstance(slice_, ast.Tuple) and len(slice_.elts) >= MIN_ANNOTATED)
            or self._external_name(expr.value, module) != "typing.Annotated"
        ):
            return []
        calls = self._field_metadata(slice_.elts[0], module, depth + 1)
        calls.extend(
            element
            for element in slice_.elts[1:]
            if isinstance(element, ast.Call) and self._is_field_call(element, module)
        )
        return calls

    @staticmethod
    def _field_keywords(kwargs: dict[str, ast.expr], call: ast.Call, owner: _Class) -> None:
        # a later `Field(...)` overrides an earlier one's attributes, as pydantic merges them, but `...` is no default
        # at all, so it sets nothing
        pairs = [("default", arg) for arg in call.args[:1]]
        for keyword in call.keywords:
            if keyword.arg is None:
                msg = f"a `Field(...)` in `{owner.name}` takes `**` arguments"
                raise _unresolved(msg)
            pairs.append((keyword.arg, keyword.value))
        kwargs.update({
            name: value
            for name, value in pairs
            if name != "default" or not (isinstance(value, ast.Constant) and value.value is Ellipsis)
        })

    def _alias(self, kwargs: dict[str, ast.expr], key: str, owner: _Class) -> _PydField:
        for name in ("validation_alias", "alias"):
            expr = kwargs.get(name)
            if expr is None or (isinstance(expr, ast.Constant) and expr.value is None):
                continue
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                return _PydField(alias=expr.value)
            if (
                isinstance(expr, ast.Call)
                and (form := ALIAS_FORMS.get(self._external_name(expr.func, owner.module) or "")) is not None
            ):
                return _PydField(reason=f"field `{key}` uses {form}")
            return _PydField(reason=f"field `{key}` has an alias that isn't a literal string")
        return _PydField()

    # `value` is the field's value and the module it is read in; None for an annotation alone. The result is None for a pydantic dataclass field with `init=False`.
    def _pydantic_source(
        self,
        stmt: ast.AnnAssign,
        key: str,
        owner: _Class,
        value: tuple[ast.expr, _Module] | None,
        *,
        dataclass: bool,
    ) -> _Source | None:
        kwargs: dict[str, ast.expr] = {}
        for call in self._field_metadata(stmt.annotation, owner.module):
            self._field_keywords(kwargs, call, owner)
        plain = False
        if value is not None:
            expr, module = value
            if isinstance(expr, ast.Call) and self._is_field_call(expr, module):
                self._field_keywords(kwargs, expr, owner)
            elif isinstance(expr, ast.Constant) and expr.value is Ellipsis:
                # `= ...` marks a pydantic field required, and a model's clears the `default` that `Annotated` carried
                # (not a `default_factory`, and not a pydantic dataclass's)
                if not dataclass:
                    kwargs.pop("default", None)
            else:
                plain = True
        # only a pydantic dataclass ignores a field with `init=False`; a model still takes it as an ordinary field
        if dataclass and (init := kwargs.get("init")) is not None:
            if not (isinstance(init, ast.Constant) and isinstance(init.value, bool)):
                msg = f"the `init` of a `Field(...)` in `{owner.name}` isn't a literal"
                raise _unresolved(msg)
            if not init.value:
                return None
        # `Field()` and `Field(...)` stay required
        has_default = plain or "default" in kwargs or "default_factory" in kwargs
        source = self._source(stmt, key, owner, default=has_default)
        source.pydantic = self._alias(kwargs, key, owner)
        return source

    def _model_fields(self, info: _Info, node: ast.ClassDef, klass: _Class, infos: list[_Info]) -> None:
        # a field the class doesn't redeclare is the first base's copy
        inherited: dict[str, _Source | None] = {}
        for base in reversed(infos):
            if base.form == "model":
                inherited.update(base.fields)
        mro = self._mro(klass)
        # an attribute of a base that is not a model survives as a default; a model deletes its own
        plain = [base.attrs for member in mro[1:] if (base := self._info(member, ())).form is None]
        # pydantic reads the annotations of every class in the MRO, the base-most first, so a redeclared name keeps its
        # place and the most derived annotation wins
        hinted: dict[str, tuple[ast.AnnAssign, _Class]] = {}
        for member in reversed(mro):
            if isinstance(member.node, ast.ClassDef):
                for stmt, key in self._annotated(member.node):
                    hinted[key] = (stmt, member)
        own = {key for _, key in self._annotated(node)}
        for key, (stmt, member) in hinted.items():
            if (
                key == "model_config"
                or key.startswith("_")
                or self._special(stmt.annotation, member.module, pydantic=True) == "ClassVar"
            ):
                continue
            if key not in own and key in inherited:
                info.fields[key] = inherited[key]
                continue
            value = (stmt.value, member.module) if stmt.value is not None else None
            source = self._pydantic_source(stmt, key, member, value, dataclass=False)
            # a non-model base's class attribute is the default of a field with no value of its own
            if source is not None and value is None and any(key in attrs for attrs in plain):
                source.default = True
            info.fields[key] = source

    @staticmethod
    def _config_key(config: _Config, key: str, value: ast.expr, klass: _Class) -> None:
        if key not in CONFIG_KEYS:
            return
        if key == "alias_generator":
            literal_none = isinstance(value, ast.Constant) and value.value is None
            config.keys[key] = None if literal_none else f"`{klass.name}` has an alias_generator"
        elif isinstance(value, ast.Constant) and (value.value is None or isinstance(value.value, bool)):
            config.keys[key] = value.value
        else:
            config.keys[key] = f"the config key `{key}` of `{klass.name}` isn't a literal"

    def _config_expr(self, expr: ast.expr, klass: _Class) -> _Config:
        config = _Config()
        if not (isinstance(expr, ast.Call) and self._external_name(expr.func, klass.module) in CONFIG_DICTS):
            config.blocked = f"`{klass.name}` has a config that isn't a literal `ConfigDict(...)`"
            return config
        for keyword in expr.keywords:
            if keyword.arg is None:
                config.blocked = f"`{klass.name}` passes `**` to `ConfigDict`"
            else:
                self._config_key(config, keyword.arg, keyword.value, klass)
        return config

    def _config_class(self, node: ast.ClassDef, klass: _Class) -> _Config:
        config = _Config()
        if node.bases or node.keywords:
            config.blocked = f"the `Config` of `{klass.name}` has a base class"
        for stmt in node.body:
            match stmt:
                case (
                    ast.Assign(targets=[ast.Name(id=key)], value=value)
                    | ast.AnnAssign(target=ast.Name(id=key), value=ast.expr() as value)
                ):
                    # pydantic 2 keeps the v1 key `allow_population_by_field_name` as an unknown key, so it is ignored
                    self._config_key(config, key, value, klass)
        return config

    @staticmethod
    def _absorb(config: _Config, other: _Config) -> None:
        config.keys.update(other.keys)
        config.blocked = other.blocked or config.blocked

    # the config a model declares itself: `model_config`, `class Config`, then the class keywords, which win
    def _model_config(self, node: ast.ClassDef, klass: _Class) -> _Config:
        config = _Config()
        assigned = _Config()
        for stmt in _plain_statements(node.body):
            match stmt:
                case (
                    ast.Assign(targets=[ast.Name(id="model_config")], value=value)
                    | ast.AnnAssign(target=ast.Name(id="model_config"), value=ast.expr() as value)
                ):
                    # a second assignment replaces the first
                    assigned = self._config_expr(value, klass)
                case ast.ClassDef(name="Config"):
                    self._absorb(config, self._config_class(stmt, klass))
        self._absorb(config, assigned)
        for keyword in node.keywords:
            if keyword.arg is None:
                config.blocked = f"`{klass.name}` takes `**` class keywords"
            elif keyword.arg == "metaclass":
                msg = f"the metaclass of `{klass.name}` is unknown, so its effect on the constructor is not checked"
                raise _invalid(msg, help_=KEYWORD_HELP)
            elif keyword.arg == "alias_generator" or _is_literal(keyword.value):
                self._config_key(config, keyword.arg, keyword.value, klass)
            else:
                config.blocked = f"the class keyword `{keyword.arg}` of `{klass.name}` isn't a literal"
        return config

    # what a `model_validator(...)` does to the names: nothing for `after`, and a `before`, a `wrap` or a mode that
    # can't be read rewrites the input before the fields see it
    def _validator_reason(self, func: ast.expr, call: ast.Call | None, klass: _Class) -> str | None:
        if self._external_name(func, klass.module) not in MODEL_VALIDATORS:
            return None
        mode = next((keyword.value for keyword in call.keywords if keyword.arg == "mode"), None) if call else None
        match mode:
            case ast.Constant(value="after"):
                return None
            case ast.Constant(value="before" | "wrap" as literal):
                return f"`{klass.name}` has a {literal}-validator"
        return f"`{klass.name}` has a model validator whose mode isn't a literal"

    # the validators a statement registers: a decorated `def`, or a `model_validator(...)` call in an assignment
    def _statement_reason(self, stmt: ast.stmt, klass: _Class) -> str | None:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in stmt.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                if (reason := self._validator_reason(call.func if call else decorator, call, klass)) is not None:
                    return reason
        elif isinstance(stmt, ast.Assign | ast.AnnAssign) and stmt.value is not None:
            for child in ast.walk(stmt.value):
                if (
                    isinstance(child, ast.Call)
                    and (reason := self._validator_reason(child.func, child, klass)) is not None
                ):
                    return reason
        return None

    # why every name of a class is unknown: it rewrites the input before the fields see it, or sets what the checker
    # can't follow inside a conditional block, which the checker skips and the runtime runs
    def _rewrites(self, node: ast.ClassDef, klass: _Class, *, model: bool) -> str | None:
        for stmt in _plain_statements(node.body):
            if (reason := self._statement_reason(stmt, klass)) is not None:
                return reason
            if isinstance(stmt, DEF_TYPES):
                continue
            for nested in _nested_statements(stmt):
                if self._statement_reason(nested, klass) is not None:
                    return f"`{klass.name}` has a validator in a conditional block"
                if model and (
                    (isinstance(nested, ast.ClassDef) and nested.name == "Config")
                    or (
                        isinstance(nested, ast.Assign | ast.AnnAssign)
                        and any(
                            isinstance(target, ast.Name) and target.id == "model_config"
                            for target in (nested.targets if isinstance(nested, ast.Assign) else [nested.target])
                        )
                    )
                ):
                    return f"`{klass.name}` sets config in a conditional block"
        return None

    def _pydantic_info(
        self, info: _Info, node: ast.ClassDef, klass: _Class, decor: _Decor | None, infos: list[_Info]
    ) -> None:
        mro = self._mro(klass)
        rest = [base for base in (self._info(member, ()) for member in mro[1:]) if base.form == info.form]
        # pydantic registers a validator of any class in the MRO, a plain mixin's included
        info.rewrites = next(
            (
                reason
                for member in mro
                if isinstance(member.node, ast.ClassDef)
                and (reason := self._rewrites(member.node, member, model=info.form == "model")) is not None
            ),
            None,
        )
        if info.form == "model":
            # pydantic merges the direct bases' configs from the first to the last, so the last base wins, then the
            # class's own
            for base in infos:
                if base.form == "model":
                    self._absorb(info.config, base.config)
            self._absorb(info.config, self._model_config(node, klass))
            return
        # a pydantic dataclass's config is the decorator's `config=` (or `__pydantic_config__`), else the nearest base's
        expr = decor.config if decor is not None else None
        for stmt in _plain_statements(node.body):
            if (
                expr is None
                and isinstance(stmt, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__pydantic_config__" for target in stmt.targets)
            ):
                expr = stmt.value
        if expr is not None:
            info.config = self._config_expr(expr, klass)
        elif rest:
            info.config = _Config(keys=dict(rest[0].config.keys), blocked=rest[0].config.blocked)

    @staticmethod
    def _name_flags(config: _Config) -> tuple[bool, bool]:
        # mirrors pydantic's `_build_effective_config`, and `_pydantic_name_flags` in `_rowplan.py`
        by_alias = config.keys.get("validate_by_alias", True) is not False
        by_name = config.keys.get("validate_by_name")
        if by_name is None:
            if (populate := config.keys.get("populate_by_name")) is not None:
                by_alias, by_name = True, bool(populate)
            elif not by_alias:
                by_name = True
        return by_alias, bool(by_name)

    def _pydantic_names(self, info: _Info, source: _Source) -> tuple[tuple[str, ...] | None, str | None]:
        reason = info.rewrites if info.rewrites is not None else info.config.reason()
        if reason is not None:
            return None, reason
        pyd = source.pydantic or _PydField()
        if pyd.reason is not None:
            return None, pyd.reason
        by_alias, by_name = self._name_flags(info.config)
        if pyd.alias is None or not by_alias:
            return (source.key,), None
        return ((pyd.alias, source.key) if by_name and pyd.alias != source.key else (pyd.alias,)), None

    @staticmethod
    def _namedtuple_entries(klass: _Class, spec: ast.expr) -> list[tuple[str, ast.expr, ast.expr]]:
        if not isinstance(spec, ast.List | ast.Tuple):
            msg = f"`NamedTuple(...)` for `{klass.name}` needs a literal list of fields"
            raise _unresolved(msg)
        entries: list[tuple[str, ast.expr, ast.expr]] = []
        for item in spec.elts:
            match item:
                case (
                    ast.Tuple(elts=[ast.Constant(value=str() as key), annotation])
                    | ast.List(
                        elts=[ast.Constant(value=str() as key), annotation],
                    )
                ):
                    entries.append((key, annotation, item))
                case _:
                    msg = f"`NamedTuple(...)` for `{klass.name}` needs literal `(name, type)` pairs"
                    raise _unresolved(msg)
        return entries

    @staticmethod
    def _typeddict_entries(klass: _Class, spec: ast.expr) -> list[tuple[str, ast.expr, ast.expr]]:
        if not isinstance(spec, ast.Dict):
            msg = f"`TypedDict(...)` for `{klass.name}` needs a literal dict of fields"
            raise _unresolved(msg)
        entries: list[tuple[str, ast.expr, ast.expr]] = []
        for key, annotation in zip(spec.keys, spec.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                msg = f"`TypedDict(...)` for `{klass.name}` needs literal string keys"
                raise _unresolved(msg)
            entries.append((key.value, annotation, key))
        return entries

    def _functional_info(self, klass: _Class, call: ast.Call) -> _Info:
        namedtuple = self._external_name(call.func, klass.module) == "typing.NamedTuple"
        label = "NamedTuple" if namedtuple else "TypedDict"
        total = True
        for keyword in call.keywords:
            if namedtuple or keyword.arg != "total":
                msg = f"`{label}(...)` for `{klass.name}` needs literal arguments"
                raise _unresolved(msg)
            total = self._literal_bool(keyword, f"the `total` of `{klass.name}`")
        if len(call.args) != FUNCTIONAL_ARGS:
            msg = f"`{label}(...)` for `{klass.name}` needs a name and a literal list of fields"
            raise _unresolved(msg)
        spec = call.args[1]
        entries = self._namedtuple_entries(klass, spec) if namedtuple else self._typeddict_entries(klass, spec)
        info = _Info(form="namedtuple" if namedtuple else "typeddict")
        for key, annotation, at in entries:
            info.fields[key] = _Source(
                key=key, annotation=annotation, module=klass.module, node=at, total=total, owner=klass
            )
        return info

    def _named_row(self, klass: _Class, site: _Site | None) -> RowType:
        info = self._info(klass, ())
        if info.form is None:
            msg = f"`{klass.name}` isn't a dataclass, NamedTuple or TypedDict"
            raise _invalid(msg)
        if info.form in {"dataclass", "attrs"} and not info.init:
            msg = f"`{klass.name}` is a dataclass with init=False, which is not supported"
            raise _invalid(msg)
        sources = [source for source in info.fields.values() if source is not None]
        if info.form == "attrs":
            aliases = [source.attrs_alias for source in sources]
            if any(alias is None or not alias.isidentifier() or iskeyword(alias) for alias in aliases) or len(
                set(aliases)
            ) != len(aliases):
                msg = f"`{klass.name}` has invalid or duplicate constructor names"
                raise _invalid(msg)
        if not sources:
            msg = f"`{klass.name}` has no fields"
            raise _invalid(msg)
        owner = self._owner(klass, site, typeddict=info.form == "typeddict")
        # a pydantic model's bases are resolved by pydantic, with no limit
        if info.form != "model":
            self._check_depth(klass)
        fields = tuple(
            self._field(source, owner, names=names, reason=reason)
            for source in sources
            for names, reason in [
                self._pydantic_names(info, source)
                if info.form in PYDANTIC_FORMS
                else ((source.attrs_alias,), None)
                if info.form == "attrs" and source.attrs_alias is not None
                else ((source.key,), None)
            ]
        )
        form, matching = FORMS[info.form]
        return RowType(
            kind="named",
            form=form,
            matching=matching,
            type_name=owner.type_name,
            fields=fields,
            alias=self.alias,
        )

    def _owner(self, klass: _Class, site: _Site | None, *, typeddict: bool) -> _Owner:
        params = self._params(klass)
        env = self._bind(klass.name, params, site or _Site((), klass.module, {}, ())) if params else {}
        if not params:
            return _Owner(klass, klass.name, typeddict, env, None)
        shown = ", ".join(self._text(env[p.key]) for p in params)
        with_ = ", ".join(f"{p.name} = {self._text(env[p.key])}" for p in params)
        span = klass.module.span(klass.node)
        generic = f"`{klass.name}` is generic, defined at {klass.module.path}:{span.line}:{span.col}, with {with_}"
        return _Owner(klass, f"{klass.name}[{shown}]", typeddict, env, generic)

    # the type parameters in scope for a field: the root class's, bound through each base on the way to the class that
    # declares the field
    def _field_env(self, owner: _Owner, source: _Source) -> dict[str, _Arg]:
        env = owner.env
        if source.owner is None:
            return env
        for step in self._path(owner.klass, source.owner):
            site = _Site(step.site.args, step.site.module, env, (), in_field=True)
            env = self._bind(step.klass.name, self._params(step.klass), site)
        return env

    @staticmethod
    def _attrs_concrete(elem: Elem) -> bool:
        return elem.leaf != "any" and (elem.item is None or _Resolver._attrs_concrete(elem.item))

    def _attrs_converter_node(self, expr: ast.expr | None, module: _Module) -> tuple[ast.FunctionDef, _Module] | None:
        seen: set[tuple[Path, str]] = set()
        while isinstance(expr, ast.Name | ast.Attribute):
            if isinstance(expr, ast.Attribute):
                try:
                    target = self._resolve_ref(expr.value, module, ())
                except RejectError:
                    target = None
                if not isinstance(target, _ModuleRef):
                    return None
                module, expr = target.module, ast.Name(id=expr.attr, ctx=ast.Load())
            key = (module.path, expr.id)
            bindings = module.bindings.get(expr.id, [])
            if key in seen or len(bindings) != 1 or bindings[0].tc:
                return None
            seen.add(key)
            binding = bindings[0]
            if binding.kind == "from":
                target = self._find(module, binding.level, binding.module)
                if target is None:
                    return None
                module, expr = target, ast.Name(id=binding.name, ctx=ast.Load())
            elif binding.kind == "alias" and binding.expr is not None:
                expr = binding.expr
            elif isinstance(binding.node, ast.FunctionDef) and not binding.node.decorator_list:
                return binding.node, module
            else:
                return None
        return None

    def _attrs_converter(self, source: _Source, output: _Node) -> Elem:
        expr = source.converter
        module = source.converter_module or source.module
        if isinstance(expr, ast.Call) and self._external_name(expr.func, module) in {
            "attrs.Converter",
            "attr.Converter",
        }:
            if len(expr.args) != 1 or any(
                option.arg not in {"takes_self", "takes_field"}
                or not isinstance(option.value, ast.Constant)
                or option.value.value is not False
                for option in expr.keywords
            ):
                msg = f"field `{source.key}` has an unsupported converter"
                raise _invalid(msg)
            expr = expr.args[0]
        found = self._attrs_converter_node(expr, module)
        if found is not None:
            node, module = found
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            if len(positional) == 1 and not (args.vararg or args.kwarg or args.kwonlyargs) and node.returns is not None:
                annotation = positional[0].annotation
                if annotation is not None:
                    result = self._attrs_annotation_node(node.returns, module, {})
                    input_ = _to_elem(self._scoped(annotation, module, (), {}), allow_none=True)
                    if (
                        result.none == output.none
                        and frozenset(result.members) == frozenset(output.members)
                        and self._attrs_concrete(input_)
                    ):
                        return input_
        msg = f"field `{source.key}` has an unsupported converter signature"
        raise _invalid(msg)

    def _attrs_annotation_node(self, expr: ast.expr, module: _Module, env: dict[str, _Arg]) -> _Node:
        self._attrs_annotation = True
        try:
            return self._scoped(expr, module, (), env)
        finally:
            self._attrs_annotation = False

    def _field_elem(self, expr: ast.expr, source: _Source, owner: _Owner) -> Elem:
        env = self._field_env(owner, source)
        elem = _to_elem(self._scoped(expr, source.module, (), env), allow_none=True)
        if source.converter is not None:
            output = self._attrs_annotation_node(expr, source.module, env)
            elem = self._attrs_converter(source, output)
        return elem

    def _field(
        self, source: _Source, owner: _Owner, *, names: tuple[str, ...] | None, reason: str | None
    ) -> NamedField:
        span = source.module.span(source.node)
        self._in_field = True
        try:
            expr, override = self._strip_field(source.annotation, source.module)
            elem = self._field_elem(expr, source, owner)
        except RejectError as reject:
            notes = [f"field defined at {source.module.path}:{span.line}:{span.col}"]
            if owner.generic is not None:
                notes.append(owner.generic)
            message = f'{reject.message} (field "{source.key}" of `{owner.type_name}`)'
            raise RejectError(reject.rule, message, help_=reject.help, notes=(*reject.notes, *notes)) from None
        finally:
            self._in_field = False
        # only a TypedDict is optional by its qualifier or `total`; the others are by their default
        required = (source.total if override is None else override) if owner.typeddict else not source.default
        return NamedField(
            key=source.key,
            names=names,
            elem=elem,
            required=required,
            path=source.module.path,
            span=span,
            reason=reason,
            generic=owner.generic,
        )

    # the layers a field annotation may carry around its element: quotes, `Annotated` and the TypedDict qualifiers,
    # found in any nesting of them (a quoted `'NotRequired[str]'` included)
    def _strip_field(self, annotation: ast.expr, module: _Module) -> tuple[ast.expr, bool | None]:
        required: bool | None = None
        expr = annotation
        for _ in range(MAX_HOPS + 1):
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                expr = self._parse_quoted(expr.value)
            if not isinstance(expr, ast.Subscript):
                break
            base = self._constructor(self._resolve_ref(expr.value, module, ()))
            if not isinstance(base, _External):
                break
            name = self._normalize(base.qualname)
            slice_ = expr.slice
            if name == "typing.Annotated" and isinstance(slice_, ast.Tuple) and len(slice_.elts) >= MIN_ANNOTATED:
                expr = slice_.elts[0]
            elif name in FIELD_QUALIFIERS and not isinstance(slice_, ast.Tuple):
                if name != "typing.ReadOnly":
                    required = name == "typing.Required"
                expr = slice_
            else:
                break
        return expr, required

    def resolve(self, call: QueryCall) -> RowType:
        if call.row_type is None:
            return RowType(kind="none")
        module = self._graph.load(call.path)
        if module is None:
            msg = "the module could not be parsed"
            raise _unresolved(msg)
        if module.tree is not None:
            _reject_shadowed(module.tree, call.row_type)
        node = self._eval(call.row_type, module, ())
        if not node.members:
            return RowType(kind="none", alias=self.alias)
        if len(node.members) > 1:
            if (pair := _network_pair(node)) is not None:
                return RowType(kind="scalar", elements=(pair,), alias=self.alias)
            msg = "a union may have only one non-`None` member"
            raise _invalid(msg)
        (member,) = node.members
        if isinstance(member, _NamedRef):
            if node.none:
                msg = f"`{member.klass.name}` is a named row type, which can't be nullable"
                raise _invalid(msg, help_="use .optional()")
            return self._named_row(member.klass, member.site)
        if isinstance(member, _Tuple):
            return self._tuple_row(node, member)
        return RowType(kind="scalar", elements=(_to_elem(node, allow_none=False),), alias=self.alias)

    def _tuple_row(self, node: _Node, member: _Tuple) -> RowType:
        if node.none:
            msg = "a tuple can't be combined with `None`"
            raise _invalid(msg)
        return RowType(kind="tuple", elements=member.elems, alias=self.alias)


def resolve_row_type(
    graph: ModuleGraph,
    call: QueryCall,
    roots: tuple[Path, ...],
    backend: CheckerBackend | None = None,
    dsn: str | None = None,
) -> Resolution:
    resolver = _Resolver(graph, roots, backend, dsn)
    try:
        return Resolution(row_type=resolver.resolve(call))
    except RejectError as reject:
        notes = ()
        if (alias := resolver.alias) is not None:
            notes = alias.notes()
        diagnostic = Diagnostic(
            rule=reject.rule,
            message=reject.message,
            path=call.path,
            span=call.row_type_span or call.span,
            notes=(*notes, *reject.notes),
        )
        return Resolution(diagnostic=diagnostic, help=reject.help)
