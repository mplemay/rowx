import json
import os
from pathlib import Path
from textwrap import dedent
from typing import Final, get_args

import pytest

from rowx._vocab import NullReasonName
from rowx.check.discover import discover_source
from rowx.check.model import Diagnostic, Span
from rowx.check.render import NULL_NOTES, Report, null_reason_note, render_json, render_text
from rowx.check.sourcemap import SourceMapper

EXPECTED: Final[Path] = Path(__file__).parent / "expected"
PATH: Final[Path] = Path("app/users.py")

ELEMENT_SOURCE: Final[str] = dedent(
    """\
    async def load(conn):
        user = await query[tuple[int, str, str]](
            t"select id, name, email from users"
        ).fetch_one(conn)
    """,
)

MULTI_LINE_SOURCE: Final[str] = (
    'from rowx import query\nrows = query[tuple[int]](t"""\n    select id\n    from users\n    wher id = 1\n""")\n'
)


def golden(name: str, actual: str) -> None:
    path = EXPECTED / name
    if "ROWX_UPDATE_GOLDENS" in os.environ:
        path.write_text(actual)
    assert actual == path.read_text()


def sql_error(source: str, position: int, message: str = "syntax error", *, help_: str | None = None) -> Report:
    result = discover_source(PATH, source.encode())
    template = result.calls[0].template
    assert template is not None
    span = SourceMapper(source).build(template).locate(position)
    diagnostic = Diagnostic(rule="sql-error", message=message, path=PATH, span=span)
    return Report(diagnostic=diagnostic, help=help_, sqlstate="42601", sql="select")


def element_report() -> Report:
    note = null_reason_note("NullableColumn", "users.email")
    assert note is not None
    diagnostic = Diagnostic(
        rule="nullability-mismatch",
        message='column 3 "email" may be NULL but tuple element 3 is `str`',
        path=PATH,
        span=Span(line=2, col=43, end_line=2, end_col=46),
        notes=(note,),
    )
    return Report(
        diagnostic=diagnostic,
        help='annotate `str | None`, or alias the column as "email!" if it can\'t be NULL',
        label="element 3",
        column_index=2,
    )


def test_element_caret() -> None:
    golden("element.txt", render_text([element_report()], {PATH: ELEMENT_SOURCE}))


def test_alias_note() -> None:
    source = 'Row = tuple[int, str]\n\nrows = query[Row](t"select id, name from users")\n'
    diagnostic = Diagnostic(
        rule="arity-mismatch",
        message="row type has 2 elements but the query returns 3 columns",
        path=PATH,
        span=Span(line=3, col=15, end_line=3, end_col=18),
        notes=("alias `Row` defined at app/users.py:1:1",),
    )
    golden("alias.txt", render_text([Report(diagnostic=diagnostic)], {PATH: source}))


def test_sql_error_single_line() -> None:
    source = 'from rowx import query\nrows = query[tuple[int]](t"select frm users")\n'
    report = sql_error(source, position=12, message='syntax error at or near "users"')
    golden("sql_error_single.txt", render_text([report], {PATH: source}))


def test_sql_error_multi_line() -> None:
    source = (
        'from rowx import query\nrows = query[tuple[int]](t"""\n    select id\n    from users\n    wher id = 1\n""")\n'
    )
    report = sql_error(source, position=40, message='syntax error at or near "id"')
    golden("sql_error_multi.txt", render_text([report], {PATH: source}))


def test_sql_error_across_lines() -> None:
    source = (
        'from rowx import query\nrows = query[tuple[int]](t"""\n    select id\n    from users\n    wher id = 1\n""")\n'
    )
    result = discover_source(PATH, source.encode())
    template = result.calls[0].template
    assert template is not None
    sql_map = SourceMapper(source).build(template)
    span = Span(
        line=sql_map.locate(6).line,
        col=sql_map.locate(6).col,
        end_line=sql_map.locate(29).line,
        end_col=sql_map.locate(29).end_col,
    )
    diagnostic = Diagnostic(rule="sql-error", message="spans lines", path=PATH, span=span)
    golden("sql_error_span.txt", render_text([Report(diagnostic=diagnostic, sqlstate="42601")], {PATH: source}))


