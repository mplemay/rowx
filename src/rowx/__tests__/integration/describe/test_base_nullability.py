import secrets

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from rowx.__tests__.integration.describe.test_base import run_checker, run_describe
from rowx._core.postgres import _catalog_queries, describe

pytestmark = [pytest.mark.integration, pytest.mark.db]


def verdicts(dsn: str, sql: str) -> list[tuple[bool, str]]:
    return [(c.nullable, c.reason) for c in run_describe(dsn, sql).columns]


def execute(dsn: str, *statements: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement.encode())


def test_table_columns(schema: str) -> None:
    assert verdicts(schema, "SELECT id, email FROM users") == [
        (False, "NotNullColumn"),
        (True, "NullableColumn"),
    ]


def test_computed_column(schema: str) -> None:
    assert verdicts(schema, "SELECT upper(name) FROM users") == [(True, "Computed")]


def test_not_valid_constraint(schema: str) -> None:
    assert verdicts(schema, "SELECT x FROM not_valid") == [(True, "NotValidatedConstraint")]


def test_view_column(schema: str) -> None:
    assert verdicts(schema, "SELECT user_id, name, post_id, title FROM user_posts") == [
        (False, "ViewColumn"),
        (False, "ViewColumn"),
        (True, "OuterJoin"),
        (True, "OuterJoin"),
    ]


def test_view_over_view(schema: str) -> None:
    execute(schema, "CREATE VIEW up2 AS SELECT user_id, title FROM user_posts")
    assert verdicts(schema, "SELECT user_id, title FROM up2") == [(False, "ViewColumn"), (True, "OuterJoin")]


def test_view_computed_column(schema: str) -> None:
    execute(schema, "CREATE VIEW vc AS SELECT id, upper(name) AS shout, name FROM users")
    assert verdicts(schema, "SELECT id, shout, name FROM vc") == [
        (False, "ViewColumn"),
        (True, "ForeignOrView"),
        (False, "ViewColumn"),
    ]


def test_view_nullable_source_column_stays_nullable(schema: str) -> None:
    execute(schema, "CREATE VIEW ve AS SELECT id, email FROM users")
    assert verdicts(schema, "SELECT id, email FROM ve") == [(False, "ViewColumn"), (True, "ForeignOrView")]


def test_view_column_on_the_outer_side_of_the_query_join_is_nullable(schema: str) -> None:
    sql = "SELECT v.name FROM posts p LEFT JOIN user_posts v ON v.post_id = p.id"
    assert verdicts(schema, sql) == [(True, "OuterJoin")]


def test_view_over_a_table_with_a_dropped_column(schema: str) -> None:
    execute(
        schema,
        "CREATE TABLE dropme (a int NOT NULL, gone int, b int, c int NOT NULL)",
        "ALTER TABLE dropme DROP COLUMN gone",
        "CREATE VIEW dv AS SELECT a, b, c FROM dropme",
    )
    # the table's attnum gap must not shift which definition position a view column maps to
    assert verdicts(schema, "SELECT a, b, c FROM dv") == [
        (False, "ViewColumn"),
        (True, "ForeignOrView"),
        (False, "ViewColumn"),
    ]


def test_view_in_other_schema_resolves_its_own_table(schema: str) -> None:
    other = f"o_{secrets.token_hex(4)}"
    execute(
        schema,
        f"CREATE SCHEMA {other}",
        # a `users` in the view's schema whose id is nullable, while the checker's own `users` has a NOT NULL id
        f"CREATE TABLE {other}.users (id int, name text)",
        f"CREATE VIEW {other}.v AS SELECT id, name FROM {other}.users",  # ruff: ignore[hardcoded-sql-expression]
        # the other way round: the view's table is NOT NULL, while the checker's `strict` is nullable
        f"CREATE TABLE {other}.strict (id int NOT NULL)",
        f"CREATE VIEW {other}.sv AS SELECT id FROM {other}.strict",  # ruff: ignore[hardcoded-sql-expression]
        "CREATE TABLE strict (id int)",
    )
    try:
        assert verdicts(schema, f"SELECT id FROM {other}.v") == [(True, "ForeignOrView")]  # ruff: ignore[hardcoded-sql-expression]
        assert verdicts(schema, f"SELECT id FROM {other}.sv") == [(False, "ViewColumn")]  # ruff: ignore[hardcoded-sql-expression]
        # an unqualified name in the definition, bound when the view was created with its own schema first on the path
        other_first = make_conninfo(schema, options=f"-c search_path={other}")
        assert verdicts(other_first, f"SELECT id FROM {other}.sv") == [(False, "ViewColumn")]  # ruff: ignore[hardcoded-sql-expression]
    finally:
        execute(schema, f"DROP SCHEMA {other} CASCADE")


