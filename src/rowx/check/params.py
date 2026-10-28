import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

type ParamName = Literal[
    "bool",
    "int",
    "float",
    "str",
    "bytes",
    "None",
    "Template",
    "Decimal",
    "UUID",
    "IPv4Interface",
    "IPv6Interface",
    "IPv4Address",
    "IPv6Address",
    "IPv4Network",
    "IPv6Network",
    "Range",
    "Money",
    "Interval",
    "Point",
    "Line",
    "LineSegment",
    "Box",
    "Path",
    "Polygon",
    "Circle",
    "Cube",
    "dict",
    "date",
    "datetime",
    "timedelta",
    "list",
]
type _BindingKind = Literal["annotated", "assign", "import", "other"]
type _ScopeKind = Literal["module", "function", "class", "comprehension", "type_params"]


# `item` is the element type of a `list`; a tuple literal is a `list` too, since it binds an array
@dataclass(slots=True, frozen=True)
class ParamType:
    name: ParamName
    item: ParamType | None = None


@dataclass(slots=True, eq=False)
class _Scope:
    kind: _ScopeKind
    parent: _Scope | None
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)

    def bind(self, name: str, binding: _Binding) -> None:
        self.bindings.setdefault(name, []).append(binding)

    def bind_other(self, name: str) -> None:
        self.bind(name, _Binding("other", self))

    def lookup(self, name: str) -> list[_Binding] | None:
        current: _Scope | None = self
        while current is not None:
            # a class body is visible only to code directly inside it
            if (current.kind != "class" or current is self) and name in current.bindings:
                return current.bindings[name]
            current = current.parent
        return None


# `expr` is the annotation or right-hand side, evaluated in `scope`; `target` is the dotted name an import binds
@dataclass(slots=True, eq=False)
class _Binding:
    kind: _BindingKind
    scope: _Scope
    expr: ast.expr | None = None
    target: str = ""


BUILTIN_NAMES: Final[dict[str, ParamName]] = {
    "int": "int",
    "str": "str",
    "float": "float",
    "bool": "bool",
    "bytes": "bytes",
    "dict": "dict",
}
CLASS_NAMES: Final[dict[str, ParamName]] = {
    "decimal.Decimal": "Decimal",
    "uuid.UUID": "UUID",
    "ipaddress.IPv4Interface": "IPv4Interface",
    "ipaddress.IPv6Interface": "IPv6Interface",
    "ipaddress.IPv4Address": "IPv4Address",
    "ipaddress.IPv6Address": "IPv6Address",
    "ipaddress.IPv4Network": "IPv4Network",
    "ipaddress.IPv6Network": "IPv6Network",
    "rowx.postgres.Range": "Range",
    "rowx.postgres._range.Range": "Range",
    "rowx.postgres.Money": "Money",
    "rowx.postgres._money.Money": "Money",
    "rowx.postgres.Interval": "Interval",
    "rowx.postgres._interval.Interval": "Interval",
    "rowx.postgres.Point": "Point",
    "rowx.postgres._geometric.Point": "Point",
    "rowx.postgres.Line": "Line",
    "rowx.postgres._geometric.Line": "Line",
    "rowx.postgres.LineSegment": "LineSegment",
    "rowx.postgres._geometric.LineSegment": "LineSegment",
    "rowx.postgres.Box": "Box",
    "rowx.postgres._geometric.Box": "Box",
    "rowx.postgres.Path": "Path",
    "rowx.postgres._geometric.Path": "Path",
    "rowx.postgres.Polygon": "Polygon",
    "rowx.postgres._geometric.Polygon": "Polygon",
    "rowx.postgres.Circle": "Circle",
    "rowx.postgres._geometric.Circle": "Circle",
    "rowx.postgres.Cube": "Cube",
    "rowx.postgres._cube.Cube": "Cube",
    "datetime.date": "date",
    "datetime.datetime": "datetime",
    "datetime.timedelta": "timedelta",
}
CALL_NAMES: Final[dict[str, ParamName]] = {**CLASS_NAMES, "datetime.datetime.now": "datetime"}
LITERAL_NAMES: Final[dict[type, ParamName]] = {
    int: "int",
    float: "float",
    str: "str",
    bytes: "bytes",
    bool: "bool",
    type(None): "None",
}
NEGATABLE: Final[tuple[type, ...]] = (int, float)


