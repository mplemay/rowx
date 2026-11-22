import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from rowx_core.errors import DatabaseError, InterfaceError
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


def test_an_interface_error_is_lost(session: SqliteCheckerSession) -> None:
    assert session.classify(InterfaceError("the connection is closed")) == "lost"


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
async def test_describe_proves_a_plain_table_column_and_leaves_a_left_join_view_unproven(
    session: SqliteCheckerSession,
) -> None:
    plain = await session.describe("SELECT id, name FROM users")
    assert [(c.name, c.type_key, c.nullable, c.reason) for c in plain.columns] == [
        ("id", "integer", False, "NotNullColumn"),
        ("name", "text", False, "NotNullColumn"),
    ]
    view = await session.describe("SELECT user_id, title FROM user_posts")
    assert [(c.nullable, c.reason) for c in view.columns] == [(True, "Unproven"), (True, "Unproven")]


@pytest.mark.asyncio
async def test_describe_reports_untyped_parameters(session: SqliteCheckerSession) -> None:
    described = await session.describe("SELECT id FROM users WHERE id = ?1 AND name = ?2")
    assert [param.type_key for param in described.params] == [None, None]


@pytest.mark.parametrize("code", sorted(SQL_ERROR_HELP))
def test_the_help_is_keyed_by_native_result_names(code: str) -> None:
    assert code.startswith("SQLITE_")
    assert SQL_ERROR_HELP[code]
