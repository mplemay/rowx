import asyncio
from typing import TYPE_CHECKING, Final

import psycopg
import pytest

from rowx.__tests__.integration.describe.sqlx_cases import SQLX_CASES
from rowx.__tests__.integration.describe.test_base import run_checker, run_describe, scalar
from rowx.__tests__.integration.describe.test_base_nullability import execute
from rowx._core.postgres import connect_checker, describe
from rowx.errors import DatabaseError
from rowx.postgres import connect

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
        ("SELECT count(*) FROM users", [(False, "ComputedExpr")]),
        ("SELECT coalesce(email, 'x') FROM users", [(False, "ComputedExpr")]),
        ("SELECT 1", [(False, "ComputedExpr")]),
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
            (
                "WITH c AS MATERIALIZED (SELECT u.id, p.title @JOIN@), "
                "d AS MATERIALIZED (WITH c AS MATERIALIZED (SELECT p.user_id AS id, p.title FROM posts p) "
                "SELECT id, title FROM c) SELECT o.title, d.title FROM c o JOIN d ON o.id = d.id"
            ),
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
        "shadowed_cte_name",
        "swapped_aliases",
        "union_all_branch_nulls",
    ],
)
def test_scan_boundary_mapping(schema: str, sql: str, expected: list[tuple[bool, str]]) -> None:
    assert verdicts(schema, sql.replace("@JOIN@", JOIN)) == expected


def test_zero_parameter_statement_is_explained_without_parentheses(schema: str) -> None:
    d = run_describe(schema, "SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id")
    assert d.params == []
    assert [(c.nullable, c.reason) for c in d.columns] == [(True, "OuterJoin")]


@pytest.mark.parametrize("count", [1, 3, 100, 1000])
def test_many_parameter_statement_gets_one_null_per_parameter(schema: str, count: int) -> None:
    marks = ", ".join(f"${i}::int4" for i in range(1, count + 1))
    sql = f"SELECT p.title, u.id FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id IN ({marks})"  # ruff: ignore[hardcoded-sql-expression]
    d = run_describe(schema, sql)
    assert [p.type_key for p in d.params] == ["int4"] * count
    assert [(c.nullable, c.reason) for c in d.columns] == [(True, "OuterJoin"), (False, "NotNullColumn")]


@pytest.mark.parametrize("mode", ["auto", "force_custom_plan", "force_generic_plan"])
def test_the_session_plan_cache_mode_is_unchanged_by_describe(schema: str, mode: str) -> None:
    async def body(conn: Connection) -> list[object]:
        await scalar(conn, f"SELECT set_config('plan_cache_mode', '{mode}', false)")
        found: list[object] = [await scalar(conn, "SHOW plan_cache_mode")]
        await describe(conn, "SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = $1")
        found.append(await scalar(conn, "SHOW plan_cache_mode"))
        # the pinned transaction of a view definition leaves nothing behind either
        await describe(conn, "SELECT user_id, title FROM user_posts")
        found.append(await scalar(conn, "SHOW plan_cache_mode"))
        return found

    assert run_checker(schema, body) == [mode] * 3


# a view over a left join: its NOT NULL side is only known once the definition's own plan pass has run
def test_view_over_a_left_join_is_resolved_through_its_plan(schema: str) -> None:
    execute(
        schema,
        "CREATE VIEW lj AS SELECT u.id AS uid, u.name AS uname, p.title AS ptitle "
        "FROM users u LEFT JOIN posts p ON p.user_id = u.id",
        "CREATE VIEW lj2 AS SELECT uid, ptitle FROM lj",
    )
    assert verdicts(schema, "SELECT uid, uname, ptitle FROM lj") == [
        (False, "ViewColumn"),
        (False, "ViewColumn"),
        (True, "OuterJoin"),
    ]
    assert verdicts(schema, "SELECT uid, ptitle FROM lj2") == [(False, "ViewColumn"), (True, "OuterJoin")]
    assert verdicts(schema, "SELECT x.ptitle FROM users u LEFT JOIN lj x ON x.uid = u.id") == [(True, "OuterJoin")]


def test_multi_statement_with_a_side_effect_is_rejected_and_never_runs(schema: str) -> None:
    async def body(conn: Connection) -> object:
        with pytest.raises(DatabaseError, match="multiple commands"):
            await describe(conn, "SELECT 1; CREATE TABLE rowx_never_made (a int)")
        with pytest.raises(DatabaseError, match="multiple commands"):
            await describe(conn, "SELECT 1; SELECT 2;")
        return await scalar(conn, "SELECT to_regclass('rowx_never_made') IS NULL")

    assert run_checker(schema, body) is True


DOMAINS: Final[dict[str, str]] = {
    "nn_int": "CREATE DOMAIN nn_int AS int NOT NULL",
    "ck_int": "CREATE DOMAIN ck_int AS int CHECK (VALUE IS NOT NULL)",
    "nn_nested": "CREATE DOMAIN nn_inner AS int NOT NULL; CREATE DOMAIN nn_nested AS nn_inner",
}


