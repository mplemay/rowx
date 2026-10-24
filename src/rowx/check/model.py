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
    "unsupported-type",
    "type-mismatch",
    "nullability-mismatch",
    "invalid-override",
]
type QueryKind = Literal["query", "query_unchecked"]

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


@dataclass(slots=True, kw_only=True)
class FileResult:
    path: Path
    calls: list[QueryCall] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    unchecked_count: int = 0
    # None when the file could not be decoded or parsed
    tree: ast.Module | None = None
    # the decoded text the tree was parsed from, so offsets and snippets agree with it
    source: str | None = None
