import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from rowx_core.errors import DatabaseError, MigrateError
from rowx_core.lifecycle import ORPHAN_AGE_SECONDS
from rowx_sqlite import ConnectOptions, Migrator, connect
from rowx_sqlite._checker import SqliteCheckerSession
from rowx_sqlite._scratch import PREFIX, SqliteScratch, scratch_url
from rowx_sqlite._services import SqliteMigrations, open_checker


def names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def exists(path: Path) -> bool:
    return path.exists()


@pytest.fixture
def scratch_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "scratch"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


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
async def test_the_scratch_is_a_migrated_temporary_file_that_every_worker_shares(
    scratch_dir: Path, migrations: Path
) -> None:
    service = SqliteMigrations("sqlite::memory:", migrations)
    scratch = SqliteScratch("sqlite::memory:")
    async with scratch.session(service, 4) as build:
        assert build.urls == (scratch.url,)
        assert scratch.url.startswith("sqlite:////")
        assert scratch.path.parent == scratch_dir
        assert scratch.path.name.startswith(PREFIX)
        first = await open_checker(build.urls[0], 5.0)
        second = await open_checker(build.urls[0], 5.0)
        try:
            for session in (first, second):
                described = await session.describe("SELECT id, name FROM users")
                assert [c.name for c in described.columns] == ["id", "name"]
        finally:
            await first.close()
            await second.close()
        # no WAL file is left, and the read-only workers above opened it
        assert not exists(Path(f"{scratch.path}-wal"))
        assert not exists(Path(f"{scratch.path}-shm"))
    assert names(scratch_dir) == []


def test_the_url_of_a_posix_path_has_four_slashes() -> None:
    url = scratch_url(PurePosixPath("/var/data/rowx_check_a b.db"))
    assert url == "sqlite:////var/data/rowx_check_a%20b.db"
    assert ConnectOptions.from_dsn(url).path == "/var/data/rowx_check_a b.db"


def test_the_url_of_a_drive_path_keeps_its_colon() -> None:
    url = scratch_url(PureWindowsPath("C:\\Users\\me\\Temp\\rowx_check_a.db"))
    assert url == "sqlite:///C:/Users/me/Temp/rowx_check_a.db"
    assert ConnectOptions.from_dsn(url).path == "C:/Users/me/Temp/rowx_check_a.db"


@pytest.mark.asyncio
async def test_the_scratch_is_removed_when_a_migration_fails(scratch_dir: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "1_bad.sql").write_text("CREATE TABLE (", encoding="utf-8")
    scratch = SqliteScratch("sqlite::memory:")
    with pytest.raises(MigrateError):
        async with scratch.session(SqliteMigrations("sqlite::memory:", broken), 1):
            pytest.fail("the session must not open")
    assert names(scratch_dir) == []


def test_a_scratch_needs_a_worker(migrations: Path) -> None:
    with pytest.raises(ValueError, match="at least one worker"):
        SqliteScratch("sqlite::memory:").session(SqliteMigrations("sqlite::memory:", migrations), 0)


@pytest.mark.asyncio
async def test_sweep_removes_what_an_interrupted_run_left(scratch_dir: Path, migrations: Path) -> None:
    scratch = SqliteScratch("sqlite::memory:")
    async with scratch.session(SqliteMigrations("sqlite::memory:", migrations), 1):
        assert names(scratch_dir) == [scratch.path.name]
        scratch.sweep()
        assert names(scratch_dir) == []
    scratch.sweep()


@pytest.mark.asyncio
async def test_an_old_orphan_is_swept_and_a_fresh_one_is_kept(scratch_dir: Path, migrations: Path) -> None:
    old, fresh = scratch_dir / f"{PREFIX}old.db", scratch_dir / f"{PREFIX}fresh.db"
    other = scratch_dir / "unrelated.db"
    for path in (old, fresh, other):
        path.write_bytes(b"")
    (scratch_dir / f"{PREFIX}old.db-wal").write_bytes(b"")
    past = time.time() - ORPHAN_AGE_SECONDS - 60
    for path in (old, other):
        os.utime(path, (past, past))
    async with SqliteScratch("sqlite::memory:").session(SqliteMigrations("sqlite::memory:", migrations), 1):
        assert not old.exists()
        assert not (scratch_dir / f"{PREFIX}old.db-wal").exists()
        assert fresh.exists()
        assert other.exists()


@pytest.mark.asyncio
async def test_sweep_previous_can_be_turned_off(scratch_dir: Path, migrations: Path) -> None:
    old = scratch_dir / f"{PREFIX}old.db"
    old.write_bytes(b"")
    past = time.time() - ORPHAN_AGE_SECONDS - 60
    os.utime(old, (past, past))
    scratch = SqliteScratch("sqlite::memory:", sweep_previous=False)
    async with scratch.session(SqliteMigrations("sqlite::memory:", migrations), 1):
        assert old.exists()


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
