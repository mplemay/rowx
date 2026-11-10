from dataclasses import dataclass
from json import dumps
from typing import TYPE_CHECKING, Final, Literal, assert_never
from unicodedata import category

from rowx.check.sourcemap import LINE_PATTERN

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from rowx.check.model import Diagnostic, Span

TAB_WIDTH: Final[int] = 4
# kept: newline and tab, the joiners that real text needs (ZWNJ, ZWJ) and the soft hyphen
KEPT_CHARS: Final[frozenset[str]] = frozenset("\n\t\u200c\u200d\u00ad")
# C1 bytes decoded with surrogateescape, which a terminal could act on once re-encoded
ESCAPED_C1: Final[range] = range(0xDC80, 0xDCA0)
# Cc (controls), Zl and Zp (line and paragraph separators) and Cf (format characters: bidi overrides, isolates,
# zero-width marks, BOM); built per character, since a compiled class of all of Unicode costs over 100 ms at import
STRIPPED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Zl", "Zp"})


# `Diagnostic` carries what every rule has; the rest is only known to some rules
@dataclass(slots=True, kw_only=True, frozen=True)
class Report:
    diagnostic: Diagnostic
    help: str | None = None
    # the caret label, such as "element 3"
    label: str | None = None
    sqlstate: str | None = None
    sql: str | None = None
    column_index: int | None = None
    # the field key of a field-related rule on a named row
    field: str | None = None
    # the position of the report among those at one span, which a named row uses to keep the order the design gives
    order: int = 0


type _TemplateReason = Literal[
    "NotNullColumn",
    "NullableColumn",
    "ScanFilterNotNull",
    "NotValidatedConstraint",
    "ForeignOrView",
    "ViewColumn",
    "OuterJoin",
    "GroupingSets",
    "ScanBoundary",
    "ReturningOldNew",
    "SetOperation",
    "ComputedExpr",
    "Computed",
    "ExplainFailed",
    "OverrideBang",
    "OverrideQuestion",
]

# `{d}` is the detail, or "the column" when there is none
NULL_NOTES: Final[dict[_TemplateReason, str]] = {
    "NullableColumn": "nullable because {d} has no NOT NULL constraint",
    "NotNullColumn": "NOT NULL because {d} is declared NOT NULL",
    "ScanFilterNotNull": "NOT NULL because a scan filter rejects NULL for {d}",
    "NotValidatedConstraint": "{d} has a NOT NULL constraint that is not validated, so it may still hold NULL",
    "ForeignOrView": "comes from a foreign table or view, which may hold NULL",
    "ViewColumn": "NOT NULL because the definition of the view proves {d} is never NULL",
    "GroupingSets": "GROUPING SETS can produce NULL",
    "ReturningOldNew": "the old or new row in RETURNING is NULL for some operations",
    "SetOperation": "NOT NULL because every branch of the set operation is never NULL here",
    "ComputedExpr": "NOT NULL because the expression is a constant or a function that never returns NULL",
    "ExplainFailed": "nullability analysis failed, so the column is assumed nullable",
    "OverrideBang": 'forced NOT NULL by the "!" suffix',
    "OverrideQuestion": 'forced nullable by the "?" suffix',
}


def null_reason_note(reason: _TemplateReason, detail: str | None = None, *, column: str | None = None) -> str:
    match reason:
        case "Computed":
            return f'computed expression; add AS "{column or "col"}!"'
        case "OuterJoin":
            return "nullable side of an outer join" + ("" if detail is None else f" ({detail})")
        case "ScanBoundary":
            return (
                "a subquery contains an outer join"
                if detail is None
                else f'"{detail}" contains an outer join or GROUPING SETS'
            )
        case (
            "NullableColumn"
            | "NotNullColumn"
            | "ScanFilterNotNull"
            | "NotValidatedConstraint"
            | "ForeignOrView"
            | "ViewColumn"
            | "GroupingSets"
            | "ReturningOldNew"
            | "SetOperation"
            | "ComputedExpr"
            | "ExplainFailed"
            | "OverrideBang"
            | "OverrideQuestion"
        ):
            return NULL_NOTES[reason].format(d="the column" if detail is None else detail)
        case _ as unreachable:
            assert_never(unreachable)


