import ast
from dataclasses import dataclass, field
from io import BytesIO
from re import compile as re_compile
from tokenize import detect_encoding
from typing import TYPE_CHECKING, Final, Literal, Self, override

from rowx.check.model import (
    QUERY_KINDS,
    Diagnostic,
    DynamicFormatSpec,
    FileResult,
    IndirectKind,
    IndirectUse,
    QueryCall,
    QueryKind,
    Rule,
    Span,
    TemplateCall,
    TemplateConcat,
    TemplateFacts,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

ROWX_MODULE: Final[str] = "rowx"
STRING_MODULE: Final[str] = "string"
DUNDER: Final[str] = "__"
TEMPLATELIB_ATTR: Final[str] = "templatelib"
TEMPLATELIB_MODULE: Final[str] = f"{STRING_MODULE}.{TEMPLATELIB_ATTR}"
IDENTITY_OPERATORS: Final = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)
NAME_ATTRIBUTES: Final[frozenset[str]] = frozenset({"__name__", "__qualname__", "__module__"})
TYPE_CHECK_FUNCTIONS: Final[frozenset[str]] = frozenset({"isinstance", "issubclass"})
TYPE_PARAMS: Final = (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)
LINE_PATTERN: Final = re_compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")

# "private" is a `string.templatelib` name other than `Template`, `Interpolation` and `convert`
type Tag = Literal[
    "query",
    "query_unchecked",
    "module",
    "template",
    "interpolation",
    "private",
    "string",
    "templatelib",
    "rowxpriv",
    "cast",
    "typing",
    "optional",
    "annotated",
    "other",
]

TEMPLATELIB_PUBLIC: Final[frozenset[str]] = frozenset({"Template", "Interpolation", "convert"})
TYPING_TAGS: Final[dict[str, Tag]] = {"cast": "cast", "Optional": "optional", "Annotated": "annotated"}
TYPING_MODULES: Final[frozenset[str]] = frozenset({"typing", "typing_extensions"})
MODULE_TAG: Final[frozenset[Tag]] = frozenset({"module"})
TEMPLATELIB_TAG: Final[frozenset[Tag]] = frozenset({"templatelib"})
STRING_TAG: Final[frozenset[Tag]] = frozenset({"string"})
CAST_TAG: Final[frozenset[Tag]] = frozenset({"cast"})
TYPING_TAG: Final[frozenset[Tag]] = frozenset({"typing"})
OPTIONAL_TAG: Final[frozenset[Tag]] = frozenset({"optional"})
ANNOTATED_TAG: Final[frozenset[Tag]] = frozenset({"annotated"})
QUERY_TAGS: Final[frozenset[Tag]] = frozenset({"query", "query_unchecked"})
CLASS_TAGS: Final[frozenset[Tag]] = frozenset({"template", "interpolation", "private"})
PRIVATE_MODULE_TAG: Final[frozenset[Tag]] = frozenset({"rowxpriv"})
MODULE_VALUE_TAGS: Final[frozenset[Tag]] = frozenset({"string", "templatelib", "rowxpriv"})
RE_EXPORTED: Final[frozenset[str]] = frozenset({"Template", "Interpolation"})
EXPORT_TAGS: Final[frozenset[Tag]] = frozenset({*CLASS_TAGS, *MODULE_VALUE_TAGS})
type Scoped = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef | ast.Module
type Comprehension = ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp


class _Lines:
    def __init__(self, source: str) -> None:
        self._lines = [line.encode() for line in LINE_PATTERN.findall(source)]

    def _col(self, line: int, byte_col: int) -> int:
        if not 1 <= line <= len(self._lines):
            return byte_col + 1
        return len(self._lines[line - 1][:byte_col].decode(errors="replace")) + 1

    def span(self, node: ast.expr | ast.stmt | ast.alias) -> Span:
        end_line = node.end_lineno or node.lineno
        end_col = node.end_col_offset if node.end_col_offset is not None else node.col_offset
        return Span(
            line=node.lineno,
            col=self._col(node.lineno, node.col_offset),
            end_line=end_line,
            end_col=self._col(end_line, end_col),
        )


@dataclass(slots=True)
class _Scope:
    kind: Literal["module", "function", "class", "comprehension", "annotation"]
    bindings: dict[str, list[Tag]] = field(default_factory=dict)
    # how a name was declared evidently as a `Template`: the annotation it was declared with, or "literal" for a
    # t-string; annotations of parameters are evaluated in the enclosing scope, the others in this one
    hints: dict[str, ast.expr | Literal["literal"]] = field(default_factory=dict)
    arg_hints: dict[str, ast.expr] = field(default_factory=dict)
    # the value of each plain assignment to a name, which is evidently a `Template` when that value is
    aliases: dict[str, ast.expr] = field(default_factory=dict)
    # the names whose declaration is evidently a `Template`, resolved in the scope that evaluates the annotation
    templates: set[str] = field(default_factory=set)

    def bind(self, name: str, tag: Tag = "other") -> None:
        self.bindings.setdefault(name, []).append(tag)

    def bind_all(self, names: Iterable[str]) -> None:
        for name in names:
            self.bind(name)

    @classmethod
    def build(cls, node: Scoped | Comprehension) -> Self:
        match node:
            case ast.Module():
                scope = cls("module")
                _collect(scope, node.body)
            case ast.ClassDef():
                scope = cls("class")
                _collect(scope, node.body)
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                scope = cls("function")
                _bind_arguments(scope, node.args)
                _collect(scope, node.body)
            case ast.Lambda():
                scope = cls("function")
                _bind_arguments(scope, node.args)
                _collect(scope, [node.body])
            case _:
                scope = cls("comprehension")
                for generator in node.generators:
                    _collect(scope, [generator.target])
        return scope


