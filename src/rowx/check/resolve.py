import ast
import builtins
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from sys import stdlib_module_names
from tokenize import detect_encoding
from typing import TYPE_CHECKING, Final, Literal, get_args

from rowx.check.model import Diagnostic, QueryCall, Rule, Span
from rowx.postgres._vocab import LeafName

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rowx.check.config import Config

MAX_HOPS: Final[int] = 16
INIT_FILE: Final[str] = "__init__.py"
TYPING_MODULES: Final[frozenset[str]] = frozenset({"typing", "typing_extensions"})
BUILTIN_NAMES: Final[frozenset[str]] = frozenset(dir(builtins))
LEAVES: Final[dict[str, LeafName]] = {
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
    "rowx.postgres.Money": "rowx.postgres.Money",
    "rowx.postgres._money.Money": "rowx.postgres.Money",
    "rowx.postgres.Interval": "rowx.postgres.Interval",
    "rowx.postgres._interval.Interval": "rowx.postgres.Interval",
}
# a pair of classes is ONE leaf, and a member alone is not a leaf
NETWORK_PAIRS: Final[dict[frozenset[str], LeafName]] = {
    frozenset({"ipaddress.IPv4Interface", "ipaddress.IPv6Interface"}): (
        "ipaddress.IPv4Interface | ipaddress.IPv6Interface"
    ),
    frozenset({"ipaddress.IPv4Network", "ipaddress.IPv6Network"}): "ipaddress.IPv4Network | ipaddress.IPv6Network",
}
NETWORK_MEMBERS: Final[frozenset[str]] = frozenset(member for pair in NETWORK_PAIRS for member in pair)
# `Range[T]` is one leaf per subtype leaf a range column can have
RANGE_LEAVES: Final[dict[str, LeafName]] = {
    "int": "rowx.postgres.Range[int]",
    "decimal.Decimal": "rowx.postgres.Range[decimal.Decimal]",
    "datetime.date": "rowx.postgres.Range[datetime.date]",
    "datetime.datetime": "rowx.postgres.Range[datetime.datetime]",
}
RANGE_CLASSES: Final[frozenset[str]] = frozenset({"rowx.postgres.Range", "rowx.postgres._range.Range"})
# `LeafName` is these maps plus the three structural markers; keep them in lockstep
assert (  # ruff: ignore[assert]
    set(LEAVES.values()) | set(NETWORK_PAIRS.values()) | set(RANGE_LEAVES.values()) | {"list", "any", "none"}
    == set(get_args(LeafName.__value__))
)
NEUTRAL: Final[frozenset[str]] = frozenset({"typing.Any", "builtins.object"})

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

type RowKind = Literal["none", "scalar", "tuple"]
type BindingKind = Literal["import", "from", "alias", "invalid", "other"]


# `leaf` is a LEAVES value, "list" (with `item`), "any" (`Any` and `object`) or "none" (a tuple element `None`)
@dataclass(slots=True, frozen=True)
class Elem:
    leaf: LeafName
    nullable: bool
    item: Elem | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class AliasSite:
    path: Path
    span: Span


@dataclass(slots=True, kw_only=True, frozen=True)
class RowType:
    kind: RowKind
    elements: tuple[Elem, ...] = ()
    # the first alias followed while resolving, for the "alias defined at" note
    alias: AliasSite | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class Resolution:
    row_type: RowType | None = None
    diagnostic: Diagnostic | None = None


@dataclass(slots=True, frozen=True)
class _Binding:
    kind: BindingKind
    module: str = ""
    level: int = 0
    name: str = ""
    expr: ast.expr | None = None
    generic: bool = False
    type_stmt: bool = False
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

    def span(self, node: ast.stmt) -> Span:
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
    # a `type X = …` statement makes a `TypeAliasType`, which can't be subscripted
    type_stmt: bool = False


type _Value = _External | _ModuleRef | _Alias