def _dotted(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return [node.id, *reversed(parts)]


def _union_members(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*_union_members(node.left), *_union_members(node.right)]
    return [node]


def _type_params_scope(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef, scope: _Scope
) -> _Scope:
    if isinstance(node, ast.Lambda) or not node.type_params:
        return scope
    inner = _Scope("type_params", scope)
    for param in node.type_params:
        match param:
            case ast.TypeVar(name=name) | ast.ParamSpec(name=name) | ast.TypeVarTuple(name=name):
                inner.bind_other(name)
    return inner


class ParamInference:
    def __init__(self, tree: ast.Module) -> None:
        self._module = _Scope("module", None)
        self._scopes: dict[int, _Scope] = {}
        # a name declared `global` or `nonlocal` anywhere can be rebound from another scope
        self._declared: set[str] = set()
        self._star_import = False
        self._memo: dict[int, ParamType | None] = {}
        self._visit_all(tree.body, self._module)
        self._scopes[id(tree)] = self._module

    def interpolations(self, template: ast.TemplateStr) -> tuple[ParamType | None, ...]:
        return tuple(self.infer(part.value) for part in template.values if isinstance(part, ast.Interpolation))

    def infer(self, expr: ast.expr) -> ParamType | None:
        scope = self._scopes.get(id(expr))
        return None if scope is None else self._expr_type(expr, scope)

    def _visit_all(self, nodes: Sequence[ast.AST], scope: _Scope) -> None:
        for node in nodes:
            self._visit(node, scope)

    def _visit_children(self, node: ast.AST, scope: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, scope: _Scope) -> None:
        args = node.args
        # decorators, defaults and annotations are evaluated where the function is defined
        if not isinstance(node, ast.Lambda):
            scope.bind_other(node.name)
            self._visit_all(node.decorator_list, scope)
        self._visit_all([*args.defaults, *(d for d in args.kw_defaults if d is not None)], scope)
        # annotations are evaluated inside the scope that holds the function's type parameters
        outer = _type_params_scope(node, scope)
        inner = _Scope("function", outer)
        self._scopes[id(node)] = inner
        positional = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if not isinstance(node, ast.Lambda) and node.returns is not None:
            self._visit(node.returns, outer)
        for arg in positional:
            self._scopes[id(arg)] = outer
            if arg.annotation is not None:
                self._visit(arg.annotation, outer)
                inner.bind(arg.arg, _Binding("annotated", outer, arg.annotation))
            else:
                inner.bind_other(arg.arg)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                if arg.annotation is not None:
                    self._visit(arg.annotation, outer)
                inner.bind_other(arg.arg)
        self._visit_all([node.body] if isinstance(node, ast.Lambda) else node.body, inner)

    def _visit_class(self, node: ast.ClassDef, scope: _Scope) -> None:
        scope.bind_other(node.name)
        self._visit_all(node.decorator_list, scope)
        outer = _type_params_scope(node, scope)
        self._visit_all([*node.bases, *(keyword.value for keyword in node.keywords)], outer)
        self._visit_all(node.body, _Scope("class", outer))

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp, scope: _Scope
    ) -> None:
        inner = _Scope("comprehension", scope)
        self._scopes[id(node)] = scope
        # only the first iterable is evaluated outside the comprehension
        for index, generator in enumerate(node.generators):
            self._visit(generator.iter, scope if index == 0 else inner)
            self._visit(generator.target, inner)
            self._visit_all(generator.ifs, inner)
        if isinstance(node, ast.DictComp):
            self._visit_all([node.key, node.value], inner)
        else:
            self._visit(node.elt, inner)

    def _visit_assign(self, node: ast.Assign | ast.AnnAssign, scope: _Scope) -> None:
        self._scopes[id(node)] = scope
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if isinstance(node, ast.AnnAssign):
            self._visit(node.annotation, scope)
        if node.value is not None:
            self._visit(node.value, scope)
        for target in targets:
            if not isinstance(target, ast.Name):
                self._visit(target, scope)
                continue
            self._scopes[id(target)] = scope
            if isinstance(node, ast.AnnAssign):
                scope.bind(target.id, _Binding("annotated", scope, node.annotation))
            else:
                scope.bind(target.id, _Binding("assign", scope, node.value))

    def _visit_import(self, node: ast.Import | ast.ImportFrom, scope: _Scope) -> None:
        self._scopes[id(node)] = scope
        for alias in node.names:
            if isinstance(node, ast.Import):
                # `import a.b` binds `a`; `import a.b as c` binds `c` to `a.b`
                bound, target = (
                    (alias.asname, alias.name) if alias.asname is not None else (alias.name.split(".")[0],) * 2
                )
            elif alias.name == "*":
                self._star_import = True
                continue
            else:
                bound = alias.asname or alias.name
                # a relative import resolves to nothing the checker knows
                target = "" if node.level or node.module is None else f"{node.module}.{alias.name}"
            scope.bind(bound, _Binding("import", scope, target=target))

    def _visit(self, node: ast.AST, scope: _Scope) -> None:
        self._scopes[id(node)] = scope
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.Lambda():
                self._visit_function(node, scope)
            case ast.ClassDef():
                self._visit_class(node, scope)
            case ast.ListComp() | ast.SetComp() | ast.GeneratorExp() | ast.DictComp():
                self._visit_comprehension(node, scope)
            case ast.Assign() | ast.AnnAssign():
                self._visit_assign(node, scope)
            case ast.Import() | ast.ImportFrom():
                self._visit_import(node, scope)
            case _:
                self._visit_binder(node, scope)

    def _visit_binder(self, node: ast.AST, scope: _Scope) -> None:
        match node:
            case ast.Global(names=names) | ast.Nonlocal(names=names):
                self._declared.update(names)
            case ast.NamedExpr(target=target):
                # a walrus inside a comprehension binds in the scope around it
                home = scope
                while home.kind == "comprehension" and home.parent is not None:
                    home = home.parent
                self._scopes[id(target)] = scope
                home.bind_other(target.id)
                self._visit(node.value, scope)
            case ast.Name(id=name, ctx=ast.Store() | ast.Del()):
                scope.bind_other(name)
            case ast.ExceptHandler(name=str(name)):
                scope.bind_other(name)
                self._visit_children(node, scope)
            case ast.MatchAs(name=str(name)) | ast.MatchStar(name=str(name)) | ast.MatchMapping(rest=str(name)):
                scope.bind_other(name)
                self._visit_children(node, scope)
            case ast.TypeAlias(name=ast.Name(id=name)):
                scope.bind_other(name)
                self._visit_children(node, scope)
            case _:
                self._visit_children(node, scope)

    # the dotted name an expression refers to after following its head through imports; a bare
    # builtin is itself, and anything rebound, declared `global` or possibly star-imported is unknown
    def _qualify(self, node: ast.expr, scope: _Scope) -> str | None:
        parts = _dotted(node)
        if parts is None or parts[0] in self._declared:
            return None
        head, *rest = parts
        bindings = scope.lookup(head)
        if bindings is None:
            return None if self._star_import else ".".join(parts)
        match bindings:
            case [_Binding(kind="import", target=target)] if target:
                return ".".join([target, *rest]).removeprefix("builtins.")
            case _:
                return None

    def _annotation_type(self, node: ast.expr, scope: _Scope) -> ParamType | None:
        match node:
            case ast.Constant(value=None):
                return ParamType("None")
            case ast.BinOp(op=ast.BitOr()):
                return self._union_type(node, scope)
            case ast.Subscript(value=value, slice=item):
                return self._subscript_type(value, item, scope)
            case ast.Name() | ast.Attribute():
                qualified = self._qualify(node, scope)
                name = None if qualified is None else BUILTIN_NAMES.get(qualified) or CLASS_NAMES.get(qualified)
                return None if name is None else ParamType(name)
        return None

    def _subscript_type(self, value: ast.expr, item: ast.expr, scope: _Scope) -> ParamType | None:
        qualified = self._qualify(value, scope)
        if qualified is not None and CLASS_NAMES.get(qualified) == "Range":
            return ParamType("Range")
        element = self._annotation_type(item, scope) if qualified == "list" else None
        if element is None:
            return ParamType("dict") if qualified == "dict" else None
        return ParamType("list", element)

    def _union_type(self, node: ast.expr, scope: _Scope) -> ParamType | None:
        members = [m for m in _union_members(node) if not (isinstance(m, ast.Constant) and m.value is None)]
        if not members:
            return ParamType("None")
        found = {self._annotation_type(member, scope) for member in members}
        return found.pop() if len(found) == 1 else None

    def _literal_type(self, node: ast.expr, scope: _Scope) -> ParamType | None:
        match node:
            case ast.Constant(value=value):
                name = LITERAL_NAMES.get(type(value))
                return None if name is None else ParamType(name)
            case ast.UnaryOp(op=ast.USub() | ast.UAdd(), operand=ast.Constant(value=value)) if type(value) in NEGATABLE:
                return ParamType(LITERAL_NAMES[type(value)])
            case ast.TemplateStr():
                return ParamType("Template")
            case ast.List(elts=items) | ast.Tuple(elts=items):
                found = {None if isinstance(item, ast.Starred) else self._expr_type(item, scope) for item in items}
                if len(found) == 1 and None not in found:
                    return ParamType("list", found.pop())
        return None

    def _name_type(self, name: str, scope: _Scope) -> ParamType | None:
        if name in self._declared:
            return None
        match scope.lookup(name):
            case [binding]:
                pass
            case _:
                return None
        key = id(binding)
        if key in self._memo:
            return self._memo[key]
        # a self-referencing assignment is unknown rather than recursing
        self._memo[key] = None
        found = None
        if binding.expr is not None:
            if binding.kind == "annotated":
                found = self._annotation_type(binding.expr, binding.scope)
            elif binding.kind == "assign":
                found = self._expr_type(binding.expr, binding.scope)
        self._memo[key] = found
        return found

    def _expr_type(self, node: ast.expr, scope: _Scope) -> ParamType | None:
        match node:
            case ast.Name(id=name):
                return self._name_type(name, scope)
            case ast.Call(func=func):
                qualified = self._qualify(func, scope)
                name = CALL_NAMES.get(qualified) if qualified is not None else None
                return None if name is None else ParamType(name)
        return self._literal_type(node, scope)
