import ast
import builtins
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path
from sys import stdlib_module_names
from tokenize import detect_encoding
from typing import TYPE_CHECKING, Final, Literal

from rowx._backend import CheckerBackend, backend_for, default_backend
from rowx.check.model import Diagnostic, QueryCall, Rule, Span

if TYPE_CHECKING:
    from collections.abc import Iterable

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
KNOWN_ROOTS: Final[frozenset[str]] = stdlib_module_names | {"typing_extensions", "rowx", "pydantic"}
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

SCOPE_TYPES: Final = (
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
type Form = Literal["dataclass", "namedtuple", "typeddict", "model", "pydantic_dataclass"]

FORMS: Final[dict[Form, tuple[RowForm, Matching]]] = {
    "dataclass": ("keyword", "by_name"),
    "namedtuple": ("positional", "positional"),
    "typeddict": ("dict", "by_name"),
    "model": ("validator", "by_name"),
    "pydantic_dataclass": ("validator", "by_name"),
}
DIRECT_FORMS: Final[dict[str, Form]] = {"NamedTuple": "namedtuple", "TypedDict": "typeddict", "BaseModel": "model"}
FUNCTIONAL_ARGS: Final[int] = 2
type ClassKey = tuple[Path, int, int]
DEF_TYPES: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
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


def _external_reject(qualname: str, *, in_field: bool = False) -> RejectError:
    root = qualname.split(".", maxsplit=1)[0]
    if root in KNOWN_ROOTS:
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
            module.bind(stmt.name, _Binding("invalid", name=stmt.name, site=site, tc=tc))
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
    roots = [root for entry in config.include if (root := (config.root / entry).resolve()).is_dir()]
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
    def __init__(self) -> None:
        self._modules: dict[Path, _Module | None] = {}

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
        # the type parameters in scope while a generic's body is evaluated, each bound to its argument
        self._env: dict[str, _Arg] = {}
        self._infos: dict[ClassKey, _Info] = {}
        self._free: set[tuple[Path, str]] = set()
        self._paths: dict[tuple[ClassKey, ClassKey], tuple[_Rebind, ...]] = {}
        self._mros: dict[ClassKey, list[_Class]] = {}

    def _selected(self) -> CheckerBackend:
        if self._backend is not None:
            return self._backend
        # a failure is not stored, so a later lookup tries again
        selected = backend_for(self._dsn) if self._dsn is not None and self._dsn.strip() else default_backend()
        self._backend = selected
        return selected

    def _find(self, module: _Module, level: int, dotted: str | None) -> _Module | None:
        if level == 0:
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
                    return _Node((Elem("any", nullable=False),), none=False)
                if (
                    normalized.startswith("rowx.")
                    and (leaf := self._selected().value_leaves.get(normalized)) is not None
                ):
                    return _Node((Elem(leaf, nullable=False),), none=False)
                raise _external_reject(qualname, in_field=self._in_field)
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
            qualname.startswith("rowx.")
            and qualname in self._selected().range_classes
            and len(elts) == 1
            and not isinstance(slice_, ast.Tuple)
        ):
            return self._eval_range(slice_, module, chain)
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

    def _eval_range(self, subtype: ast.expr, module: _Module, chain: tuple[tuple[Path, str], ...]) -> _Node:
        inner = _to_elem(self._eval(subtype, module, chain), allow_none=False)
        leaves = self._selected().range_leaves
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
        if qualname.startswith("rowx.") and (value := self._selected().value_classes.get(qualname)) is not None:
            msg = f"a subclass of rowx.postgres.{value} is not a row type"
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
        if qualname.split(".", maxsplit=1)[0] in KNOWN_ROOTS:
            msg = f"`{qualname}` isn't a supported row type"
            raise _invalid(msg, help_=BASE_HELP)
        raise _external_reject(qualname)

    @staticmethod
    def _literal_bool(keyword: ast.keyword, what: str) -> bool:
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool):
            return keyword.value.value
        msg = f"{what} isn't a literal"
        raise _unresolved(msg)

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
        if name != "dataclasses.dataclass" and kind != "pydantic_dataclass":
            msg = f"the decorator `{ast.unparse(func)}` on `{klass.name}` is unknown, so its effect on the constructor is not checked"
            raise _invalid(msg, help_=DECORATOR_HELP)
        init, config = True, None
        for keyword in call.keywords if call is not None else ():
            if keyword.arg is None:
                msg = f"the dataclass decorator on `{klass.name}` takes `**` arguments"
                raise _unresolved(msg)
            if keyword.arg == "config":
                config = keyword.value
            if keyword.arg in {"init", "kw_only"}:
                literal = self._literal_bool(
                    keyword, f"the `{keyword.arg}` of the dataclass decorator on `{klass.name}`"
                )
                init = literal if keyword.arg == "init" else init
        # pydantic's decorator only takes `init=False`, which keeps its own validating `__init__`
        return _Decor(kind=kind, init=True if kind == "pydantic_dataclass" else init, config=config)

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
        raw = self._raw_attrs(node, klass)
        match form:
            case "dataclass" | "pydantic_dataclass":
                self._dataclass_fields(info, node, klass, raw)
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
        if info.form == "dataclass" and not info.init:
            msg = f"`{klass.name}` is a dataclass with init=False, which is not supported"
            raise _invalid(msg)
        sources = [source for source in info.fields.values() if source is not None]
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
                self._pydantic_names(info, source) if info.form in PYDANTIC_FORMS else ((source.key,), None)
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

    def _field(
        self, source: _Source, owner: _Owner, *, names: tuple[str, ...] | None, reason: str | None
    ) -> NamedField:
        span = source.module.span(source.node)
        self._in_field = True
        try:
            expr, override = self._strip_field(source.annotation, source.module)
            elem = _to_elem(self._scoped(expr, source.module, (), self._field_env(owner, source)), allow_none=True)
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
