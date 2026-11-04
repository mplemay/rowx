from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    import ast
    from pathlib import Path

type Rule = Literal[
    "syntax-error",
    "indirect-query",
    "dynamic-query",
    "template-construction",
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
# "value": the class used as a value; "attribute": an attribute access other than a direct call (including any dunder
# on a t-string and `.__class__` of an evident `Template`); "private": a `string.templatelib` name other than `Template`,
# `Interpolation` and `convert`, imported or accessed but not called; "star-import": `from string.templatelib import *`;
# "type-call": `type(<template>)` or `<template>.__class__` used as a callable, where the receiver is evidently a
# `Template` (a t-string, a `Template(...)` call, or a name with one binding that is a t-string or annotated
# `Template`); "module-value": `string` or `string.templatelib` used other than as the base of an attribute;
# "alias": `type T = Template` or `X: TypeAlias = Template`; "re-export": a name listed in `__all__`; "ambiguous": a
# name that is also bound another way in its scope. Names inside a subscript or the type argument of `cast` are
# type-level and not recorded
type IndirectKind = Literal[
    "value",
    "attribute",
    "private",
    "star-import",
    "type-call",
    "module-value",
    "alias",
    "re-export",
    "ambiguous",
]

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


# a call that resolves to `string.templatelib.Template` or `Interpolation`; the arguments are on `node`. `private` is
# set for any other name in the module (`name` is then the attribute or the imported local name)
@dataclass(slots=True, kw_only=True, frozen=True)
class TemplateCall:
    name: str
    node: ast.Call
    span: Span
    private: bool = False


@dataclass(slots=True, kw_only=True, frozen=True)
class IndirectUse:
    kind: IndirectKind
    # None for a star import and for `type-call`; the name in `__all__` for a re-export
    name: str | None
    span: Span


# `t"..." + operand` (either order, and along a chain); `operand` is the side that is not a t-string chain
@dataclass(slots=True, kw_only=True, frozen=True)
class TemplateConcat:
    node: ast.BinOp
    operand: ast.expr
    span: Span
    operand_span: Span
    # the operand is a t-string chain, or evidently a `Template` (a `Template(...)` call, or a name declared as one)
    evident: bool = False


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