def _sort_key(report: Report) -> tuple[str, int, int, int, int, int, str, str]:
    diagnostic = report.diagnostic
    span = diagnostic.span
    path = diagnostic.path.as_posix()
    return (path, span.line, span.col, span.end_line, span.end_col, report.order, diagnostic.rule, diagnostic.message)


def sort_reports(reports: Iterable[Report]) -> list[Report]:
    return sorted(reports, key=_sort_key)


# only CR, LF and CRLF end a line, as in Python's tokenizer; `str.splitlines` also splits on U+2028 and friends
def _source_lines(source: str) -> list[str]:
    return [line.rstrip("\r\n") for line in LINE_PATTERN.findall(source)]


def _is_stripped(char: str) -> bool:
    if char in KEPT_CHARS:
        return False
    return ord(char) in ESCAPED_C1 or category(char) in STRIPPED_CATEGORIES


def strip_controls(text: str) -> str:
    return "".join(char for char in text if not _is_stripped(char))


def _expand(text: str) -> str:
    return text.replace("\t", " " * TAB_WIDTH)


def _carets(line: str, span: Span, number: int) -> tuple[int, int]:
    # the 0-based start offset and the width of the underline on source line `number`
    stripped = line.rstrip("\r\n")
    start = span.col - 1 if number == span.line else len(stripped) - len(stripped.lstrip())
    stop = span.end_col - 1 if number == span.end_line else len(stripped)
    start = min(start, len(stripped))
    stop = max(min(stop, len(stripped)), start)
    prefix = len(_expand(stripped[:start]))
    width = len(_expand(stripped[start:stop]))
    # a zero-width span (an inexact segment, or end of input) still gets one caret
    return prefix, max(width, 1)


def _snippet(report: Report, source: str, gutter: int) -> list[str]:
    span = report.diagnostic.span
    lines = _source_lines(source)
    pad = " " * gutter
    rows: list[str] = []
    last = min(span.end_line, len(lines))
    for number in range(span.line, last + 1):
        line = lines[number - 1]
        prefix, width = _carets(line, span, number)
        rows.append(f"{number:>{gutter}} | {_expand(line)}".rstrip())
        underline = f"{pad} | {' ' * prefix}{'^' * width}"
        if number == last and report.label is not None:
            underline += f" {report.label}"
        rows.append(underline)
    return rows


def _relative(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def _block(report: Report, source: str | None, width: int, root: Path | None) -> list[str]:
    diagnostic = report.diagnostic
    span = diagnostic.span
    pad = " " * width
    out = [
        f"error[{diagnostic.rule}]: {strip_controls(diagnostic.message)}",
        f"{pad}--> {_relative(diagnostic.path, root)}:{span.line}:{span.col}",
    ]
    if source is not None and 1 <= span.line <= len(_source_lines(source)):
        out.append(f"{pad} |")
        out.extend(_snippet(report, source, width))
    trailer = [f"= note: {strip_controls(note)}" for note in diagnostic.notes]
    if report.help is not None:
        trailer.append(f"= help: {strip_controls(report.help)}")
    if trailer:
        out.append(f"{pad} |")
        out.extend(f"{pad} {line}" for line in trailer)
    return out


def _gutter(report: Report, source: str | None) -> int:
    span = report.diagnostic.span
    shown = min(span.end_line, len(_source_lines(source))) if source is not None else span.line
    return len(str(max(span.line, shown)))


def render_text(reports: Iterable[Report], sources: Mapping[Path, str], root: Path | None = None) -> str:
    blocks: list[str] = []
    for report in sort_reports(reports):
        source = sources.get(report.diagnostic.path)
        lines = _block(report, source, _gutter(report, source), root)
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def render_json(reports: Iterable[Report], root: Path | None = None) -> str:
    items = []
    for report in sort_reports(reports):
        diagnostic = report.diagnostic
        span = diagnostic.span
        items.append(
            {
                "rule": diagnostic.rule,
                "message": diagnostic.message,
                "file": _relative(diagnostic.path, root),
                "line": span.line,
                "col": span.col,
                "end_line": span.end_line,
                "end_col": span.end_col,
                "notes": list(diagnostic.notes),
                "help": report.help,
                "sqlstate": report.sqlstate,
                "sql": report.sql,
                "column_index": report.column_index,
                "field": report.field,
            },
        )
    return dumps(items, indent=2, ensure_ascii=False) + "\n"