def _bind_arguments(scope: _Scope, arguments: ast.arguments) -> None:
    for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
        scope.bind(arg.arg)
        if arg.annotation is not None:
            scope.arg_hints[arg.arg] = arg.annotation
    for arg in (arguments.vararg, arguments.kwarg):
        if arg is not None:
            scope.bind(arg.arg)


def _as_query_kind(name: str) -> QueryKind | None:
    # strict equality does not narrow `str` through `in QUERY_KINDS`
    if name == "query":
        return "query"
    if name == "query_unchecked":
        return "query_unchecked"
    return None


def _templatelib_tag(name: str) -> Tag:
    match name:
        case "Template":
            return "template"
        case "Interpolation":
            return "interpolation"
        case "*" | "convert":
            return "other"
    return "private"


def _is_private_name(name: str) -> bool:
    return name.startswith("_") and not _is_dunder(name)


# a private module of this package (for example `rowx._template`) may re-export `string.templatelib`
def _is_private_rowx_module(module: str) -> bool:
    root, *rest = module.split(".")
    return root == ROWX_MODULE and any(_is_private_name(part) for part in rest)


def _rowx_private_tag(module: str, name: str) -> Tag:
    if module.split(".", maxsplit=1)[0] != ROWX_MODULE:
        return "other"
    # a name imported from a private module is a function or class unless it re-exports `Template` or `Interpolation`
    if _is_private_rowx_module(module):
        return _templatelib_tag(name) if name in RE_EXPORTED else "other"
    return "rowxpriv" if _is_private_name(name) else "other"


def _import_tag(module: str, name: str) -> Tag:
    if module == ROWX_MODULE and name in QUERY_KINDS:
        return "query" if name == "query" else "query_unchecked"
    if module in TYPING_MODULES and name in TYPING_TAGS:
        return TYPING_TAGS[name]
    if module == TEMPLATELIB_MODULE:
        return _templatelib_tag(name)
    if module == STRING_MODULE and name == TEMPLATELIB_ATTR:
        return "templatelib"
    return _rowx_private_tag(module, name)


def _module_tag(alias: ast.alias) -> Tag:
    root = alias.name.split(".")[0]
    if root == ROWX_MODULE and (alias.asname is None or alias.name == ROWX_MODULE):
        return "module"
    # `import string.templatelib` binds `string`; with an alias the alias is the submodule
    if root == STRING_MODULE and (alias.asname is None or alias.name == STRING_MODULE):
        return "string"
    if root in TYPING_MODULES and (alias.asname is None or alias.name == root):
        return "typing"
    if alias.name == TEMPLATELIB_MODULE and alias.asname is not None:
        return "templatelib"
    if alias.asname is not None and _is_private_rowx_module(alias.name):
        return "rowxpriv"
    return "other"


def _bind_import(scope: _Scope, node: ast.Import | ast.ImportFrom) -> None:
    for alias in node.names:
        if isinstance(node, ast.ImportFrom):
            tag = _import_tag(node.module or "", alias.name) if node.level == 0 else "other"
            scope.bind(alias.asname or alias.name, tag)
        else:
            scope.bind(alias.asname or alias.name.split(".")[0], _module_tag(alias))


def _bind_pattern_name(scope: _Scope, node: ast.ExceptHandler | ast.MatchAs | ast.MatchStar | ast.MatchMapping) -> None:
    if (name := node.rest if isinstance(node, ast.MatchMapping) else node.name) is not None:
        scope.bind(name)


def _declared_names(node: ast.AST) -> tuple[str, ...]:
    match node:
        case ast.Global() | ast.Nonlocal():
            return tuple(node.names)
        case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
            return (node.name,)
    return ()


def _enclosing_expressions(node: ast.AST) -> list[ast.AST]:
    # the parts of a definition that are evaluated in the scope that contains it
    match node:
        case ast.FunctionDef() | ast.AsyncFunctionDef():
            found = [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]
        case ast.ClassDef():
            found = [*node.decorator_list, *node.bases, *node.keywords]
        case ast.Lambda():
            found = [*node.args.defaults, *node.args.kw_defaults]
        case _:
            found = []
    return [item for item in found if item is not None]


def _record_hints(scope: _Scope, node: ast.Assign | ast.AnnAssign) -> None:
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            scope.hints[node.target.id] = node.annotation
    else:
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if isinstance(node.value, ast.TemplateStr):
                scope.hints[target.id] = "literal"
            else:
                scope.aliases[target.id] = node.value