def test_sql_error_after_placeholder() -> None:
    source = 'from rowx import query\nrows = query[tuple[int]](t"select {x}y from users")\n'
    report = sql_error(source, position=11, message='syntax error at or near "y"')
    golden("sql_error_after_placeholder.txt", render_text([report], {PATH: source}))


def test_sql_error_at_end_of_input() -> None:
    source = 'from rowx import query\nrows = query[tuple[int]](t"select id from")\n'
    report = sql_error(
        source,
        position=15,
        message="syntax error at end of input",
        help_="cast the parameter: `{x}::int`",
    )
    golden("sql_error_end.txt", render_text([report], {PATH: source}))


def test_inexact_fallback() -> None:
    source = 'rows = query[tuple[int]](t"select \\q from users")\n'
    diagnostic = Diagnostic(
        rule="sql-error",
        message='syntax error at or near "from"',
        path=PATH,
        span=Span(line=1, col=28, end_line=1, end_col=28),
    )
    golden("inexact.txt", render_text([Report(diagnostic=diagnostic)], {PATH: source}))


def test_json_clean_run() -> None:
    golden("clean.json.txt", render_json([]))
    assert json.loads(render_json([])) == []


def test_json_two_diagnostics() -> None:
    other = Diagnostic(
        rule="sql-error",
        message="boom",
        path=Path("/project/app/a.py"),
        span=Span(line=1, col=1, end_line=1, end_col=5),
    )
    reports = [
        element_report(),
        Report(diagnostic=other, sqlstate="42P01", sql="select 1", help="try again"),
    ]
    reports[0] = Report(
        diagnostic=Diagnostic(
            rule=reports[0].diagnostic.rule,
            message=reports[0].diagnostic.message,
            path=Path("/project/app/users.py"),
            span=reports[0].diagnostic.span,
            notes=reports[0].diagnostic.notes,
        ),
        help=reports[0].help,
        column_index=2,
    )
    actual = render_json(reports, Path("/project"))
    golden("two.json.txt", actual)
    items = json.loads(actual)
    assert [i["file"] for i in items] == ["app/a.py", "app/users.py"]


def test_sorted_and_separated() -> None:
    late = Diagnostic(rule="sql-error", message="b", path=PATH, span=Span(line=9, col=1, end_line=9, end_col=2))
    early = Diagnostic(rule="sql-error", message="a", path=PATH, span=Span(line=2, col=1, end_line=2, end_col=2))
    text = render_text([Report(diagnostic=late), Report(diagnostic=early)], {})
    assert text.index(": a") < text.index(": b")
    assert "\n\nerror[" in text


def test_missing_source_omits_snippet() -> None:
    text = render_text([element_report()], {})
    assert "^" not in text
    assert "--> app/users.py:2:43" in text


def test_crlf_and_tabs() -> None:
    source = "x = 1\r\n\tfoo bar\r\n"
    diagnostic = Diagnostic(rule="sql-error", message="m", path=PATH, span=Span(line=2, col=2, end_line=2, end_col=5))
    text = render_text([Report(diagnostic=diagnostic)], {PATH: source})
    assert "\r" not in text
    assert "2 |     foo bar\n  |     ^^^\n" in text


@pytest.mark.parametrize(
    ("reason", "detail", "expected"),
    [
        ("NullableColumn", "users.email", "nullable because users.email has no NOT NULL constraint"),
        ("OuterJoin", "Hash Left Join", "nullable side of an outer join (Hash Left Join)"),
        ("Computed", None, 'computed expression; add AS "col!"'),
    ],
)
def test_null_reason_note(reason: NullReasonName, detail: str | None, expected: str) -> None:
    assert null_reason_note(reason, detail) == expected


def test_null_reasons_match_the_note_table() -> None:
    noted = set(NULL_NOTES) | {"Computed", "OuterJoin", "ScanBoundary"}
    names = set(get_args(NullReasonName.__value__))
    assert noted == names
    assert all(null_reason_note(reason) for reason in names)


def test_unicode_line_separator_is_not_a_line_break() -> None:
    source = 's = "a\u2028b"\nrows = 1\n'
    diagnostic = Diagnostic(rule="sql-error", message="m", path=PATH, span=Span(line=2, col=1, end_line=2, end_col=5))
    text = render_text([Report(diagnostic=diagnostic)], {PATH: source})
    assert "2 | rows = 1\n  | ^^^^\n" in text