# `EXPLAIN ... EXECUTE name(NULL)` raises for a parameter whose domain rejects NULL, while the plan itself is fine
@pytest.mark.parametrize("domain", list(DOMAINS))
def test_null_rejecting_domain_parameter_keeps_the_outer_join(schema: str, domain: str) -> None:
    execute(schema, *DOMAINS[domain].split("; "))
    sql = f"SELECT p.title, u.id FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = $1::{domain}"  # ruff: ignore[hardcoded-sql-expression]
    d = run_describe(schema, sql)
    assert [p.type_name for p in d.params] == [domain]
    assert [(c.nullable, c.reason) for c in d.columns] == [(True, "OuterJoin"), (False, "NotNullColumn")]


# the parameter takes the domain of the column it is assigned to, with no cast in the statement
@pytest.mark.parametrize("domain", ["nn_int", "ck_int"])
def test_null_rejecting_domain_parameter_inferred_from_a_column(schema: str, domain: str) -> None:
    execute(
        schema,
        DOMAINS[domain],
        f"CREATE TABLE dom_t (id int PRIMARY KEY, d {domain} NOT NULL DEFAULT 1)",
    )
    sql = (
        "UPDATE dom_t t SET d = $1 FROM users u LEFT JOIN posts p ON p.user_id = u.id "
        "WHERE t.id = u.id RETURNING p.title, t.id"
    )
    d = run_describe(schema, sql)
    assert [p.type_name for p in d.params] == [domain]
    assert [(c.nullable, c.reason) for c in d.columns] == [(True, "OuterJoin"), (False, "NotNullColumn")]


def test_domain_parameter_without_a_null_check_is_explained_through_its_base_type(schema: str) -> None:
    # positive_int accepts NULL; it is still never explained as prepared, and the verdicts are the same
    d = run_describe(
        schema,
        "SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = $1::positive_int",
    )
    assert [(c.nullable, c.reason) for c in d.columns] == [(True, "OuterJoin")]


LOCK_KEY: Final[int] = 229_250
LOCKED_JOIN: Final[str] = (
    "SELECT p.title, u.id FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = $1::lock_dom"
)


def advisory_locks_held(dsn: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = 0 AND objid = %s",
            [LOCK_KEY],
        ).fetchone()
    assert row is not None
    assert isinstance(row[0], int)
    return row[0]


# `EXPLAIN ... EXECUTE name(NULL)` would evaluate a domain's constraint, so a CHECK that takes a session lock
# would leave it held on the checker's connection, which describe keeps open
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT $1::lock_dom", [(True, "Computed")]),
        (LOCKED_JOIN, [(True, "OuterJoin"), (False, "NotNullColumn")]),
    ],
)
def test_a_domain_check_never_runs_during_describe(schema: str, sql: str, expected: list[tuple[bool, str]]) -> None:
    execute(schema, f"CREATE DOMAIN lock_dom AS int CHECK (pg_try_advisory_lock({LOCK_KEY}))")

    async def body(conn: Connection) -> tuple[list[tuple[bool, str]], object, int]:
        d = await describe(conn, sql)
        held = await scalar(conn, f"SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = {LOCK_KEY}")  # ruff: ignore[hardcoded-sql-expression]
        # read while the checker's connection is still open
        return [(c.nullable, c.reason) for c in d.columns], held, advisory_locks_held(schema)

    got, own, others = run_checker(schema, body)
    assert got == expected
    assert (own, others) == (0, 0)


# a volatile function in a CHECK, on a connection that is allowed to write
@pytest.mark.parametrize("checker", [True, False])
def test_a_volatile_domain_check_has_no_side_effect_during_describe(schema: str, *, checker: bool) -> None:
    execute(
        schema,
        "CREATE TABLE domain_effects (n int)",
        "CREATE SEQUENCE domain_effects_seq",
        "CREATE FUNCTION record_effect() RETURNS boolean LANGUAGE plpgsql VOLATILE AS "
        "$$ BEGIN INSERT INTO domain_effects VALUES (1); PERFORM nextval('domain_effects_seq'); RETURN true; END $$",
        "CREATE DOMAIN effect_dom AS int CHECK (record_effect())",
    )
    sql = "SELECT p.title, u.id FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = $1::effect_dom"

    async def run() -> list[tuple[bool, str]]:
        if checker:
            conn = await connect_checker(schema)
        else:
            conn = (await connect(schema))._impl
        try:
            return [(c.nullable, c.reason) for c in (await describe(conn, sql)).columns]
        finally:
            await conn.close()

    assert asyncio.run(run()) == [(True, "OuterJoin"), (False, "NotNullColumn")]
    with psycopg.connect(schema, autocommit=True) as conn:
        rows = conn.execute("SELECT count(*) FROM domain_effects").fetchone()
        called = conn.execute("SELECT is_called FROM domain_effects_seq").fetchone()
    assert rows == (0,)
    assert called == (False,)
