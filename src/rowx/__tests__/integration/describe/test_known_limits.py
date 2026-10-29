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
    sql = f"SELECT x.t FROM (SELECT p.title AS t, row_number() OVER () {JOIN}) x"  # ruff: ignore[hardcoded-sql-expression]
    assert verdicts(schema, sql) == [(True, "ScanBoundary")]


# correct: a nested CTE chain is promoted at the outermost scan boundary
def test_nested_cte_chain(schema: str) -> None:
    sql = (
        f"WITH a AS MATERIALIZED (SELECT p.title {JOIN}), "  # ruff: ignore[hardcoded-sql-expression]
        "b AS MATERIALIZED (SELECT title FROM a) SELECT title FROM b"
    )
    assert verdicts(schema, sql) == [(True, "ScanBoundary")]


# correct: a scan-boundary column from the non-null side keeps its catalog verdict (it was the limit #105 fixed)
def test_scan_boundary_over_promotes(schema: str) -> None:
    sql = f"SELECT x.id, x.title FROM (SELECT u.id, p.title, row_number() OVER () {JOIN}) x"  # ruff: ignore[hardcoded-sql-expression]
    assert verdicts(schema, sql) == [(False, "NotNullColumn"), (True, "ScanBoundary")]


# correct: view columns from the non-null side of the definition are NOT NULL, and the nullable side of its join stays nullable (`OuterJoin`)
def test_view_columns_are_nullable(schema: str) -> None:
    assert verdicts(schema, "SELECT user_id, title FROM user_posts") == [
        (False, "ViewColumn"),
        (True, "OuterJoin"),
    ]


# correct: a set operation column is NOT NULL when the plan proves it at that output position (`SetOperation`)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM users UNION SELECT title FROM posts",
        "SELECT name FROM users UNION ALL SELECT title FROM posts ORDER BY 1",
        "SELECT name FROM users EXCEPT SELECT email FROM users",
        "SELECT email FROM users INTERSECT SELECT title FROM posts",
        "(SELECT name FROM users UNION ALL SELECT title FROM posts) INTERSECT SELECT name FROM users",
        "SELECT name FROM users EXCEPT (SELECT title FROM posts UNION SELECT email FROM users)",
    ],
)
def test_set_operation_not_null_is_inferred(schema: str, sql: str) -> None:
    assert verdicts(schema, sql) == [(False, "SetOperation")]


# correct: a set operation column stays nullable when any position is unproven, by position and not by name
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM users UNION SELECT email FROM users",
        "SELECT email FROM users EXCEPT SELECT name FROM users",
        "SELECT email FROM users INTERSECT SELECT email FROM users",
        "SELECT name FROM users UNION ALL SELECT NULL::text",
        "SELECT name FROM users UNION ALL SELECT upper(title) FROM posts",
        "SELECT x FROM not_valid UNION SELECT id FROM users",
        "SELECT name, email FROM users UNION SELECT email, name FROM users",
        "SELECT u.id FROM users u FULL JOIN posts p ON p.id = u.id UNION SELECT id FROM users",
    ],
)
def test_set_operation_stays_nullable_when_unproven(schema: str, sql: str) -> None:
    assert all(nullable for nullable, _ in verdicts(schema, sql))


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
    markers = len(MARKER.findall(Path(__file__).read_text(encoding="utf-8")))
    rows = [line for line in DOC.read_text(encoding="utf-8").splitlines() if line.startswith("| ")]
    # the first two table lines are the header and its separator
    assert markers == len(rows) - 1
    assert markers == 10
