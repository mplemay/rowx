import asyncio
import sqlite3
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

import pytest
from rowx_core.errors import DatabaseError, InterfaceError
from rowx_sqlite import ConnectOptions, connect
from rowx_sqlite._admin import SqliteAdmin
from rowx_sqlite._backend import load

from rowx.check.cli import main

if TYPE_CHECKING:
    from rowx_sqlite.__tests__.conftest import CheckProject


def url_of(path: Path) -> str:
    return f"sqlite:///{path}"


def seed(path: Path, *, wal: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE t (v TEXT)")
    connection.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",), ("c",)])
    connection.commit()
    return connection


def fetch(path: Path, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as raw:
        rows: list[tuple[Any, ...]] = raw.execute(sql).fetchall()
    return rows


def change(path: Path, sql: str) -> None:
    with closing(sqlite3.connect(path)) as raw:
        raw.execute(sql)
        raw.commit()


def present(path: Path) -> bool:
    return path.exists()


def remove(path: Path) -> None:
    path.unlink()


def listing(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_the_admin_factory_is_registered() -> None:
    backend = load()
    assert backend.admin_factory is SqliteAdmin
    assert backend.admin("sqlite:///x.db").database == "x.db"


def test_the_database_is_the_path_of_the_url(tmp_path: Path) -> None:
    assert SqliteAdmin(url_of(tmp_path / "a b.db")).database == str(tmp_path / "a b.db")
    assert SqliteAdmin("sqlite:///relative.db").database == "relative.db"
    assert SqliteAdmin("sqlite::memory:").database is None


def test_with_database_builds_a_file_url_for_the_path() -> None:
    admin = SqliteAdmin("sqlite:///x.db")
    assert admin.with_database("rel/a.db") == "sqlite:///rel/a.db"
    assert admin.with_database("/abs/a b.db") == "sqlite:////abs/a%20b.db"
    assert ConnectOptions.from_dsn(admin.with_database("/abs/a b.db")).path == "/abs/a b.db"
    assert admin.with_database(str(PurePosixPath("/abs/a.db"))) == "sqlite:////abs/a.db"
    assert SqliteAdmin.with_database(PureWindowsPath("C:/data/a.db").as_posix()) == "sqlite:///C:/data/a.db"


@pytest.mark.asyncio
async def test_a_path_that_does_not_exist_is_not_there_and_dropping_it_is_a_no_op(tmp_path: Path) -> None:
    admin = SqliteAdmin(url_of(tmp_path / "none.db"))
    assert not await admin.exists()
    await admin.drop()
    await admin.ready(1.0)
    assert not await admin.exists()
    assert await admin.list("none") == []
    assert listing(tmp_path) == []


@pytest.mark.asyncio
async def test_a_missing_parent_directory_is_a_clear_error(tmp_path: Path) -> None:
    admin = SqliteAdmin(url_of(tmp_path / "nope" / "a.db"))
    assert not await admin.exists()
    await admin.drop()
    with pytest.raises(DatabaseError, match="does not exist") as ready:
        await admin.ready(1.0)
    assert ready.value.sqlstate == "58030"
    with pytest.raises(DatabaseError, match="does not exist"):
        await admin.create()
    assert listing(tmp_path) == []


@pytest.mark.asyncio
async def test_create_makes_an_empty_database_that_connects_and_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    admin = SqliteAdmin(url_of(path))
    await admin.create(comment="ignored")
    assert await admin.exists()
    assert await admin.exists(str(path))
    async with await connect(ConnectOptions(path=str(path), mode="rw")) as connection:
        await connection.ping()
    path.write_bytes(b"precious")
    with pytest.raises(DatabaseError) as error:
        await admin.create()
    assert error.value.sqlstate == "42P04"
    assert admin.duplicate(error.value)
    assert path.read_bytes() == b"precious"


@pytest.mark.asyncio
async def test_duplicate_is_only_the_duplicate_database_sqlstate(tmp_path: Path) -> None:
    admin = SqliteAdmin(url_of(tmp_path / "a.db"))
    with pytest.raises(DatabaseError) as error:
        await admin.create(str(tmp_path / "nope" / "a.db"))
    assert not admin.duplicate(error.value)


@pytest.mark.asyncio
async def test_create_scratch_creates_the_named_file(tmp_path: Path) -> None:
    admin = SqliteAdmin(url_of(tmp_path / "a.db"))
    await admin.create_scratch(str(tmp_path / "scratch.db"), comment="tag")
    assert listing(tmp_path) == ["scratch.db"]


@pytest.mark.asyncio
async def test_drop_removes_the_database_and_its_wal_and_shm_files(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    # the connection stays open, so the WAL and shared-memory files exist while the file is dropped
    held = seed(path, wal=True)
    try:
        assert {"a.db-wal", "a.db-shm"} <= set(listing(tmp_path))
        (tmp_path / "a.db-journal").write_bytes(b"")
        (tmp_path / "a.db.keep").write_bytes(b"other")
        if sys.platform == "win32":
            held.close()
        await SqliteAdmin(url_of(path)).drop(force=True)
        assert listing(tmp_path) == ["a.db.keep"]
    finally:
        held.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot remove a file that is open")
@pytest.mark.asyncio
async def test_drop_removes_orphaned_side_files_when_the_main_file_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    held = seed(path, wal=True)
    try:
        held.execute("INSERT INTO t (v) VALUES ('d')")
        held.commit()
        assert present(tmp_path / "a.db-wal")
        remove(path)
        (tmp_path / "a.db-journal").write_bytes(b"x")
        admin = SqliteAdmin(url_of(path))
        await admin.drop()
        assert listing(tmp_path) == []
        await admin.create()
        async with await connect(ConnectOptions(path=str(path), mode="rw")) as connection:
            await connection.ping()
        assert fetch(path, "SELECT count(*) FROM sqlite_master") == [(0,)]
    finally:
        held.close()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to remove an open file")
@pytest.mark.asyncio
async def test_dropping_a_file_with_open_connections_is_a_clear_error_on_windows(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    held = seed(path)
    try:
        with pytest.raises(DatabaseError, match="open connections") as error:
            await SqliteAdmin(url_of(path)).drop()
        assert error.value.sqlstate == "55006"
        assert present(path)
    finally:
        held.close()


@pytest.mark.asyncio
async def test_drop_refuses_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"not a database, do not delete")
    admin = SqliteAdmin(url_of(notes))
    with pytest.raises(DatabaseError, match="not a SQLite database") as error:
        await admin.drop()
    assert error.value.sqlstate == "XX002"
    assert notes.read_bytes() == b"not a database, do not delete"
    with pytest.raises(DatabaseError, match="not a SQLite database"):
        await SqliteAdmin(url_of(tmp_path)).drop(str(tmp_path))
    assert present(tmp_path)


@pytest.mark.asyncio
async def test_ready_opens_an_existing_file_and_rejects_one_that_is_not_a_database(tmp_path: Path) -> None:
    good = tmp_path / "good.db"
    seed(good).close()
    await SqliteAdmin(url_of(good)).ready(1.0)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"x" * 4096)
    with pytest.raises(DatabaseError) as error:
        await SqliteAdmin(url_of(bad)).ready(1.0)
    assert error.value.code == "SQLITE_NOTADB"
    assert error.value.sqlstate == "XX002"


@pytest.mark.asyncio
async def test_list_names_files_with_the_prefix_beside_the_url_and_leaves_out_side_files(tmp_path: Path) -> None:
    seed(tmp_path / "rowx_b.db").close()
    (tmp_path / "rowx_a.db").write_bytes(b"")
    (tmp_path / "rowx_a.db-wal").write_bytes(b"x")
    (tmp_path / "rowx_a.db-shm").write_bytes(b"x")
    (tmp_path / "rowx_text.db").write_bytes(b"not sqlite")
    (tmp_path / "other.db").write_bytes(b"")
    admin = SqliteAdmin(url_of(tmp_path / "app.db"))
    found = await admin.list("rowx_")
    assert [info.name for info in found] == [str(tmp_path / "rowx_a.db"), str(tmp_path / "rowx_b.db")]
    assert all(info.comment is None and info.sessions == 0 for info in found)
    # a listed name is accepted by with_database, so a caller can drop it
    assert ConnectOptions.from_dsn(admin.with_database(found[0].name)).path == found[0].name
    await admin.drop(found[0].name)
    assert not present(tmp_path / "rowx_a.db")


@pytest.mark.asyncio
async def test_a_clone_goes_through_the_backup_api_and_keeps_rowids(tmp_path: Path) -> None:
    template = tmp_path / "template.db"
    source = seed(template)
    # a table without an INTEGER PRIMARY KEY: `VACUUM INTO` could renumber rowid 3 to 2
    source.execute("DELETE FROM t WHERE v = 'b'")
    source.commit()
    source.close()
    copy = tmp_path / "copy.db"
    await SqliteAdmin(url_of(copy)).create(template=str(template))
    assert fetch(copy, "SELECT rowid, v FROM t ORDER BY rowid") == [(1, "a"), (3, "c")]


@pytest.mark.asyncio
async def test_a_clone_includes_a_write_that_only_the_wal_holds(tmp_path: Path) -> None:
    template = tmp_path / "template.db"
    held = seed(template, wal=True)
    try:
        held.execute("INSERT INTO t (v) VALUES ('d')")
        held.commit()
        copy = tmp_path / "copy.db"
        await SqliteAdmin(url_of(copy)).create(template=str(template))
        assert fetch(copy, "SELECT count(*) FROM t") == [(4,)]
    finally:
        held.close()


@pytest.mark.asyncio
async def test_a_clone_of_a_missing_template_is_an_error_and_creates_nothing(tmp_path: Path) -> None:
    admin = SqliteAdmin(url_of(tmp_path / "copy.db"))
    with pytest.raises(DatabaseError, match="does not exist"):
        await admin.create(template=str(tmp_path / "missing.db"))
    assert listing(tmp_path) == []


@pytest.mark.asyncio
async def test_an_in_memory_admin_has_no_database_to_act_on() -> None:
    admin = SqliteAdmin("sqlite::memory:")
    for call in (admin.exists(), admin.create(), admin.drop(), admin.ready(1.0), admin.list("x")):
        with pytest.raises(InterfaceError, match="no name"):
            await call


@pytest.mark.asyncio
async def test_concurrent_creates_of_one_name_make_exactly_one(tmp_path: Path) -> None:
    admin = SqliteAdmin(url_of(tmp_path / "race.db"))
    results = await asyncio.gather(*(admin.create() for _ in range(8)), return_exceptions=True)
    failures = [result for result in results if isinstance(result, DatabaseError)]
    assert len(failures) == 7
    assert all(admin.duplicate(failure) for failure in failures)


def run(project: CheckProject, *args: str) -> tuple[int, str, str]:
    code = main(["database", *args])
    captured = project.capsys.readouterr()
    return code, captured.out, captured.err


def test_database_create_drop_setup_and_reset_work_on_a_file(project: CheckProject) -> None:
    target = project.root / "fresh.db"
    url = url_of(target)
    code, out, err = run(project, "create", "--database-url", url)
    assert (code, err) == (0, "")
    assert "Created database" in out
    assert target.exists()
    code, out, _ = run(project, "create", "--database-url", url)
    assert code == 0
    assert "already exists" in out

    code, out, err = run(project, "setup", "--database-url", url)
    assert (code, err) == (0, "")
    assert "Applied 1" in out
    change(target, "INSERT INTO users (name, age) VALUES ('ann', 3)")
    code, out, err = run(project, "reset", "-y", "--database-url", url)
    assert (code, err) == (0, "")
    assert fetch(target, "SELECT count(*) FROM users") == [(0,)]

    code, out, err = run(project, "drop", "-y", "--database-url", url)
    assert (code, err) == (0, "")
    assert "Dropped database" in out
    assert not target.exists()
    # dropping what is not there is not an error
    assert run(project, "drop", "-y", "--database-url", url)[0] == 0


def test_database_setup_creates_a_missing_file_and_migrates_it(project: CheckProject) -> None:
    target = project.root / "made.db"
    code, _, err = run(project, "setup", "--database-url", url_of(target))
    assert (code, err) == (0, "")
    assert fetch(target, "SELECT count(*) FROM users") == [(0,)]


def test_database_reset_validates_the_migrations_before_it_deletes_anything(project: CheckProject) -> None:
    target = project.root / "app.db"
    before = target.read_bytes()
    (project.root / "bad").mkdir()
    (project.root / "bad" / "1_x.sql").write_text("SELECT 1;", encoding="utf-8")
    (project.root / "bad" / "1_y.sql").write_text("SELECT 2;", encoding="utf-8")
    for source in ("missing", "bad"):
        code, _, err = run(project, "reset", "-y", "--source", source, "--database-url", project.url)
        assert code != 0
        assert err
        assert target.read_bytes() == before


def test_database_reset_refuses_a_file_that_is_not_a_database_and_keeps_it(project: CheckProject) -> None:
    notes = project.root / "notes.txt"
    notes.write_bytes(b"keep me")
    code, _, err = run(project, "reset", "-y", "--database-url", url_of(notes))
    assert code != 0
    # `ready` opens the file first and stops on the header that is not SQLite's
    assert "file is not a database" in err
    assert notes.read_bytes() == b"keep me"


def test_database_commands_reject_an_in_memory_url(project: CheckProject) -> None:
    for command in ("create", "drop", "reset", "setup"):
        extra = ("-y",) if command in {"drop", "reset"} else ()
        code, _, err = run(project, command, *extra, "--database-url", "sqlite::memory:")
        assert code != 0
        assert "not supported for in-memory URLs" in err
