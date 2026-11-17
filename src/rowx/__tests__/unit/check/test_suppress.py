from dataclasses import replace
from pathlib import Path

from rowx.check.model import Diagnostic, Rule, Span
from rowx.check.render import Report
from rowx.check.rules import apply_levels, resolve_levels
from rowx.check.suppress import apply_suppressions


def report(rule: Rule, start: int, end: int | None = None, end_col: int = 2) -> Report:
    return Report(
        diagnostic=Diagnostic(
            rule=rule,
            message="test",
            path=Path("sample.py"),
            span=Span(line=start, col=1, end_line=end or start, end_col=end_col),
        )
    )


def check(source: str, reports: list[Report], levels: dict[str, str] | None = None) -> list[Report]:
    selected = resolve_levels(levels or {}, [])
    return apply_levels(
        apply_suppressions(apply_levels(reports, selected), {Path("sample.py"): source}, selected), selected
    )


def test_inline_header_and_span_endpoints() -> None:
    source = "# rowx: ignore[invalid-row-type]\nx = ( # rowx: ignore[sql-error]\n  1\n) # rowx: ignore[type-mismatch]\n"
    actual = check(source, [report("invalid-row-type", 3), report("sql-error", 2, 4), report("type-mismatch", 2, 4)])
    assert actual == []
    actual = check("x = (\n  1 # rowx: ignore[sql-error]\n)\n", [report("sql-error", 1, 3)])
    assert [r.diagnostic.rule for r in actual] == ["sql-error", "unused-ignore-comment"]
    actual = check("x = (\n  1 # rowx: ignore[sql-error]\n)\n", [report("sql-error", 1, 3, 1)])
    assert actual == []


def test_strings_comments_duplicates_and_levels() -> None:
    source = 'text = "# rowx: ignore"\nx = 1 # noqa # rowx: ignore[sql-error, sql-error, nope]\n'
    actual = check(source, [report("sql-error", 2)])
    assert sorted(r.diagnostic.rule for r in actual) == ["invalid-ignore-comment", "unused-ignore-comment"]
    actual = check(source, [report("sql-error", 2)], {"sql-error": "ignore"})
    assert [r.diagnostic.rule for r in actual].count("unused-ignore-comment") == 2


def test_tokenization_failure_and_standalone_after_code() -> None:
    assert [r.diagnostic.rule for r in check("x = ( # rowx: ignore\n", [report("syntax-error", 1)])] == ["syntax-error"]
    actual = check("x = 1\n# rowx: ignore[sql-error]\ny = 2\n", [report("sql-error", 3)])
    assert sorted(r.diagnostic.rule for r in actual) == ["sql-error", "unused-ignore-comment"]


def test_invalid_cannot_broaden_and_explicit_unused_ignore() -> None:
    actual = check("x=1 # rowx: ignore[\n", [report("sql-error", 1)])
    assert sorted(r.diagnostic.rule for r in actual) == ["invalid-ignore-comment", "sql-error"]
    actual = check("# rowx: ignore[unused-ignore-comment]\nx=1 # rowx: ignore[sql-error]\n", [])
    assert actual == []


def test_one_entry_suppresses_multiple_reports_and_physical_lines() -> None:
    source = 'text = "\u2028"\rx = 1 # rowx: ignore[sql-error]\r'
    assert check(source, [report("sql-error", 2), report("sql-error", 2)]) == []


def test_operational_report_remains_error() -> None:
    actual = check("x = 1 # rowx: ignore\n", [replace(report("sql-error", 1), operational=True)], {"sql-error": "warn"})
    assert actual[0].operational
    assert actual[0].severity == "error"


def test_disabled_invalid_comment_does_not_consume_ignore() -> None:
    source = "# rowx: ignore[invalid-ignore-comment]\nx = 1 # rowx: ignore[bad-rule]\n"
    actual = check(source, [], {"invalid-ignore-comment": "ignore"})
    assert [r.diagnostic.rule for r in actual] == ["unused-ignore-comment"]


def test_adjacent_junk_is_invalid() -> None:
    actual = check("x = 1 # rowx: ignorejunk\n", [report("sql-error", 1)])
    assert sorted(r.diagnostic.rule for r in actual) == ["invalid-ignore-comment", "sql-error"]
