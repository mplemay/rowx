import re
from pathlib import Path
from typing import Final

import pytest

from rowx.__tests__.integration.describe.test_base import run_describe

pytestmark = [pytest.mark.integration, pytest.mark.db]

JOIN: Final[str] = "FROM users u LEFT JOIN posts p ON p.user_id = u.id"
DOC: Final[Path] = Path(__file__).resolve().parents[5] / "docs" / "known-limits.md"
MARKER: Final[re.Pattern[str]] = re.compile(r"^\s*# (?:limit|correct):", re.MULTILINE)


def verdicts(dsn: str, sql: str) -> list[tuple[bool, str]]:
    return [(c.nullable, c.reason) for c in run_describe(dsn, sql).columns]


# correct: a LATERAL subquery on the null-extended side is promoted through the join walk
def test_lateral_on_outer_join(schema: str) -> None:
    sql = (
        "SELECT l.title FROM users u "
        "LEFT JOIN LATERAL (SELECT p.title FROM posts p WHERE p.user_id = u.id LIMIT 1) l ON true"
    )
    assert verdicts(schema, sql) == [(True, "OuterJoin")]


# correct: a window function over an outer join hides the join behind a subquery scan, so the boundary rule promotes it
def test_window_over_outer_join(schema: str) -> None:
    sql = f"SELECT x.t FROM (SELECT p.title AS t, row_number() OVER () {JOIN}) x"  # noqa: S608
    assert verdicts(schema, sql) == [(True, "ScanBoundary")]


# correct: a nested CTE chain is promoted at the outermost scan boundary
def test_nested_cte_chain(schema: str) -> None:
    sql = (
        f"WITH a AS MATERIALIZED (SELECT p.title {JOIN}), "  # noqa: S608
        "b AS MATERIALIZED (SELECT title FROM a) SELECT title FROM b"
    )
    assert verdicts(schema, sql) == [(True, "ScanBoundary")]


# limit: the scan-boundary rule promotes every column of the alias, including ones from the non-null side
def test_scan_boundary_over_promotes(schema: str) -> None:
    sql = f"SELECT x.id, x.title FROM (SELECT u.id, p.title, row_number() OVER () {JOIN}) x"  # noqa: S608
    assert verdicts(schema, sql) == [(True, "ScanBoundary"), (True, "ScanBoundary")]


# limit: view columns are always nullable, because attnotnull is false for views
def test_view_columns_are_nullable(schema: str) -> None:
    assert verdicts(schema, "SELECT user_id, name FROM user_posts") == [
        (True, "ForeignOrView"),
        (True, "ForeignOrView"),
    ]


# limit: set operations have no table origin, so they are Unknown and default to nullable
def test_set_operation_is_nullable(schema: str) -> None:
    assert verdicts(schema, "SELECT name FROM users UNION SELECT title FROM posts") == [(True, "Computed")]


# limit: a recursive CTE has no table origin, so it is Unknown and defaults to nullable
def test_recursive_cte_is_nullable(schema: str) -> None:
    sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r"
    assert verdicts(schema, sql) == [(True, "Computed")]


# limit: a computed column is always nullable, even when its inputs are NOT NULL
def test_computed_column_is_nullable(schema: str) -> None:
    assert verdicts(schema, "SELECT upper(name), id + 1 FROM users") == [(True, "Computed")] * 2


# correct: the bang override narrows a computed column
def test_bang_narrows_computed_column(schema: str) -> None:
    assert verdicts(schema, 'SELECT upper(name) AS "n!" FROM users') == [(False, "OverrideBang")]


def test_marker_count_matches_doc() -> None:
    markers = len(MARKER.findall(Path(__file__).read_text()))
    rows = [line for line in DOC.read_text().splitlines() if line.startswith("| ")]
    # the first two table lines are the header and its separator
    assert markers == len(rows) - 1
    assert markers == 9