def _collect(scope: _Scope, nodes: Iterable[ast.AST], *, in_comprehension: bool = False) -> None:
    for node in nodes:
        match node:
            case (
                ast.FunctionDef()
                | ast.AsyncFunctionDef()
                | ast.ClassDef()
                | ast.Lambda()
                | ast.Global()
                | ast.Nonlocal()
            ):
                scope.bind_all(_declared_names(node))
                _collect(scope, _enclosing_expressions(node), in_comprehension=in_comprehension)
            case ast.ListComp() | ast.SetComp() | ast.GeneratorExp() | ast.DictComp():
                # only a walrus inside a comprehension binds in the enclosing scope
                _collect(scope, ast.iter_child_nodes(node), in_comprehension=True)
            case ast.Import() | ast.ImportFrom():
                _bind_import(scope, node)
            case ast.Name() if isinstance(node.ctx, ast.Store) and not in_comprehension:
                scope.bind(node.id)
            case ast.Assign() | ast.AnnAssign():
                _record_hints(scope, node)
                _collect(scope, ast.iter_child_nodes(node), in_comprehension=in_comprehension)
            case ast.NamedExpr():
                scope.bind(node.target.id)
                _collect(scope, [node.value], in_comprehension=in_comprehension)
            case ast.ExceptHandler() | ast.MatchAs() | ast.MatchStar() | ast.MatchMapping():
                _bind_pattern_name(scope, node)
                _collect(scope, ast.iter_child_nodes(node), in_comprehension=in_comprehension)
            case _:
                _collect(scope, ast.iter_child_nodes(node), in_comprehension=in_comprehension)


@dataclass(slots=True, frozen=True)
class _Ref:
    kind: QueryKind
    ambiguous: bool


@dataclass(slots=True, frozen=True)
class _TemplatelibRef:
    name: str
    ambiguous: bool
    private: bool = False


def _is_dunder(name: str) -> bool:
    return len(name) > len(DUNDER) * 2 and name.startswith(DUNDER) and name.endswith(DUNDER)


def _is_name_chain(node: ast.expr) -> bool:
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _is_type_alias(annotation: ast.expr) -> bool:
    match annotation:
        case ast.Name(id="TypeAlias") | ast.Attribute(attr="TypeAlias"):
            return True
    return False


def _is_dynamic_spec(part: ast.Interpolation) -> bool:
    return isinstance(part.format_spec, ast.JoinedStr) and not all(
        isinstance(value, ast.Constant) for value in part.format_spec.values
    )


