from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    import ast
    from pathlib import Path

type Rule = Literal[
    "syntax-error",
    "indirect-query",
    "dynamic-query",
    "unresolved-row-type",
    "invalid-row-type",
    "sql-error",
    "param-count-mismatch",
    "missing-row-type",
    "unexpected-row-type",
    "arity-mismatch",
    "missing-column",
    "unexpected-column",
    "duplicate-column",
    "ambiguous-column",
    "column-name-mismatch",
    "unsupported-type",
    "type-mismatch",
    "nullability-mismatch",
    "invalid-override",
    "param-type-mismatch",
]
type QueryKind = Literal["query", "query_unchecked"]
type TemplatelibName = Literal["Template", "Interpolation"]
# "value": the class used as a value; "attribute": an attribute access other than a direct call; "star-import":
# `from string.templatelib import *`; "type-call": `type(<t-string>)` or `<t-string>.__class__` used as a callable;
# "ambiguous": a name that is also bound another way in its scope
type IndirectKind = Literal["value", "attribute", "star-import", "type-call", "ambiguous"]

QUERY_KINDS: Final[tuple[QueryKind, ...]] = ("query", "query_unchecked")


# line is 1-based, col is a 1-based code point index, and the end is exclusive
@dataclass(slots=True, kw_only=True, frozen=True)
class Span:
    line: int
    col: int
    end_line: int
    end_col: int


@dataclass(slots=True, kw_only=True, frozen=True)
class Diagnostic:
    rule: Rule
    message: str
    path: Path
    span: Span
    notes: tuple[str, ...] = ()


@dataclass(slots=True, kw_only=True, frozen=True)
class QueryCall:
    path: Path
    kind: QueryKind
    span: Span
    row_type: ast.expr | None
    row_type_span: Span | None
    # None for `query_unchecked`, whose SQL is never checked
    template: ast.TemplateStr | None


# a call that resolves to `string.templatelib.Template` or `Interpolation`; the arguments are on `node`
@dataclass(slots=True, kw_only=True, frozen=True)
class TemplateCall:
    name: TemplatelibName
    node: ast.Call
    span: Span


@dataclass(slots=True, kw_only=True, frozen=True)
class IndirectUse:
    kind: IndirectKind
    # None for a star import and for `type(<t-string>)` / `<t-string>.__class__`
    name: TemplatelibName | None
    span: Span


# `t"..." + operand` (either order, and along a chain); `operand` is the side that is not a t-string chain
@dataclass(slots=True, kw_only=True, frozen=True)
class TemplateConcat:
    node: ast.BinOp
    operand: ast.expr
    span: Span
    operand_span: Span


# an interpolation of a t-string literal whose format spec is not a literal
@dataclass(slots=True, kw_only=True, frozen=True)
class DynamicFormatSpec:
    node: ast.Interpolation
    span: Span
    # set when the interpolation is in the t-string that is the sole argument of a `query` or `query_unchecked`
    # call, which reports its format spec as `dynamic-query`
    direct_argument_of: QueryKind | None


@dataclass(slots=True, kw_only=True, frozen=True)
class TemplateFacts:
    calls: tuple[TemplateCall, ...] = ()
    indirect_uses: tuple[IndirectUse, ...] = ()
    concats: tuple[TemplateConcat, ...] = ()
    dynamic_specs: tuple[DynamicFormatSpec, ...] = ()


@dataclass(slots=True, kw_only=True)
class FileResult:
    path: Path
    calls: list[QueryCall] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    unchecked_count: int = 0
    templates: TemplateFacts = field(default_factory=TemplateFacts)
    # None when the file could not be decoded or parsed
    tree: ast.Module | None = None
    # the decoded text the tree was parsed from, so offsets and snippets agree with it
    source: str | None = None
