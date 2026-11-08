from re import DOTALL, VERBOSE, compile as re_compile
from typing import TYPE_CHECKING, Final

from rowx_core.backend import OverrideIssue

from rowx_postgres._compiler import IDENT_MAX_BYTES

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from rowx_core.backend import ColumnInfo

SQL_ERROR_HELP: Final[dict[str, str]] = {
    "42P18": "cast the parameter: `{x}::int`",
    "42P08": "cast the parameter: `{x}::int`",
    "42501": "grant the checker's role the privileges this query needs, or run the check as a role that has them",
}
SQL_TOKEN: Final = re_compile(
    r"""
    (?P<space>\s+)
    |(?P<line_comment>--[^\n]*)
    |(?P<block_open>/\*)
    |(?P<string>[eE]?'(?:[^'\\]|\\.|'')*')
    |(?P<dollar>\$(?:[^\W\d]\w*)?\$)
    |(?P<quoted>"(?:[^"]|"")*")
    |(?P<word>[^\W\d]\w*)
    |(?P<other>.)
    """,
    flags=VERBOSE | DOTALL,
)


def _skip_block_comment(sql: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(sql):
        if sql.startswith("/*", index):
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return len(sql)


# Quoted aliases only: comments, string literals and dollar quotes cannot supply an override.
def quoted_aliases(sql: str) -> Iterator[tuple[int, str]]:
    index = 0
    previous = ""
    while (match := SQL_TOKEN.match(sql, index)) is not None:
        kind = match.lastgroup
        index = match.end()
        if kind == "block_open":
            index = _skip_block_comment(sql, match.start())
        elif kind == "dollar":
            index = len(sql) if (close := sql.find(match[0], index)) == -1 else close + len(match[0])
        elif kind == "quoted" and previous == "as":
            yield match.start(), match[0][1:-1].replace('""', '"')
        if kind not in {"space", "line_comment", "block_open"}:
            previous = match[0].lower() if kind == "word" else ""


def _truncated_alias(sql: str, column: ColumnInfo) -> int | None:
    # Truncation at a UTF-8 boundary can leave up to three bytes of room.
    if not IDENT_MAX_BYTES - 4 < len(column.raw_name.encode()) <= IDENT_MAX_BYTES:
        return None
    for offset, ident in quoted_aliases(sql):
        if ident.endswith(("!", "?")) and len(ident.encode()) > IDENT_MAX_BYTES and ident.startswith(column.raw_name):
            return offset
    return None


def override_issues(sql: str, columns: Sequence[ColumnInfo]) -> tuple[OverrideIssue, ...]:
    issues: list[OverrideIssue] = []
    for index, column in enumerate(columns):
        if (offset := _truncated_alias(sql, column)) is None:
            continue
        message = (
            f'column {index + 1} "{column.raw_name}" was truncated at {IDENT_MAX_BYTES} bytes, '
            'so its "!" or "?" override is lost'
        )
        help_ = f"shorten the alias so the name and its suffix fit in {IDENT_MAX_BYTES} bytes"
        issues.append(OverrideIssue(message=message, offset=offset, help=help_))
    return tuple(issues)
