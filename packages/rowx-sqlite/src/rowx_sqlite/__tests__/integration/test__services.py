import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from rowx_core.errors import DatabaseError
from rowx_sqlite import ConnectOptions, Migrator, connect
from rowx_sqlite._checker import SqliteCheckerSession
from rowx_sqlite._services import SqliteMigrations, open_checker

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "1_users.sql").write_text("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);", encoding="utf-8")
    return directory


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE t (id INTEGER NOT NULL)")
    return path


@pytest.mark.asyncio
async def test_open_checker_returns_a_session_that_describes_without_changing_the_file(database: Path) -> None:
    session = await open_checker(f"sqlite:///{database}", 5.0)
    assert isinstance(session, SqliteCheckerSession)
    try:
        described = await session.describe("SELECT id FROM t")
        assert [(c.name, c.nullable) for c in described.columns] == [("id", False)]
    finally:
        await session.close()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM t").fetchone() == (0,)


@pytest.mark.asyncio
async def test_open_checker_fails_for_a_missing_file_as_operational(tmp_path: Path) -> None:
    with pytest.raises(DatabaseError) as raised:
        await open_checker(f"sqlite:///{tmp_path / 'missing.db'}", 5.0)
    assert (raised.value.sqlstate, raised.value.code) == ("58030", "SQLITE_CANTOPEN")
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.asyncio
async def test_the_migrator_can_still_use_a_writable_file(tmp_path: Path, migrations: Path) -> None:
    path = tmp_path / "w.db"
    async with await connect(ConnectOptions(path=str(path), mode="rwc")) as connection:
        await Migrator(migrations).run(connection)
    session = await open_checker(f"sqlite:///{path}", 5.0)
    try:
        assert (await session.describe("SELECT id FROM users")).columns[0].name == "id"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_a_readonly_migration_session_waits_for_a_lock_up_to_the_connect_timeout(
    database: Path, migrations: Path
) -> None:
    service = SqliteMigrations(f"sqlite:///{database}", migrations)
    async with await service.open(readonly=True, connect_timeout=1.25) as session:
        # the session's connection is internal; the pragma reads the busy timeout it was opened with
        rows = await session._connection._impl._fetch(
            "PRAGMA busy_timeout",
            [],
            None,
            False,  # ruff: ignore[boolean-positional-value-in-call]
            True,  # ruff: ignore[boolean-positional-value-in-call]
            None,
            None,
        )
        assert rows == [(1250,)]
