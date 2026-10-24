import ast
from dataclasses import dataclass, field
from io import BytesIO
from re import compile as re_compile
from tokenize import detect_encoding
from typing import TYPE_CHECKING, Final, Literal, Self, override

from rowx.check.model import QUERY_KINDS, Diagnostic, FileResult, QueryCall, QueryKind, Rule, Span

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

ROWX_MODULE: Final[str] = "rowx"
TYPE_CHECK_FUNCTIONS: Final[frozenset[str]] = frozenset({"isinstance", "issubclass"})
TYPE_PARAMS: Final = (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)
LINE_PATTERN: Final = re_compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")

type Tag = Literal["query", "query_unchecked", "module", "other"]
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
    for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg):
        if arg is not None:
            scope.bind(arg.arg)


def _import_tag(module: str, name: str) -> Tag:
    if module == ROWX_MODULE and name in QUERY_KINDS:
        return "query" if name == "query" else "query_unchecked"
    return "other"


def _bind_import(scope: _Scope, node: ast.Import | ast.ImportFrom) -> None:
    for alias in node.names:
        if isinstance(node, ast.ImportFrom):
            tag = _import_tag(node.module or "", alias.name) if node.level == 0 else "other"
            scope.bind(alias.asname or alias.name, tag)
        else:
            root = alias.name.split(".")[0]
            is_module = root == ROWX_MODULE and (alias.asname is None or alias.name == ROWX_MODULE)
            scope.bind(alias.asname or root, "module" if is_module else "other")


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


class _Finder(ast.NodeVisitor):
    def __init__(self, path: Path, tree: ast.Module, lines: _Lines) -> None:
        self._path = path
        self._lines = lines
        self._scopes = [_Scope.build(tree)]
        self._annotating = 0
        self.result = FileResult(path=path, tree=tree)

    def _report(self, rule: Rule, message: str, node: ast.expr | ast.stmt | ast.alias) -> None:
        self.result.diagnostics.append(
            Diagnostic(rule=rule, message=message, path=self._path, span=self._lines.span(node)),
        )

    def _lookup(self, name: str) -> list[Tag] | None:
        # a class body is only visible to code directly in it (including its annotations), not to
        # functions or comprehensions nested in it
        in_code = False
        for scope in reversed(self._scopes):
            if scope.kind == "class" and in_code:
                continue
            if name in scope.bindings:
                return scope.bindings[name]
            in_code = in_code or scope.kind in {"function", "comprehension"}
        return None

    def _tags(self, name: str) -> tuple[Tag | None, bool]:
        tags = self._lookup(name)
        if tags is None:
            return None, False
        distinct = set(tags)
        if distinct == {"other"}:
            return None, False
        if len(distinct) == 1:
            return tags[0], False
        return next(tag for tag in tags if tag != "other"), True

    def _module_name(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and self._tags(node.id)[0] == "module"

    def _ref(self, node: ast.expr) -> _Ref | None:
        match node:
            case ast.Name():
                tag, ambiguous = self._tags(node.id)
                if tag in QUERY_KINDS:
                    return _Ref(tag, ambiguous)
            case ast.Attribute(value=ast.Name() as base, attr=attr) if attr in QUERY_KINDS:
                tag, ambiguous = self._tags(base.id)
                if tag == "module":
                    return _Ref(attr, ambiguous)
        return None

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
        self._scopes.append(_Scope.build(node))
        self._visit_all(body)
        self._scopes.pop()

    @override
    def visit_Name(self, node: ast.Name) -> None:
        if self._annotating or not isinstance(node.ctx, ast.Load):
            return
        if (ref := self._ref(node)) is not None:
            self._indirect(node, f"`{node.id}` (rowx.{ref.kind})")

    @override
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (ref := self._ref(node)) is None:
            self.generic_visit(node)
        elif not self._annotating:
            self._indirect(node, f"`rowx.{ref.kind}`")

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module == ROWX_MODULE:
            for alias in node.names:
                if alias.name == "*":
                    self._indirect(node, "`from rowx import *`")

    @override
    def visit_Call(self, node: ast.Call) -> None:
        target = node.func
        subscript = target if isinstance(target, ast.Subscript) else None
        if subscript is not None:
            target = subscript.value
        annotating, self._annotating = self._annotating, 0
        if (ref := self._ref(target)) is not None:
            self._visit_query(node, target, subscript, ref)
        elif isinstance(node.func, ast.Name) and node.func.id in TYPE_CHECK_FUNCTIONS:
            self.visit(node.func)
            self._visit_all(node.args[:1])
            self._annotating += 1
            self._visit_all(node.args[1:])
            self._annotating -= 1
            self._visit_all(node.keywords)
        else:
            is_getattr = isinstance(node.func, ast.Name) and node.func.id == "getattr"
            if is_getattr and node.args and self._module_name(node.args[0]):
                self._indirect(node.args[0], "`getattr(rowx, ...)`")
            self.generic_visit(node)
        self._annotating = annotating

    def _visit_query(self, node: ast.Call, target: ast.expr, subscript: ast.Subscript | None, ref: _Ref) -> None:
        if ref.ambiguous:
            self._indirect(target, "an ambiguous name that is also bound another way in its scope")
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

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

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
        self._visit_all((node.target, node.value))

    @override
    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        pushed = self._type_params(node)
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

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _visit_comprehension  # noqa: N815


def _syntax_error(path: Path, message: str, span: Span) -> FileResult:
    return FileResult(path=path, diagnostics=[Diagnostic(rule="syntax-error", message=message, path=path, span=span)])


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
    finder = _Finder(path, tree, lines)
    finder.visit(tree)
    return finder.result


def discover_file(path: Path) -> FileResult:
    return discover_source(path, path.read_bytes())