@dataclass(slots=True, frozen=True)
class _Tuple:
    elems: tuple[Elem, ...]


@dataclass(slots=True, frozen=True)
class _Node:
    members: tuple[Elem | _Tuple, ...]
    none: bool


class _Reject(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    def __init__(self, rule: Rule, message: str) -> None:
        super().__init__(message)
        self.rule: Rule = rule
        self.message = message


def _invalid(message: str) -> _Reject:
    return _Reject("invalid-row-type", message)


def _unresolved(message: str) -> _Reject:
    return _Reject("unresolved-row-type", message)


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


def _collect_definition(module: _Module, stmt: ast.stmt, *, tc: bool) -> None:
    site = module.span(stmt)
    match stmt:
        case ast.TypeAlias(name=ast.Name(id=name)):
            module.bind(
                name,
                _Binding("alias", expr=stmt.value, generic=bool(stmt.type_params), type_stmt=True, site=site, tc=tc),
            )
        case ast.AnnAssign(target=ast.Name(id=name), value=value) if value is not None and _is_type_alias_annotation(
            stmt.annotation,
        ):
            module.bind(name, _Binding("alias", expr=value, site=site, tc=tc))
        case ast.Assign(targets=[ast.Name(id="__all__")], value=value):
            module.all_names = _literal_names(value)
            module.bind("__all__", _Binding("other", tc=tc))
        case ast.Assign(targets=[ast.Name(id=name)], value=value):
            impure = any(isinstance(child, ast.Call | ast.Lambda | ast.NamedExpr) for child in ast.walk(value))
            module.bind(name, _Binding("invalid" if impure else "alias", name=name, expr=value, site=site, tc=tc))
        case ast.ClassDef() | ast.FunctionDef() | ast.AsyncFunctionDef():
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
        if parts[0] in stdlib_module_names:
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
    if member.leaf == "any":
        return Elem("any", nullable=True)
    if member.leaf in NETWORK_MEMBERS:
        msg = f"`{member.leaf}` is only valid as the pair `{_pair_of(member.leaf)}`"
        raise _invalid(msg)
    return Elem(member.leaf, nullable=node.none, item=member.item)


class _Resolver:
    def __init__(self, graph: ModuleGraph, roots: tuple[Path, ...]) -> None:
        self._graph = graph
        self._roots = roots
        self.alias: AliasSite | None = None

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
        if len(chain) >= MAX_HOPS:
            msg = f"`{name}` is more than {MAX_HOPS} hops from its definition"
            raise _unresolved(msg)
        chain = (*chain, key)
        if (bindings := module.bindings.get(name)) is not None:
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
            return self._lookup(providers[0], name, chain, allow_builtin=False)
        if allow_builtin and name in BUILTIN_NAMES:
            return _External(f"builtins.{name}")
        msg = f"`{name}` can't be resolved statically"
        raise _unresolved(msg)

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
            unique = tc[:1]
        if len(unique) > 1:
            msg = f"`{name}` is bound more than once at module level"
            raise _unresolved(msg)
        binding = unique[0]
        match binding.kind:
            case "import":
                return self._module_value(binding.module, module)
            case "from":
                return self._from_value(module, binding, chain)
            case "alias":
                if binding.generic:
                    msg = f"`{name}` is a generic alias"
                    raise _invalid(msg)
                if binding.expr is None or binding.site is None:
                    msg = f"`{name}` has no value"
                    raise _unresolved(msg)
                return _Alias(module, binding.expr, binding.site, chain, type_stmt=binding.type_stmt)
            case "invalid":
                msg = f"`{name}` is not a type expression"
                raise _invalid(msg)
            case "other":
                msg = f"`{name}` is not bound by an import or a type alias"
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
            case ast.Constant(value=str()):
                msg = "a quoted type isn't supported"
                raise _invalid(msg)
            case ast.Name() | ast.Attribute():
                return self._eval_value(self._resolve_ref(expr, module, chain))
            case ast.Subscript():
                return self._eval_subscript(expr, module, chain)
            case ast.BinOp(op=ast.BitOr(), left=left, right=right):
                return _union(self._eval(left, module, chain), self._eval(right, module, chain))
        msg = "the row type isn't derivable from the row-type grammar"
        raise _invalid(msg)

    def _eval_value(self, value: _Value) -> _Node:
        match value:
            case _Alias():
                if self.alias is None:
                    self.alias = AliasSite(path=value.module.path, span=value.site)
                return self._eval(value.expr, value.module, value.chain)
            case _External(qualname):
                normalized = self._normalize(qualname)
                if (leaf := LEAVES.get(normalized)) is not None:
                    return _Node((Elem(leaf, nullable=False),), none=False)
                if normalized in NEUTRAL:
                    return _Node((Elem("any", nullable=False),), none=False)
                msg = f"`{qualname}` isn't a supported row type"
                raise _invalid(msg)
            case _ModuleRef():
                msg = "a module isn't a row type"
                raise _invalid(msg)

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

    def _eval_subscript(self, expr: ast.Subscript, module: _Module, chain: tuple[tuple[Path, str], ...]) -> _Node:
        base = self._constructor(self._resolve_ref(expr.value, module, chain))
        if not isinstance(base, _External):
            msg = "only `tuple`, `list`, `Optional`, `Union` and `Annotated` can be subscripted"
            raise _invalid(msg)
        slice_ = expr.slice
        elts = slice_.elts if isinstance(slice_, ast.Tuple) else [slice_]
        qualname = self._normalize(base.qualname)
        if qualname in RANGE_CLASSES and len(elts) == 1 and not isinstance(slice_, ast.Tuple):
            return self._eval_range(slice_, module, chain)
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
            case "builtins.list" if len(elts) == 1 and not isinstance(slice_, ast.Tuple):
                item = _to_elem(self._eval(slice_, module, chain), allow_none=False)
                return _Node((Elem("list", nullable=False, item=item),), none=False)
            case "builtins.tuple" if elts:
                elems = tuple(_to_elem(self._eval(element, module, chain), allow_none=True) for element in elts)
                return _Node((_Tuple(elems),), none=False)
        msg = f"`{base.qualname}` can't be subscripted this way in a row type"
        raise _invalid(msg)

    def _eval_range(self, subtype: ast.expr, module: _Module, chain: tuple[tuple[Path, str], ...]) -> _Node:
        inner = _to_elem(self._eval(subtype, module, chain), allow_none=False)
        if inner.nullable or inner.leaf not in RANGE_LEAVES:
            msg = f"`Range[{inner.leaf}]` isn't a range subtype; use one of {', '.join(f'`{leaf}`' for leaf in RANGE_LEAVES)}"
            raise _invalid(msg)
        return _Node((Elem(RANGE_LEAVES[inner.leaf], nullable=False),), none=False)

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
        if isinstance(member, _Tuple):
            if node.none:
                msg = "a tuple can't be combined with `None`"
                raise _invalid(msg)
            return RowType(kind="tuple", elements=member.elems, alias=self.alias)
        return RowType(kind="scalar", elements=(_to_elem(node, allow_none=False),), alias=self.alias)


def resolve_row_type(graph: ModuleGraph, call: QueryCall, roots: tuple[Path, ...]) -> Resolution:
    resolver = _Resolver(graph, roots)
    try:
        return Resolution(row_type=resolver.resolve(call))
    except _Reject as reject:
        notes = ()
        if (alias := resolver.alias) is not None:
            notes = (f"alias defined at {alias.path}:{alias.span.line}:{alias.span.col}",)
        diagnostic = Diagnostic(
            rule=reject.rule,
            message=reject.message,
            path=call.path,
            span=call.row_type_span or call.span,
            notes=notes,
        )
        return Resolution(diagnostic=diagnostic)
