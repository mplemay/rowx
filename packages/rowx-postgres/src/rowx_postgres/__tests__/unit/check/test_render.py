import json
import os
import sys
import unicodedata
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Final, get_args

import pytest
from rowx_postgres._backend import load
from rowx_postgres._compiler import COMPILER
from rowx_postgres._vocab import NullReasonName

from rowx.check.discover import discover_file, discover_source
from rowx.check.model import Diagnostic, Span
from rowx.check.render import NULL_NOTES, Report, null_reason_note, render_json, render_text, strip_controls
from rowx.check.resolve import ModuleGraph, resolve_row_type
from rowx.check.sourcemap import SourceMapper

if TYPE_CHECKING:
    from rowx_core.backend import CheckerBackend

PG_RULES: Final[CheckerBackend] = load().checker()

EXPECTED: Final[Path] = Path(__file__).parent / "expected"
PATH: Final[Path] = Path("app/users.py")

ELEMENT_SOURCE: Final[str] = dedent(
    """\
    async def load(conn):
        user = await query[tuple[int, str, str]](
            t"select id, name, email from users"
        ).one(conn)
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
    span = SourceMapper(source, COMPILER).build(template).locate(position)
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
    sql_map = SourceMapper(source, COMPILER).build(template)
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
        ("ScanFilterNotNull", "users.email", "NOT NULL because a scan filter rejects NULL for users.email"),
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


def test_json_carries_the_field_and_order_keeps_a_named_rows_findings_in_sequence() -> None:
    span = Span(line=4, col=7, end_line=4, end_col=11)
    first = Diagnostic(rule="unexpected-column", message="b", path=PATH, span=span)
    second = Diagnostic(rule="duplicate-column", message="a", path=PATH, span=span)
    reports = [
        Report(diagnostic=second, order=1, column_index=1),
        Report(diagnostic=first, order=0, column_index=0, field="id"),
    ]
    items = json.loads(render_json(reports))
    assert [(item["rule"], item["field"], item["column_index"]) for item in items] == [
        ("unexpected-column", "id", 0),
        ("duplicate-column", None, 1),
    ]


RESOLVER_SOURCE: Final[str] = """\
import abc
from dataclasses import dataclass
from thirdparty import Model
from typing import TYPE_CHECKING, TypedDict

from rowx import query

if TYPE_CHECKING:
    import datetime


def deco(cls):
    return cls


class Meta(type): ...


class Base:
    def __init_subclass__(cls, flag=False, **kwargs): ...


@dataclass
class Dated:
    when: datetime.date


type When = datetime.date


class Aliased(TypedDict):
    when: When


@deco
@dataclass
class Decorated:
    id: int


@dataclass
class Custom:
    id: int

    def __init__(self, id: int) -> None:
        self.id = id


@dataclass
class Meta2(metaclass=Meta):
    id: int


@dataclass
class Keyword(Base, flag=True):
    id: int


@dataclass
class Abstract(abc.ABC):
    id: int


@dataclass
class Conditional:
    id: int
    if True:
        extra: int


query[Dated](t"select 1")
query[Aliased](t"select 1")
query[Model](t"select 1")
query[Decorated](t"select 1")
query[Custom](t"select 1")
query[Meta2](t"select 1")
query[Keyword](t"select 1")
query[Abstract](t"select 1")
query[Conditional](t"select 1")
"""


def resolver_reports(tmp_path: Path) -> tuple[list[Report], dict[Path, str]]:
    path = tmp_path / "queries.py"
    path.write_text(RESOLVER_SOURCE)
    graph = ModuleGraph()
    reports: list[Report] = []
    for call in discover_file(path).calls:
        resolution = resolve_row_type(graph, call, (tmp_path,), backend=PG_RULES)
        assert resolution.diagnostic is not None
        reports.append(Report(diagnostic=resolution.diagnostic, help=resolution.help))
    return reports, {path: RESOLVER_SOURCE}


def relative(text: str, tmp_path: Path) -> str:
    return text.replace(str(tmp_path.resolve()), "<root>").replace(str(tmp_path), "<root>")


def test_named_row_resolver_text(tmp_path: Path) -> None:
    reports, sources = resolver_reports(tmp_path)
    golden("named_rows_resolver.txt", relative(render_text(reports, sources, tmp_path), tmp_path))


def test_named_row_resolver_json(tmp_path: Path) -> None:
    reports, _ = resolver_reports(tmp_path)
    golden("named_rows_resolver.json.txt", relative(render_json(reports, tmp_path), tmp_path))


def test_text_output_strips_control_characters_from_server_text() -> None:
    escape = "\x1b[2J\x1b]0;owned\x07"
    c1 = "\x9b31m"
    diagnostic = Diagnostic(
        rule="sql-error",
        message=f'relation {escape}"x" does not exist\n\tdetail{c1}',
        path=PATH,
        span=Span(line=1, col=1, end_line=1, end_col=2),
        notes=(f"note{escape}",),
    )
    out = render_text([Report(diagnostic=diagnostic, help=f"help{escape}{c1}")], {})
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "\x9b" not in out
    assert 'relation [2J]0;owned"x" does not exist\n\tdetail31m' in out
    assert "= note: note[2J]0;owned" in out
    assert "= help: help[2J]0;owned31m" in out


def test_json_output_escapes_control_characters() -> None:
    diagnostic = Diagnostic(
        rule="sql-error",
        message="bad \x1b[2J",
        path=PATH,
        span=Span(line=1, col=1, end_line=1, end_col=2),
    )
    out = render_json([Report(diagnostic=diagnostic)])
    assert "\x1b" not in out
    assert "\\u001b" in out


def test_strip_controls_removes_format_and_separator_characters() -> None:
    hostile = "a\x1b[31m\u202eb\u200bc\u2028d\u2029e\ufeff\u2066f\u2069\nkeep\t"
    assert strip_controls(hostile) == "a[31mbcdef\nkeep\t"
    assert strip_controls("p\u00e4 \u4e2d") == "p\u00e4 \u4e2d"


def test_strip_controls_keeps_joiners_and_soft_hyphen() -> None:
    kept = "a\u200cb\u200dc\u00add\ne\tf"
    assert strip_controls(kept) == kept


def test_strip_controls_covers_every_other_control_format_and_separator() -> None:
    kept = {0x0A, 0x09, 0x200C, 0x200D, 0x00AD}
    for code in range(sys.maxunicode + 1):
        if code not in kept and unicodedata.category(chr(code)) in {"Cc", "Cf", "Zl", "Zp"}:
            assert not strip_controls(chr(code)), hex(code)


def test_strip_controls_removes_escaped_c1_bytes() -> None:
    assert strip_controls("a\udc85b") == "ab"
    assert strip_controls("a\udc7fb\udca0") == "a\udc7fb\udca0"


def test_text_output_strips_bidi_overrides() -> None:
    diagnostic = Diagnostic(
        rule="sql-error",
        message="bad \u202etxet",
        path=PATH,
        span=Span(line=1, col=1, end_line=1, end_col=2),
    )
    assert "\u202e" not in render_text([Report(diagnostic=diagnostic)], {})
