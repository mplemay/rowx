from dataclasses import dataclass
from json import dumps
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from rowx.check.model import Diagnostic, Span

TAB_WIDTH: Final[int] = 4


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


# `{d}` is the detail, or "the column" when there is none
NULL_NOTES: Final[dict[str, str]] = {
    "NullableColumn": "nullable because {d} has no NOT NULL constraint",
    "NotNullColumn": "NOT NULL because {d} is declared NOT NULL",
    "NotValidatedConstraint": "{d} has a NOT NULL constraint that is not validated, so it may still hold NULL",
    "ForeignOrView": "comes from a foreign table or view, which may hold NULL",
    "GroupingSets": "GROUPING SETS can produce NULL",
    "ReturningOldNew": "the old or new row in RETURNING is NULL for some operations",
    "ExplainFailed": "nullability analysis failed, so the column is assumed nullable",
    "OverrideBang": 'forced NOT NULL by the "!" suffix',
    "OverrideQuestion": 'forced nullable by the "?" suffix',
}


def null_reason_note(reason: str, detail: str | None = None, *, column: str | None = None) -> str | None:
    if (template := NULL_NOTES.get(reason)) is not None:
        return template.format(d="the column" if detail is None else detail)
    if reason == "Computed":
        return f'computed expression; add AS "{column or "col"}!"'
    if reason == "OuterJoin":
        return "nullable side of an outer join" + ("" if detail is None else f" ({detail})")
    if reason == "ScanBoundary":
        return (
            "a subquery contains an outer join"
            if detail is None
            else f'"{detail}" contains an outer join or GROUPING SETS'
        )
    return None


def _sort_key(report: Report) -> tuple[str, int, int, int, int, str, str]:
    diagnostic = report.diagnostic
    span = diagnostic.span
    path = diagnostic.path.as_posix()
    return (path, span.line, span.col, span.end_line, span.end_col, diagnostic.rule, diagnostic.message)


def sort_reports(reports: Iterable[Report]) -> list[Report]:
    return sorted(reports, key=_sort_key)


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
    lines = source.splitlines()
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


def _block(report: Report, source: str | None, width: int) -> list[str]:
    diagnostic = report.diagnostic
    span = diagnostic.span
    pad = " " * width
    out = [
        f"error[{diagnostic.rule}]: {diagnostic.message}",
        f"{pad}--> {diagnostic.path.as_posix()}:{span.line}:{span.col}",
    ]
    if source is not None and 1 <= span.line <= len(source.splitlines()):
        out.append(f"{pad} |")
        out.extend(_snippet(report, source, width))
    trailer = [f"= note: {note}" for note in diagnostic.notes]
    if report.help is not None:
        trailer.append(f"= help: {report.help}")
    if trailer:
        out.append(f"{pad} |")
        out.extend(f"{pad} {line}" for line in trailer)
    return out


def _gutter(report: Report, source: str | None) -> int:
    span = report.diagnostic.span
    shown = min(span.end_line, len(source.splitlines())) if source is not None else span.line
    return len(str(max(span.line, shown)))


def render_text(reports: Iterable[Report], sources: Mapping[Path, str]) -> str:
    blocks: list[str] = []
    for report in sort_reports(reports):
        source = sources.get(report.diagnostic.path)
        lines = _block(report, source, _gutter(report, source))
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def _relative(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


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
            },
        )
    return dumps(items, indent=2, ensure_ascii=False) + "\n"
