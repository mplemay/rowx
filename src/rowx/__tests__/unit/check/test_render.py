from json import loads
from pathlib import Path

from rowx.check.model import Diagnostic, Span
from rowx.check.render import Report, render_json


def _report(*, code: str | None) -> Report:
    diagnostic = Diagnostic(
        rule="sql-error",
        message="boom",
        path=Path("app/query.py"),
        span=Span(line=1, col=1, end_line=1, end_col=2),
    )
    return Report(diagnostic=diagnostic, sqlstate="23505", code=code)


def test_json_includes_code_after_sqlstate_when_set() -> None:
    [item] = loads(render_json([_report(code="SQLITE_CONSTRAINT_UNIQUE")]))
    assert item["code"] == "SQLITE_CONSTRAINT_UNIQUE"
    keys = list(item)
    assert keys.index("code") == keys.index("sqlstate") + 1


def test_json_omits_code_when_none() -> None:
    [item] = loads(render_json([_report(code=None)]))
    assert "code" not in item
