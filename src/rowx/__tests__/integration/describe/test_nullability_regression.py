import pytest

from rowx.__tests__.integration.describe.sqlx_cases import SQLX_CASES
from rowx.__tests__.integration.describe.test_base import run_describe

pytestmark = [pytest.mark.integration, pytest.mark.db]


def verdicts(dsn: str, sql: str) -> list[tuple[bool, str]]:
    return [(c.nullable, c.reason) for c in run_describe(dsn, sql).columns]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = $1", [(True, "OuterJoin")]),
        (
            "SELECT p.title FROM posts p RIGHT JOIN users u ON p.user_id = u.id",
            [(True, "OuterJoin")],
        ),
        ("SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id ORDER BY p.title", [(True, "OuterJoin")]),
        (
            "SELECT x.title FROM (SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id) x",
            [(True, "OuterJoin")],
        ),
        (
            (
                "SELECT x.title FROM (SELECT p.title, row_number() OVER () "
                "FROM users u LEFT JOIN posts p ON p.user_id = u.id) x"
            ),
            [(True, "ScanBoundary")],
        ),
        (
            (
                "WITH c AS MATERIALIZED (SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id) "
                "SELECT title FROM c"
            ),
            [(True, "ScanBoundary")],
        ),
        ("SELECT name FROM users GROUP BY ROLLUP(name)", [(True, "GroupingSets")]),
        (
            "INSERT INTO users (name) VALUES ($1) RETURNING old.name",
            [(True, "ReturningOldNew")],
        ),
        ("SELECT p.title, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id", [(True, "OuterJoin")] * 2),
        ("SELECT x FROM not_valid", [(True, "NotValidatedConstraint")]),
        (
            "SELECT u.name, p.title FROM users u FULL JOIN posts p ON p.user_id = u.id",
            [(True, "OuterJoin")] * 2,
        ),
        (
            "SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE p.title IS NOT NULL",
            [(False, "NotNullColumn")],
        ),
        ("SELECT upper(name) FROM users", [(True, "Computed")]),
        ("SELECT count(*) FROM users", [(True, "Computed")]),
        ("SELECT coalesce(email, 'x') FROM users", [(True, "Computed")]),
        ("SELECT 1", [(True, "Computed")]),
    ],
)
def test_design_table(schema: str, sql: str, expected: list[tuple[bool, str]]) -> None:
    assert verdicts(schema, sql) == expected


def test_count_with_bang_override(schema: str) -> None:
    (col,) = run_describe(schema, 'SELECT count(*) AS "n!" FROM users').columns
    assert (col.name, col.raw_name, col.nullable, col.reason) == ("n", "n!", False, "OverrideBang")


def test_question_override_strips_suffix(schema: str) -> None:
    (col,) = run_describe(schema, 'SELECT id AS "id?" FROM users').columns
    assert (col.name, col.raw_name, col.nullable, col.reason) == ("id", "id?", True, "OverrideQuestion")


def test_show_fails_explain_without_exception(schema: str) -> None:
    assert verdicts(schema, "SHOW work_mem") == [(True, "ExplainFailed")]


def test_zero_column_statement_skips_explain(schema: str) -> None:
    assert run_describe(schema, "UPDATE users SET name = name WHERE id = $1").columns == []


def test_multi_statement_rejected_before_explain(schema: str) -> None:
    with pytest.raises(Exception, match="multiple commands"):
        run_describe(schema, "SELECT 1; SELECT 2")


@pytest.mark.parametrize("case", SQLX_CASES)
def test_sqlx_cases(schema: str, case: str) -> None:
    sql, expected = SQLX_CASES[case]
    got = [(c.name, c.nullable, c.reason) for c in run_describe(schema, sql).columns]
    assert got == expected