def test_view_with_an_instead_rule_is_nullable(schema: str) -> None:
    execute(
        schema,
        "CREATE VIEW rv AS SELECT id, name FROM users",
        "CREATE RULE rv_ins AS ON INSERT TO rv DO INSTEAD NOTHING",
    )
    assert verdicts(schema, "SELECT id FROM rv") == [(True, "ForeignOrView")]


def test_security_barrier_and_check_option_views_are_analysed(schema: str) -> None:
    execute(
        schema,
        "CREATE VIEW sb WITH (security_barrier) AS SELECT id FROM users",
        "CREATE VIEW co AS SELECT id FROM users WHERE id > 0 WITH CHECK OPTION",
    )
    assert verdicts(schema, "SELECT id FROM sb") == [(False, "ViewColumn")]
    assert verdicts(schema, "SELECT id FROM co") == [(False, "ViewColumn")]


def test_view_over_unreadable_tables_is_nullable(schema: str) -> None:
    role = f"rowx_r_{secrets.token_hex(4)}"
    execute(
        schema,
        f"CREATE ROLE {role} LOGIN PASSWORD 'x'",
        "CREATE VIEW si AS SELECT id FROM users",
        "ALTER VIEW si SET (security_invoker = true)",
        "CREATE VIEW so AS SELECT id FROM users",
    )
    try:
        # the superuser can read `users`, so the invoker view is analysed
        assert verdicts(schema, "SELECT id FROM si") == [(False, "ViewColumn")]
        dsn = make_conninfo(schema, user=role, password="x")  # ruff: ignore[hardcoded-password-func-arg]
        schema_name = str(conninfo_to_dict(schema)["options"]).rsplit("=", 1)[1]
        execute(
            schema,
            f"GRANT USAGE ON SCHEMA {schema_name} TO {role}",
            f"GRANT SELECT ON si, so TO {role}",
        )
        # the role reads the views but not `users`, so neither definition can be planned as that role
        assert verdicts(dsn, "SELECT id FROM si") == [(True, "ForeignOrView")]
        assert verdicts(dsn, "SELECT id FROM so") == [(True, "ForeignOrView")]
    finally:
        execute(schema, "DROP VIEW si, so")
        execute(schema, f"DROP OWNED BY {role}", f"DROP ROLE {role}")


def test_view_depth_limit_falls_back(schema: str) -> None:
    execute(schema, "CREATE VIEW d0 AS SELECT id FROM users")
    for i in range(1, 10):
        execute(schema, f"CREATE VIEW d{i} AS SELECT id FROM d{i - 1}")  # ruff: ignore[hardcoded-sql-expression]
    assert verdicts(schema, "SELECT id FROM d7") == [(False, "ViewColumn")]
    # a chain deeper than 8 views is not followed to the table
    assert verdicts(schema, "SELECT id FROM d9") == [(True, "ForeignOrView")]


def test_view_resolution_leaves_no_transaction_or_search_path_behind(schema: str) -> None:
    async def body(conn) -> list[tuple[bool, str]]:
        await describe(conn, "SELECT user_id FROM user_posts")
        d = await describe(conn, "SELECT id, name FROM users")
        return [(c.nullable, c.reason) for c in d.columns]

    assert run_checker(schema, body) == [(False, "NotNullColumn"), (False, "NotNullColumn")]


def test_foreign_table_column(schema: str) -> None:
    assert verdicts(schema, "SELECT id, name FROM foreign_users") == [
        (True, "ForeignOrView"),
        (True, "ForeignOrView"),
    ]


def test_duplicate_origin_columns(schema: str) -> None:
    assert verdicts(schema, "SELECT id, id AS again FROM users") == [(False, "NotNullColumn")] * 2


def test_one_catalog_round_trip(schema: str) -> None:
    sql = (
        "SELECT u.id, u.name, u.email, p.id, p.user_id, p.title, o.id, o.amount, o.note, 1 AS one "
        "FROM users u, posts p, orders o"
    )

    async def body(conn) -> int:
        before = _catalog_queries()
        d = await describe(conn, sql)
        assert len(d.columns) == 10
        return _catalog_queries() - before

    assert run_checker(schema, body) == 1


def test_no_catalog_round_trip_without_origins(schema: str) -> None:
    async def body(conn) -> int:
        before = _catalog_queries()
        await describe(conn, "SELECT 1")
        return _catalog_queries() - before

    assert run_checker(schema, body) == 0
