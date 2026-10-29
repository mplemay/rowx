from typing import TYPE_CHECKING, Final

import pytest

from rowx.__tests__.integration.describe.sqlx_cases import SQLX_CASES
from rowx.__tests__.integration.describe.test_base import run_checker, run_describe, scalar
from rowx._core.postgres import describe

if TYPE_CHECKING:
    from rowx._core.postgres import Connection

pytestmark = [pytest.mark.integration, pytest.mark.db]


def verdicts(dsn: str, sql: str) -> list[tuple[bool, str]]:
    return [(c.nullable, c.reason) for c in run_describe(dsn, sql).columns]


# one case per row of the evidence table in docs/design/nullability.md, in table order
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
    ids=[
        "left_join_where_param",
        "right_join",
        "left_join_order_by",
        "trivial_subquery_elided",
        "subquery_row_number_scan_boundary",
        "materialized_cte_scan_boundary",
        "rollup_grouping_sets",
        "returning_old",
        "duplicate_columns",
        "not_valid_constraint",
        "full_join",
        "left_join_reduced_to_inner",
        "computed_upper",
        "computed_count",
        "computed_coalesce",
        "computed_literal",
    ],
)
def test_design_table(schema: str, sql: str, expected: list[tuple[bool, str]]) -> None:
    assert verdicts(schema, sql) == expected


# sqlx#3541 and #4274 (describe.rs it_forces_generic_plan_for_describe): a session that forces custom plans
# would fold `= NULL` and drop the outer join, so describe must plan generically whatever the session says
@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan", "auto"])
def test_generic_plan_is_used_whatever_the_session_mode(schema: str, mode: str) -> None:
    async def body(conn: Connection) -> list[tuple[bool, str]]:
        await scalar(conn, f"SELECT set_config('plan_cache_mode', '{mode}', false)")
        d = await describe(conn, "SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = $1")
        return [(c.nullable, c.reason) for c in d.columns]

    assert run_checker(schema, body) == [(True, "OuterJoin")]


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


def test_reason_detail_names_node_or_alias(schema: str) -> None:
    join = "FROM users u LEFT JOIN posts p ON p.user_id = u.id"
    (outer,) = run_describe(schema, f"SELECT p.title {join}").columns
    assert outer.reason == "OuterJoin"
    assert outer.reason_detail in {"Nested Loop", "Hash Join", "Merge Join"}
    (boundary,) = run_describe(schema, f"SELECT x.title FROM (SELECT p.title, row_number() OVER () {join}) x").columns  # ruff: ignore[hardcoded-sql-expression]
    assert (boundary.reason, boundary.reason_detail) == ("ScanBoundary", "x")
    (plain,) = run_describe(schema, "SELECT id FROM users").columns
    assert plain.reason_detail is None


# found by the fuzzer (#109): the planner turns a right join filtered by `IS NULL` on its nullable side into an anti
# join, and the executor feeds a null tuple for the inner side, so the columns it outputs are NULL
def test_anti_join_output_from_the_null_extended_side_is_nullable(schema: str) -> None:
    sql = "SELECT t0.name FROM users t0 RIGHT JOIN users t1 ON t1.email = t0.email WHERE t0.email IS NULL"
    assert verdicts(schema, sql) == [(True, "OuterJoin")]


# found by the fuzzer (#109): a sorted CUBE or ROLLUP chain is one "Grouping Sets" entry that lists every set
@pytest.mark.parametrize("grouping", ["CUBE (t0.id)", "ROLLUP (t0.id)"])
def test_sorted_grouping_set_chain_is_nullable(schema: str, grouping: str) -> None:
    sql = (
        "SELECT t0.id, count(*) FROM users t0 FULL JOIN users t2 ON true "  # ruff: ignore[hardcoded-sql-expression]
        f"WHERE t0.email IS NOT NULL GROUP BY {grouping}"
    )
    assert verdicts(schema, sql) == [(True, "GroupingSets")] * 2


JOIN: Final[str] = "FROM users u LEFT JOIN posts p ON p.user_id = u.id"
NOT_NULL: Final[tuple[bool, str]] = (False, "NotNullColumn")
BOUNDARY: Final[tuple[bool, str]] = (True, "ScanBoundary")


# scan-boundary mapping (#105): the same shapes as the plan fixtures, through describe with real origins;
# @JOIN@ stands for the left join above
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT x.id, x.title FROM (SELECT u.id, p.title, row_number() OVER () @JOIN@) x", [NOT_NULL, BOUNDARY]),
        ("WITH c AS MATERIALIZED (SELECT u.id, p.title @JOIN@) SELECT id, title FROM c", [NOT_NULL, BOUNDARY]),
        (
            (
                "WITH a AS MATERIALIZED (SELECT u.id, p.title @JOIN@), "
                "b AS MATERIALIZED (SELECT id, title FROM a) SELECT id, title FROM b"
            ),
            [NOT_NULL, BOUNDARY],
        ),
        (
            "WITH c AS MATERIALIZED (SELECT u.id, p.title @JOIN@) SELECT a.id, b.title FROM c a JOIN c b ON a.id = b.id",
            [NOT_NULL, BOUNDARY],
        ),
        (
            "SELECT x.id, x.title FROM (SELECT u.id, upper(p.title) AS title, row_number() OVER () @JOIN@) x",
            [NOT_NULL, BOUNDARY],
        ),
        (
            "SELECT x.id, x.pid FROM (SELECT u.id, p.id AS pid, row_number() OVER () @JOIN@) x",
            [BOUNDARY, BOUNDARY],
        ),
        ("WITH c AS MATERIALIZED (SELECT u.id, p.title @JOIN@) SELECT id FROM c", [NOT_NULL]),
        (
            "SELECT x.name, x.email FROM (SELECT name, email, count(*) FROM users GROUP BY GROUPING SETS ((name), (email))) x",
            [BOUNDARY, BOUNDARY],
        ),
        (
            "WITH c AS MATERIALIZED (SELECT p.title AS name, u.name AS title @JOIN@) SELECT name, title FROM c",
            [BOUNDARY, BOUNDARY],
        ),
        (
            (
                "SELECT x.title FROM (SELECT y.title, row_number() OVER () FROM "
                "(SELECT title FROM posts UNION ALL SELECT p.title @JOIN@) y) x"
            ),
            [BOUNDARY],
        ),
    ],
    ids=[
        "subquery_non_null_side",
        "materialized_cte_non_null_side",
        "cte_chain",
        "cte_referenced_twice",
        "expression_output",
        "same_named_source_columns",
        "unused_column_pruned",
        "grouping_sets",
        "swapped_aliases",
        "union_all_branch_nulls",
    ],
)
def test_scan_boundary_mapping(schema: str, sql: str, expected: list[tuple[bool, str]]) -> None:
    assert verdicts(schema, sql.replace("@JOIN@", JOIN)) == expected
