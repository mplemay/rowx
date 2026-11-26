import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from rowx_core.errors import ConnectionBusyError, DatabaseError, InterfaceError
from rowx_sqlite._checker import SQL_ERROR_HELP, SqliteCheckerSession
from rowx_sqlite._services import open_checker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from rowx_core.backend import Failure


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> AsyncIterator[SqliteCheckerSession]:
    path = tmp_path / "check.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE posts (user_id INTEGER NOT NULL, title TEXT NOT NULL);
            CREATE VIEW user_posts AS SELECT users.id AS user_id, posts.title AS title
              FROM users LEFT JOIN posts ON posts.user_id = users.id;
            """,
        )
    opened = await open_checker(f"sqlite:///{path}", 5.0)
    try:
        yield opened
    finally:
        await opened.close()


@pytest.mark.parametrize(
    ("sqlstate", "failure"),
    [
        ("57014", "timeout"),
        ("55P03", "operational"),
        ("53100", "operational"),
        ("58030", "operational"),
        ("XX001", "operational"),
        ("XX002", "operational"),
        ("25006", "operational"),
        ("HY000", "other"),
        ("23000", "other"),
    ],
)
def test_a_database_error_is_classified_by_its_sqlstate(
    session: SqliteCheckerSession, sqlstate: str, failure: Failure
) -> None:
    assert session.classify(DatabaseError(sqlstate, "x", code="SQLITE_X")) == failure


def test_an_interface_error_is_lost_for_a_closed_connection(session: SqliteCheckerSession) -> None:
    assert session.classify(InterfaceError("the connection is closed")) == "lost"


@pytest.mark.parametrize("error", [InterfaceError("connection is broken"), ConnectionBusyError("busy")])
def test_a_broken_or_busy_connection_is_lost(session: SqliteCheckerSession, error: InterfaceError) -> None:
    assert session.classify(error) == "lost"


def test_any_other_exception_is_other(session: SqliteCheckerSession) -> None:
    assert session.classify(ValueError("x")) == "other"


@pytest.mark.asyncio
async def test_a_real_sql_error_is_other_with_its_native_code_and_a_character_position(
    session: SqliteCheckerSession,
) -> None:
    with pytest.raises(DatabaseError) as raised:
        await session.describe("SELECT 'é日', nam FROM users")
    error = raised.value
    assert (error.sqlstate, error.code) == ("HY000", "SQLITE_ERROR")
    # 1-based, counted in characters rather than the UTF-8 bytes that SQLite reports
    assert error.position == len("SELECT 'é日', ") + 1
    assert session.classify(error) == "other"


@pytest.mark.asyncio
async def test_a_real_syntax_error_has_a_position(session: SqliteCheckerSession) -> None:
    with pytest.raises(DatabaseError) as raised:
        await session.describe("SELECT FROM users")
    assert raised.value.code == "SQLITE_ERROR"
    assert raised.value.position == len("SELECT ") + 1


@pytest.mark.asyncio
async def test_a_closed_session_fails_as_lost(session: SqliteCheckerSession) -> None:
    await session.close()
    with pytest.raises(InterfaceError) as raised:
        await session.describe("SELECT 1")
    assert session.classify(raised.value) == "lost"


@pytest.mark.asyncio
async def test_describe_proves_a_plain_table_column_and_the_preserved_side_of_a_left_join_view(
    session: SqliteCheckerSession,
) -> None:
    plain = await session.describe("SELECT id, name FROM users")
    assert [(c.name, c.type_key, c.nullable, c.reason) for c in plain.columns] == [
        ("id", "integer", False, "NotNullColumn"),
        ("name", "text", False, "NotNullColumn"),
    ]
    view = await session.describe("SELECT user_id, title FROM user_posts")
    # the join builds an automatic index, which the bytecode analysis reads: only the right side can be NULL
    assert [(c.nullable, c.reason) for c in view.columns] == [(False, "NotNullColumn"), (True, "Unproven")]


@pytest.mark.asyncio
async def test_describe_proves_a_computed_column_from_the_bytecode(session: SqliteCheckerSession) -> None:
    described = await session.describe(
        "SELECT count(*) AS n, typeof(id) AS t, coalesce(name, 'x') AS c, 'x' AS s FROM users"
    )
    assert [(c.name, c.type_key, c.type_name, c.nullable, c.reason) for c in described.columns] == [
        ("n", "integer", "INTEGER", False, "ComputedExpr"),
        ("t", "text", "TEXT", False, "ComputedExpr"),
        ("c", None, "", False, "ComputedExpr"),
        ("s", "text", "TEXT", False, "ComputedExpr"),
    ]
    # a total of +inf and -inf is NaN, which SQLite returns as NULL
    total = await session.describe("SELECT total(id) AS t FROM users")
    assert [(c.name, c.type_key, c.nullable, c.reason) for c in total.columns] == [
        ("t", "real", True, "Unproven"),
    ]
    casts = await session.describe("SELECT CAST(id AS TEXT) AS k, upper(name) AS u FROM users")
    assert [(c.name, c.type_key, c.type_name, c.nullable, c.reason) for c in casts.columns] == [
        ("k", "text", "TEXT", False, "ComputedExpr"),
        ("u", "text", "TEXT", False, "ComputedExpr"),
    ]


@pytest.mark.asyncio
async def test_describe_proves_the_left_side_of_a_rowid_left_join(session: SqliteCheckerSession) -> None:
    described = await session.describe(
        "SELECT users.id, posts.title, coalesce(posts.title, 'none') AS c FROM users LEFT JOIN posts ON posts.rowid = users.id"
    )
    assert [(c.nullable, c.reason) for c in described.columns] == [
        (False, "NotNullColumn"),
        (True, "Unproven"),
        (False, "ComputedExpr"),
    ]


@pytest.mark.asyncio
async def test_describe_leaves_a_column_that_can_be_null_unproven(session: SqliteCheckerSession) -> None:
    described = await session.describe("SELECT sum(id) AS s, max(id) AS m, avg(id) AS a, id / 1 AS d FROM users")
    assert [(c.name, c.type_key, c.nullable, c.reason) for c in described.columns] == [
        ("s", None, True, "Unproven"),
        ("m", None, True, "Unproven"),
        ("a", "real", True, "Unproven"),
        ("d", None, True, "Unproven"),
    ]


@pytest.mark.asyncio
async def test_describe_proves_a_computed_column_of_a_nested_statement(session: SqliteCheckerSession) -> None:
    for sql in (
        "SELECT DISTINCT name FROM users UNION SELECT title FROM posts",
        "SELECT n FROM (SELECT count(*) AS n FROM users GROUP BY name LIMIT 5)",
        "WITH c AS MATERIALIZED (SELECT id + 1 AS n FROM users) SELECT n FROM c",
        "SELECT (SELECT count(*) FROM posts)",
        "SELECT EXISTS (SELECT 1 FROM posts)",
    ):
        described = await session.describe(sql)
        assert [(c.nullable, c.reason) for c in described.columns] == [(False, "ComputedExpr")], sql
    returning = await session.describe("INSERT INTO posts (user_id, title) VALUES (1, 'x') RETURNING title || '!' AS t")
    assert [(c.nullable, c.reason) for c in returning.columns] == [(False, "ComputedExpr")]


@pytest.mark.asyncio
async def test_describe_leaves_a_nested_column_that_can_be_null_unproven(session: SqliteCheckerSession) -> None:
    for sql in (
        "SELECT (SELECT max(id) FROM users)",
        "SELECT id FROM users UNION ALL SELECT NULL",
        "SELECT t.title FROM users LEFT JOIN (SELECT user_id, title FROM posts LIMIT 3) t ON t.user_id = users.id",
    ):
        described = await session.describe(sql)
        assert [(c.nullable, c.reason) for c in described.columns] == [(True, "Unproven")], sql


@pytest.mark.asyncio
async def test_describe_stays_silent_for_a_statement_the_bytecode_does_not_model(
    session: SqliteCheckerSession,
) -> None:
    described = await session.describe("SELECT count(*) OVER () AS n FROM users")
    assert [(c.nullable, c.reason) for c in described.columns] == [(True, "Unproven")]
    described = await session.describe("SELECT value FROM json_each('[1]')")
    assert [(c.type_key, c.nullable, c.reason) for c in described.columns] == [(None, True, "Unproven")]


@pytest.mark.asyncio
async def test_describe_reports_untyped_parameters(session: SqliteCheckerSession) -> None:
    described = await session.describe("SELECT id FROM users WHERE id = ?1 AND name = ?2")
    assert [param.type_key for param in described.params] == [None, None]


@pytest.mark.parametrize("code", sorted(SQL_ERROR_HELP))
def test_the_help_is_keyed_by_native_result_names(code: str) -> None:
    assert code.startswith("SQLITE_")
    assert SQL_ERROR_HELP[code]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", ["SELECT id FROM users; SELECT 1", "", "-- nothing"])
async def test_sql_that_is_not_one_statement_is_a_sql_error_not_a_lost_connection(
    session: SqliteCheckerSession, sql: str
) -> None:
    with pytest.raises(DatabaseError) as raised:
        await session.describe(sql)
    error = raised.value
    assert (error.sqlstate, error.code, error.position) == ("HY000", None, None)
    assert session.classify(error) == "other"


@pytest.mark.asyncio
async def test_a_describe_after_close_is_still_lost_for_any_sql(session: SqliteCheckerSession) -> None:
    await session.close()
    with pytest.raises(InterfaceError) as raised:
        await session.describe("SELECT id FROM users; SELECT 1")
    assert session.classify(raised.value) == "lost"


@pytest.mark.asyncio
async def test_describe_types_a_compound_select_by_every_arm_not_the_first(tmp_path: Path) -> None:
    path = tmp_path / "compound.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE s (i INTEGER NOT NULL, tx TEXT NOT NULL, r REAL NOT NULL, i2 INTEGER NOT NULL) STRICT;
            CREATE TABLE t (a INTEGER, b TEXT);
            CREATE VIEW sv AS SELECT i FROM s UNION ALL SELECT tx FROM s;
            """,
        )
    session = await open_checker(f"sqlite:///{path}", 5.0)
    try:
        # SQLite declares each of these with the leftmost arm, INTEGER, but other arms produce text
        for sql in (
            "SELECT i FROM s UNION ALL SELECT tx FROM s",
            "SELECT k FROM (SELECT i AS k FROM s UNION ALL SELECT tx FROM s)",
            "WITH c (k) AS (SELECT i FROM s UNION ALL SELECT tx FROM s) SELECT k FROM c",
            "SELECT i FROM sv",
            "SELECT a FROM t UNION SELECT b FROM t",
        ):
            described = await session.describe(sql)
            assert [(c.type_key, c.type_name) for c in described.columns] == [(None, "")], sql
        alike = await session.describe("SELECT i FROM s UNION ALL SELECT i2 FROM s")
        assert [(c.type_key, c.nullable) for c in alike.columns] == [("integer", False)]
    finally:
        await session.close()