class _Finder(ast.NodeVisitor):
    def __init__(self, path: Path, tree: ast.Module, lines: _Lines) -> None:
        self._path = path
        self._lines = lines
        # a name declared `global` or `nonlocal` anywhere can be rebound from another scope
        self._declared = {
            name for node in ast.walk(tree) if isinstance(node, (ast.Global, ast.Nonlocal)) for name in node.names
        }
        self._scopes = [_Scope.build(tree)]
        self._resolve_hints(self._scopes[0], [])
        self._annotating = 0
        self.result = FileResult(path=path, tree=tree)
        self._template_calls: list[TemplateCall] = []
        self._indirect_uses: list[IndirectUse] = []
        self._concats: list[TemplateConcat] = []
        self._dynamic_specs: list[DynamicFormatSpec] = []
        # interpolations of the sole t-string argument of a query call, and the t-string chains seen so far
        self._direct: dict[int, QueryKind] = {}
        self._chains: set[int] = set()
        # nodes that are the base of an attribute, so a module there is not used as a value
        self._bases: set[int] = set()
        # `type(q)` / `q.__class__` nodes already recorded as the callable of a call
        self._skip: set[int] = set()
        # nesting depth of type-level positions (subscripts, the type argument of `cast`)
        self._type_level = 0
        # nesting depth of nodes already recorded as template facts, which are only checked for query names
        self._query_only = 0

    def facts(self) -> TemplateFacts:
        return TemplateFacts(
            calls=tuple(self._template_calls),
            indirect_uses=tuple(self._indirect_uses),
            concats=tuple(self._concats),
            dynamic_specs=tuple(self._dynamic_specs),
        )

    def _report(self, rule: Rule, message: str, node: ast.expr | ast.stmt | ast.alias) -> None:
        self.result.diagnostics.append(
            Diagnostic(rule=rule, message=message, path=self._path, span=self._lines.span(node)),
        )

    def _lookup_scope(self, name: str) -> _Scope | None:
        # a class body is only visible to code directly in it (including its annotations), not to
        # functions or comprehensions nested in it
        in_code = False
        for scope in reversed(self._scopes):
            if scope.kind == "class" and in_code:
                continue
            if name in scope.bindings:
                return scope
            in_code = in_code or scope.kind in {"function", "comprehension"}
        return None

    def _lookup(self, name: str) -> list[Tag] | None:
        return None if (scope := self._lookup_scope(name)) is None else scope.bindings[name]

    # the first binding of `name` whose tag is in `wanted`; ambiguous when the name has any other binding
    def _tags(self, name: str, wanted: frozenset[Tag]) -> tuple[Tag | None, bool]:
        if (tags := self._lookup(name)) is None or (len(tags) == 1 and tags[0] not in wanted):
            return None, False
        tag = next((tag for tag in tags if tag in wanted), None)
        return tag, tag is not None and len(set(tags)) > 1

    # a name with no binding, or only plain ones, that no tagged resolution can concern
    def _plain(self, node: ast.expr) -> bool:
        while isinstance(node, ast.Attribute):
            node = node.value
        if not isinstance(node, ast.Name):
            return False
        tags = self._lookup(node.id)
        return tags is None or (len(tags) == 1 and tags[0] == "other")

    def _module_name(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and self._tags(node.id, MODULE_TAG)[0] == "module"

    def _ref(self, node: ast.expr) -> _Ref | None:
        match node:
            case ast.Name():
                tag, ambiguous = self._tags(node.id, QUERY_TAGS)
                if tag in QUERY_KINDS:
                    return _Ref(tag, ambiguous)
            case ast.Attribute(value=ast.Name() as base, attr=attr) if (kind := _as_query_kind(attr)) is not None:
                tag, ambiguous = self._tags(base.id, MODULE_TAG)
                if tag == "module":
                    return _Ref(kind, ambiguous)
        return None

    # whether `node` is the `string.templatelib` module, and whether that is ambiguous
    def _templatelib_module(self, node: ast.expr) -> bool | None:
        match node:
            case ast.Name():
                tag, ambiguous = self._tags(node.id, TEMPLATELIB_TAG)
                want = "templatelib"
            case ast.Attribute(value=ast.Name() as root, attr=attr) if attr == TEMPLATELIB_ATTR:
                tag, ambiguous = self._tags(root.id, STRING_TAG)
                want = "string"
            case _:
                return None
        return ambiguous if tag == want else None

    # whether `node` is a private module of this package, which may re-export `string.templatelib` names
    def _private_module(self, node: ast.expr) -> bool | None:
        match node:
            case ast.Name():
                tag, ambiguous = self._tags(node.id, PRIVATE_MODULE_TAG)
                return ambiguous if tag == "rowxpriv" else None
            case ast.Attribute(value=base, attr=attr):
                if not _is_private_name(attr):
                    return None
                package = self._rowx_package(base)
                return package if package is not None else self._private_module(base)
        return None

    # whether `node` is `rowx` or one of its submodules, and whether that is ambiguous
    def _rowx_package(self, node: ast.expr) -> bool | None:
        match node:
            case ast.Name():
                tag, ambiguous = self._tags(node.id, MODULE_TAG)
                return ambiguous if tag == "module" else None
            case ast.Attribute(value=base):
                return self._rowx_package(base)
        return None

    def _any_module(self, node: ast.expr) -> bool | None:
        found = self._templatelib_module(node)
        return found if found is not None else self._private_module(node)

    def _templatelib_ref(self, node: ast.expr) -> _TemplatelibRef | None:
        match node:
            case ast.Name():
                tag, ambiguous = self._tags(node.id, CLASS_TAGS)
                match tag:
                    case "template":
                        return _TemplatelibRef("Template", ambiguous)
                    case "interpolation":
                        return _TemplatelibRef("Interpolation", ambiguous)
                    case "private":
                        return _TemplatelibRef(node.id, ambiguous, private=True)
            case ast.Attribute(value=base, attr=attr) if attr != "convert":
                if (ambiguous := self._templatelib_module(base)) is not None:
                    return _TemplatelibRef(attr, ambiguous, private=attr not in TEMPLATELIB_PUBLIC)
                if attr in RE_EXPORTED and (ambiguous := self._private_module(base)) is not None:
                    return _TemplatelibRef(attr, ambiguous)
        return None

    # `X | None`, `Optional[X]` and `"X | None"` are `X`; a union of several non-None types is unknown
    def _without_none(self, annotation: ast.expr) -> ast.expr | None:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value.strip(), mode="eval").body
            except SyntaxError:
                return None
        members: list[ast.expr] = []
        pending = [annotation]
        while pending:
            match pending.pop():
                case ast.BinOp(left=left, op=ast.BitOr(), right=right):
                    pending.extend([right, left])
                case ast.Constant(value=None):
                    pass
                case ast.Subscript(value=value, slice=inner) if self._is_optional(value):
                    pending.append(inner)
                case other:
                    members.append(other)
        return members[0] if len(members) == 1 else None

    # `Optional` resolved through its binding: `from typing import Optional [as X]` or `typing.Optional`
    def _is_optional(self, node: ast.expr) -> bool:
        match node:
            case ast.Name(id=name):
                tag, ambiguous = self._tags(name, OPTIONAL_TAG)
                return tag == "optional" and not ambiguous
            case ast.Attribute(value=ast.Name() as base, attr="Optional"):
                tag, ambiguous = self._tags(base.id, TYPING_TAG)
                return tag == "typing" and not ambiguous
        return False

    def _is_template_value(self, node: ast.expr) -> bool:
        match node:
            case ast.TemplateStr():
                return True
            case ast.Call(func=func):
                return (ref := self._templatelib_ref(func)) is not None and ref.name == "Template" and not ref.ambiguous
            case ast.Name(id=name) if (scope := self._lookup_scope(name)) is not None:
                return name not in self._declared and len(scope.bindings[name]) == 1 and name in scope.templates
        return False

    def _is_template_annotation(self, annotation: ast.expr) -> bool:
        if (inner := self._without_none(annotation)) is None:
            return False
        return (ref := self._templatelib_ref(inner)) is not None and ref.name == "Template" and not ref.ambiguous

    # resolve the hints of a scope where their annotations are evaluated: `enclosing` is the scope stack around it
    def _resolve_hints(self, scope: _Scope, enclosing: list[_Scope]) -> None:
        own = [*enclosing, scope]
        stack, self._scopes = self._scopes, enclosing
        scope.templates.update(name for name, hint in scope.arg_hints.items() if self._is_template_annotation(hint))
        self._scopes = own
        scope.templates.update(
            name for name, hint in scope.hints.items() if hint == "literal" or self._is_template_annotation(hint)
        )
        # `y = x` where `x` is evidently a `Template`; each pass can settle a name that depends on a later one
        pending = {name: value for name, value in scope.aliases.items() if name not in scope.templates}
        while settled := [name for name, value in pending.items() if self._is_template_value(value)]:
            scope.templates.update(settled)
            pending = {name: value for name, value in pending.items() if name not in settled}
        self._scopes = stack

    # `type(<template>)`
    def _is_type_of_template(self, node: ast.expr) -> bool:
        match node:
            case ast.Call(func=ast.Name(id="type") as func, args=[arg], keywords=[]):
                return self._lookup(func.id) is None and self._is_template_value(arg)
        return False

    def _is_class_of_template(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Attribute) and node.attr == "__class__" and self._is_template_value(node.value)

    def _is_cast(self, node: ast.expr) -> bool:
        match node:
            case ast.Name():
                tag, ambiguous = self._tags(node.id, CAST_TAG)
                return tag == "cast" and not ambiguous
            case ast.Attribute(value=ast.Name() as base, attr="cast"):
                tag, ambiguous = self._tags(base.id, TYPING_TAG)
                return tag == "typing" and not ambiguous
        return False

    def _is_annotated(self, node: ast.expr) -> bool:
        match node:
            case ast.Name():
                tag, ambiguous = self._tags(node.id, ANNOTATED_TAG)
                return tag == "annotated" and not ambiguous
            case ast.Attribute(value=ast.Name() as base, attr="Annotated"):
                tag, ambiguous = self._tags(base.id, TYPING_TAG)
                return tag == "typing" and not ambiguous
        return False

    # a position that names a type: a name, attribute chain, subscript of those, `|` union, string, `None` or a
    # tuple (or list, as in `Callable[[X], Y]`) of those. Anything else is a runtime value even inside a subscript.
    def _is_type_expr(self, node: ast.expr) -> bool:
        match node:
            case ast.Name() | ast.Constant():
                return True
            case ast.Attribute():
                return _is_name_chain(node)
            case ast.Subscript(value=value, slice=item):
                return _is_name_chain(value) and self._is_type_expr(item)
            case ast.BinOp(left=left, op=ast.BitOr(), right=right):
                parts = [left, right]
            case ast.Tuple(elts=parts) | ast.List(elts=parts):
                pass
            case ast.Starred(value=value):
                parts = [value]
            case _:
                return False
        return all(self._is_type_expr(part) for part in parts)

    # visit `node` where a type is expected: the parts that are types are type-level, the rest are runtime values
    def _type_level_visit(self, node: ast.expr) -> None:
        if isinstance(node, ast.Tuple):
            for item in node.elts:
                self._type_level_visit(item)
        elif self._is_type_expr(node):
            self._type_level += 1
            self.visit(node)
            self._type_level -= 1
        else:
            type_level, self._type_level = self._type_level, 0
            self.visit(node)
            self._type_level = type_level

    def _exports(self, target: ast.expr, value: ast.expr | None) -> None:
        if len(self._scopes) != 1 or not isinstance(target, ast.Name) or target.id != "__all__":
            return
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return
        for item in value.elts:
            if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                continue
            if any(tag in EXPORT_TAGS for tag in self._scopes[0].bindings.get(item.value, ())):
                self._indirect_uses.append(IndirectUse(kind="re-export", name=item.value, span=self._lines.span(item)))

    def _template_use(self, ref: _TemplatelibRef, node: ast.expr, kind: IndirectKind) -> None:
        if ref.private and kind in {"value", "attribute"}:
            kind = "private"
        self._indirect_uses.append(
            IndirectUse(kind="ambiguous" if ref.ambiguous else kind, name=ref.name, span=self._lines.span(node)),
        )

    def _indirect(self, node: ast.expr | ast.stmt | ast.alias, what: str) -> None:
        self._report("indirect-query", f'{what} is only allowed as `query[T](t"...")` or in an annotation', node)

    def _visit_all(self, nodes: Iterable[ast.AST | None]) -> None:
        for node in nodes:
            if node is not None:
                self.visit(node)

    def _annotation(self, node: ast.expr | None) -> None:
        if node is not None:
            self._annotating += 1
            self.visit(node)
            self._annotating -= 1

    def _type_params(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.TypeAlias) -> int:
        # PEP 695 type parameters live in their own scope between the definition and its parts
        if not node.type_params:
            return 0
        scope = _Scope("annotation")
        scope.bind_all(param.name for param in node.type_params if isinstance(param, TYPE_PARAMS))
        self._scopes.append(scope)
        return 1

    def _scoped(self, node: Scoped | Comprehension, body: Iterable[ast.AST | None]) -> None:
        scope = _Scope.build(node)
        self._resolve_hints(scope, self._scopes)
        self._scopes.append(scope)
        self._visit_all(body)
        self._scopes.pop()

    @override
    def visit_Name(self, node: ast.Name) -> None:
        if self._annotating or not isinstance(node.ctx, ast.Load):
            return
        if self._plain(node):
            return
        if (ref := self._ref(node)) is not None:
            self._indirect(node, f"`{node.id}` (rowx.{ref.kind})")
        if self._type_level or self._query_only:
            return
        if (template_ref := self._templatelib_ref(node)) is not None:
            self._template_use(template_ref, node, "value")
        elif id(node) not in self._bases:
            tag, ambiguous = self._tags(node.id, MODULE_VALUE_TAGS)
            if tag is not None:
                self._template_use(_TemplatelibRef(tag, ambiguous), node, "module-value")

    @override
    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._bases.add(id(node.value))
        # reading the name of the class builds nothing
        if node.attr in NAME_ATTRIBUTES and self._is_type_of_template(node.value):
            self._skip.add(id(node.value))
        plain = self._plain(node)
        if not plain and (query := self._ref(node)) is not None:
            if not self._annotating:
                self._indirect(node, f"`rowx.{query.kind}`")
        elif not plain and self._resolve_attribute(node):
            # the base may also be a query name, which still has to be reported
            self._query_only_visit(node)
        else:
            recording = not (self._annotating or self._type_level or self._query_only) and id(node) not in self._skip
            if recording and self._is_template_attribute(node):
                self._indirect_uses.append(IndirectUse(kind="attribute", name="Template", span=self._lines.span(node)))
            self.generic_visit(node)

    def _query_only_visit(self, node: ast.AST) -> None:
        self._query_only += 1
        self.generic_visit(node)
        self._query_only -= 1

    # whether `node` resolved to a rowx or `string.templatelib` name, which is then recorded
    def _resolve_attribute(self, node: ast.Attribute) -> bool:
        recording = not (self._annotating or self._type_level or self._query_only)
        if (template_ref := self._templatelib_ref(node)) is not None:
            if recording:
                self._template_use(template_ref, node, "value")
        elif (base_ref := self._templatelib_ref(node.value)) is not None:
            if recording:
                self._template_use(base_ref, node, "attribute")
        elif (ambiguous := self._any_module(node)) is not None:
            if recording and id(node) not in self._bases:
                self._template_use(_TemplatelibRef(TEMPLATELIB_ATTR, ambiguous), node, "module-value")
        else:
            return False
        return True

    def _is_template_attribute(self, node: ast.Attribute) -> bool:
        if isinstance(node.value, ast.TemplateStr):
            return _is_dunder(node.attr)
        return self._is_class_of_template(node)

    @override
    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._type_level:
            self.generic_visit(node)
            return
        # `Annotated[X, ...]` is callable and builds an `X`, so outside an annotation `X` is a runtime value
        if not self._annotating and self._is_annotated(node.value):
            self.visit(node.value)
            if isinstance(node.slice, ast.Tuple) and node.slice.elts:
                self.visit(node.slice.elts[0])
                for item in node.slice.elts[1:]:
                    self._type_level_visit(item)
            else:
                self.visit(node.slice)
            return
        # only the subscript of a name is a generic alias; the value of any other subscript is a runtime value
        self.visit(node.value)
        if _is_name_chain(node.value):
            self._type_level_visit(node.slice)
        else:
            self.visit(node.slice)

    @override
    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._exports(target, node.value)
        self.generic_visit(node)

    @override
    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._exports(node.target, node.value)
        self.generic_visit(node)

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0 or node.module is None:
            return
        module = node.module
        for alias in node.names:
            if module == TEMPLATELIB_MODULE or _is_private_rowx_module(module):
                if alias.name == "*":
                    self._indirect_uses.append(IndirectUse(kind="star-import", name=None, span=self._lines.span(node)))
                elif module == TEMPLATELIB_MODULE and alias.name not in TEMPLATELIB_PUBLIC:
                    self._indirect_uses.append(
                        IndirectUse(kind="private", name=alias.name, span=self._lines.span(alias)),
                    )
            elif module == ROWX_MODULE and alias.name == "*":
                self._indirect(node, "`from rowx import *`")

    @override
    def visit_MatchClass(self, node: ast.MatchClass) -> None:
        # a class pattern only inspects the class
        self._query_only_visit(node.cls)
        self._visit_all((*node.patterns, *node.kwd_patterns))

    @override
    def visit_Compare(self, node: ast.Compare) -> None:
        # comparing with the class (`type(x) is Template`) only inspects it
        if all(isinstance(op, IDENTITY_OPERATORS) for op in node.ops):
            for operand in (node.left, *node.comparators):
                if self._is_type_of_template(operand):
                    self._skip.add(id(operand))
                elif not self._plain(operand) and self._templatelib_ref(operand) is not None:
                    self._query_only_visit(operand)
                    continue
                self.visit(operand)
            return
        self.generic_visit(node)

    @override
    def visit_MatchValue(self, node: ast.MatchValue) -> None:
        # a value pattern only compares with the class
        self._query_only_visit(node.value)

    @override
    def visit_Interpolation(self, node: ast.Interpolation) -> None:
        if _is_dynamic_spec(node):
            self._dynamic_specs.append(
                DynamicFormatSpec(
                    node=node,
                    span=self._lines.span(node),
                    direct_argument_of=self._direct.get(id(node)),
                ),
            )
        self.generic_visit(node)

    @override
    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.generic_visit(node)
        if not isinstance(node.op, ast.Add):
            return
        left = isinstance(node.left, ast.TemplateStr) or id(node.left) in self._chains
        right = isinstance(node.right, ast.TemplateStr) or id(node.right) in self._chains
        if not (left or right):
            return
        self._chains.add(id(node))
        operand = node.right if left else node.left
        self._concats.append(
            TemplateConcat(
                node=node,
                operand=operand,
                span=self._lines.span(node),
                operand_span=self._lines.span(operand),
                evident=id(operand) in self._chains or self._is_template_value(operand),
            ),
        )

    @override
    def visit_Call(self, node: ast.Call) -> None:
        target = node.func
        subscript = target if isinstance(target, ast.Subscript) else None
        if subscript is not None:
            target = subscript.value
        annotating, self._annotating = self._annotating, 0
        type_level, self._type_level = self._type_level, 0
        plain = self._plain(target)
        if not plain and (ref := self._ref(target)) is not None:
            if (template_ref := self._templatelib_ref(target)) is not None:
                self._template_use(template_ref, target, "ambiguous")
            self._visit_query(node, target, subscript, ref)
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id in TYPE_CHECK_FUNCTIONS
            # the builtin only when the name is unbound
            and self._lookup(node.func.id) is None
        ):
            self._visit_type_check(node)
        elif not plain and (template_ref := self._templatelib_ref(node.func)) is not None:
            self._visit_template_call(node, template_ref)
        elif not plain and self._is_cast(node.func):
            self._visit_cast(node)
        else:
            self._record_type_of_template(node)
            is_getattr = isinstance(node.func, ast.Name) and node.func.id == "getattr"
            if is_getattr and node.args and self._module_name(node.args[0]):
                self._indirect(node.args[0], "`getattr(rowx, ...)`")
            self.generic_visit(node)
        self._annotating = annotating
        self._type_level = type_level

    def _class_operands(self, node: ast.expr) -> list[ast.expr]:
        if isinstance(node, ast.Tuple):
            return [operand for elt in node.elts for operand in self._class_operands(elt)]
        return [node]

    def _visit_type_check(self, node: ast.Call) -> None:
        self.visit(node.func)
        # `issubclass(type(q), ...)` only inspects the class
        if len(node.args) > 1:
            self._skip.update(id(arg) for arg in self._class_operands(node.args[1]) if self._is_type_of_template(arg))
        if node.args and self._is_type_of_template(node.args[0]):
            self._skip.add(id(node.args[0]))
        # the first argument of `issubclass`, and the second of both, name classes
        is_subclass = isinstance(node.func, ast.Name) and node.func.id == "issubclass"
        self._visit_all(node.args[:0] if is_subclass else node.args[:1])
        class_args = node.args if is_subclass else node.args[1:]
        for operand in (operand for arg in class_args for operand in self._class_operands(arg)):
            if self._is_type_expr(operand):
                self._annotating += 1
                self.visit(operand)
                self._annotating -= 1
            else:
                annotating, self._annotating = self._annotating, 0
                self.visit(operand)
                self._annotating = annotating
        self._visit_all(node.keywords)

    def _visit_cast(self, node: ast.Call) -> None:
        self.visit(node.func)
        for index, arg in enumerate(node.args):
            self._type_level_visit(arg) if index == 0 else self.visit(arg)
        for keyword in node.keywords:
            self._type_level_visit(keyword.value) if keyword.arg == "typ" else self.visit(keyword)

    def _record_type_of_template(self, node: ast.Call) -> None:
        if self._is_class_of_template(node.func) or self._is_type_of_template(node.func):
            self._skip.add(id(node.func))
            self._indirect_uses.append(IndirectUse(kind="type-call", name=None, span=self._lines.span(node.func)))
        elif id(node) not in self._skip and self._is_type_of_template(node):
            self._indirect_uses.append(IndirectUse(kind="value", name="Template", span=self._lines.span(node)))

    def _visit_template_call(self, node: ast.Call, ref: _TemplatelibRef) -> None:
        if ref.ambiguous:
            self._template_use(ref, node.func, "ambiguous")
        else:
            self._template_calls.append(
                TemplateCall(name=ref.name, private=ref.private, node=node, span=self._lines.span(node)),
            )
        self._query_only_visit(node.func)
        self._visit_all(node.args)
        self._visit_all(node.keywords)

    def _mark_direct(self, node: ast.Call, kind: QueryKind) -> None:
        if len(node.args) != 1 or isinstance(node.args[0], ast.Starred):
            return
        if (kind == "query" and node.keywords) or not isinstance(template := node.args[0], ast.TemplateStr):
            return
        for part in template.values:
            if isinstance(part, ast.Interpolation):
                self._direct[id(part)] = kind

    def _visit_query(self, node: ast.Call, target: ast.expr, subscript: ast.Subscript | None, ref: _Ref) -> None:
        if ref.ambiguous:
            self._indirect(target, "an ambiguous name that is also bound another way in its scope")
        else:
            self._mark_direct(node, ref.kind)
        if subscript is not None:
            self.visit(subscript.slice)
        self._visit_all(node.args)
        self._visit_all(node.keywords)
        if ref.ambiguous:
            return
        row_type = subscript.slice if subscript is not None else None
        row_type_span = self._lines.span(row_type) if row_type is not None else None
        if ref.kind == "query_unchecked":
            self.result.unchecked_count += 1
            self._inspect_unchecked(node)
            template = None
        elif (template := self._template(node)) is None:
            return
        self.result.calls.append(
            QueryCall(
                path=self._path,
                kind=ref.kind,
                span=self._lines.span(node),
                row_type=row_type,
                row_type_span=row_type_span,
                template=template,
            ),
        )

    def _inspect_unchecked(self, node: ast.Call) -> None:
        # the SQL of a query_unchecked call is not checked, but a format spec that is not a literal could evaluate
        # to "ident" at runtime
        if len(node.args) != 1 or not isinstance(template := node.args[0], ast.TemplateStr):
            return
        for part in template.values:
            if isinstance(part, ast.Interpolation) and _is_dynamic_spec(part):
                self._report(
                    "dynamic-query",
                    "a format spec in a `query_unchecked` interpolation must be a literal",
                    part,
                )

    def _template(self, node: ast.Call) -> ast.TemplateStr | None:
        if len(node.args) != 1 or node.keywords or isinstance(node.args[0], ast.Starred):
            self._report("dynamic-query", "`query` takes exactly one positional t-string argument", node)
            return None
        template = node.args[0]
        if not isinstance(template, ast.TemplateStr):
            self._report("dynamic-query", "the argument of `query` must be an inline t-string", template)
            return None
        ok = True
        for part in template.values:
            if not isinstance(part, ast.Interpolation):
                continue
            if part.conversion != -1:
                message = "a conversion (including `{x=}`) is not allowed in a query interpolation"
            elif part.format_spec is not None:
                message = "a format spec is not allowed in a query interpolation"
            elif isinstance(part.value, ast.TemplateStr):
                message = "a nested t-string is not allowed in a query interpolation"
            else:
                continue
            self._report("dynamic-query", message, template)
            ok = False
        return template if ok else None

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._visit_all(node.decorator_list)
        pushed = self._type_params(node)
        self._visit_all((*node.args.defaults, *node.args.kw_defaults))
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg):
            if arg is not None:
                self._annotation(arg.annotation)
        self._annotation(node.returns)
        self._scoped(node, node.body)
        del self._scopes[len(self._scopes) - pushed :]

    visit_AsyncFunctionDef = visit_FunctionDef  # ruff: ignore[mixed-case-variable-in-class-scope]

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_all((*node.args.defaults, *node.args.kw_defaults))
        self._scoped(node, [node.body])

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_all(node.decorator_list)
        pushed = self._type_params(node)
        self._visit_all((*node.bases, *node.keywords))
        self._scoped(node, node.body)
        del self._scopes[len(self._scopes) - pushed :]

    @override
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._annotation(node.annotation)
        self._exports(node.target, node.value)
        if (
            node.value is not None
            and _is_type_alias(node.annotation)
            and (ref := self._templatelib_ref(node.value)) is not None
        ):
            self._template_use(ref, node.value, "alias")
            self.visit(node.target)
            self._query_only += 1
            self.visit(node.value)
            self._query_only -= 1
            return
        self._visit_all((node.target, node.value))

    @override
    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        pushed = self._type_params(node)
        if (ref := self._templatelib_ref(node.value)) is not None:
            self._template_use(ref, node.value, "alias")
        else:
            self._annotation(node.value)
        del self._scopes[len(self._scopes) - pushed :]

    def _visit_comprehension(self, node: Comprehension) -> None:
        # the first iterable is evaluated in the enclosing scope
        self.visit(node.generators[0].iter)
        body: list[ast.AST] = []
        for index, generator in enumerate(node.generators):
            if index > 0:
                body.append(generator.iter)
            body.extend(generator.ifs)
        body.extend([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
        self._scoped(node, body)

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _visit_comprehension  # ruff: ignore[mixed-case-variable-in-class-scope]


def _syntax_error(path: Path, message: str, span: Span) -> FileResult:
    return FileResult(path=path, diagnostics=[Diagnostic(rule="syntax-error", message=message, path=path, span=span)])


def too_deep(path: Path) -> FileResult:
    return _syntax_error(path, "the file is nested too deeply to analyse", Span(line=1, col=1, end_line=1, end_col=2))


def _decode(path: Path, data: bytes) -> str | FileResult:
    try:
        encoding, _ = detect_encoding(BytesIO(data).readline)
        return data.decode(encoding)
    except SyntaxError as error:
        line = error.lineno or 1
        return _syntax_error(path, error.msg, Span(line=line, col=1, end_line=line, end_col=2))
    except LookupError as error:
        # a recognised codec that isn't a text encoding, such as `base64_codec`
        message = f"cannot decode file: {error}"
        return _syntax_error(path, message, Span(line=1, col=1, end_line=1, end_col=2))
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        col = error.start - (data.rfind(b"\n", 0, error.start) + 1) + 1
        message = f"cannot decode file as {error.encoding}: {error.reason}"
        return _syntax_error(path, message, Span(line=line, col=col, end_line=line, end_col=col + 1))


def discover_source(path: Path, data: bytes) -> FileResult:
    source = _decode(path, data)
    if isinstance(source, FileResult):
        return source
    lines = _Lines(source)
    try:
        tree = ast.parse(source, str(path))
    except SyntaxError as error:
        line = error.lineno or 1
        col = error.offset or 1
        end_line = error.end_lineno or line
        end_col = error.end_offset or col + 1
        end_col = max(end_col, col + 1) if end_line == line else end_col
        return _syntax_error(path, error.msg, Span(line=line, col=col, end_line=end_line, end_col=end_col))
    except (ValueError, RecursionError, MemoryError) as error:
        return _syntax_error(path, f"cannot parse file: {error}", Span(line=1, col=1, end_line=1, end_col=2))
    try:
        finder = _Finder(path, tree, lines)
        finder.visit(tree)
    except RecursionError:
        return too_deep(path)
    finder.result.templates = finder.facts()
    finder.result.source = source
    return finder.result


def discover_file(path: Path) -> FileResult:
    return discover_source(path, path.read_bytes())
