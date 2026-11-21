import re
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Final

import pytest
from rowx_core.errors import (
    DatabaseError,
    DirtyMigrationError,
    InterfaceError,
    IrreversibleMigrationError,
    MigrationChecksumError,
    MigrationFailedError,
    MigrationMissingError,
    MigrationOrderError,
)
from rowx_sqlite import Connection, ConnectOptions, Migrator, Pool, connect

from rowx import query, query_unchecked
from rowx.check.cli import main

if TYPE_CHECKING:
    from pathlib import Path

ELAPSED: Final[re.Pattern[str]] = re.compile(r"\(\d+\.\d(?:us|ms|s)\)")


def write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def reversible(directory: Path, version: int, table: str) -> None:
    write(directory, f"{version}_{table}.up.sql", f"CREATE TABLE {table} (id INTEGER);")
    write(directory, f"{version}_{table}.down.sql", f"DROP TABLE {table};")


async def table_exists(conn: Connection | Pool, name: str) -> bool:
    return await query[bool](t"SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = {name})").one(
        conn,
    )


async def history(conn: Connection | Pool) -> list[tuple[int, bool]]:
    return await query_unchecked[tuple[int, bool]](
        t"SELECT version, success FROM _rowx_migrations ORDER BY version",
    ).all(conn)


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    return tmp_path / "migrations"


@pytest.mark.asyncio
async def test_run_on_a_connection_applies_in_version_order(conn: Connection, migrations: Path) -> None:
    write(migrations, "2_second.sql", "CREATE TABLE second (id INTEGER);")
    write(migrations, "1_first.sql", "CREATE TABLE first (id INTEGER);")
    migrator = Migrator(migrations)

    applied = await migrator.run(conn)
    assert [(r.migration.version, r.migration.description) for r in applied] == [(1, "first"), (2, "second")]
    assert all(r.elapsed is not None and r.elapsed.total_seconds() > 0 for r in applied)
    assert await table_exists(conn, "first")
    assert await table_exists(conn, "second")
    assert await history(conn) == [(1, True), (2, True)]
    assert await migrator.run(conn) == ()
    # the connection is out of every transaction
    await query(t"CREATE TABLE after (id INTEGER)").execute(conn)
    async with conn.transaction():
        pass


@pytest.mark.asyncio
async def test_run_stops_at_a_target(conn: Connection, migrations: Path) -> None:
    for version in (1, 2, 3):
        write(migrations, f"{version}_t{version}.sql", f"CREATE TABLE t{version} (id INTEGER);")
    migrator = Migrator(migrations)

    assert [r.migration.version for r in await migrator.run(conn, target=2)] == [1, 2]
    assert not await table_exists(conn, "t3")
    assert [r.migration.version for r in await migrator.run(conn)] == [3]
    with pytest.raises(ValueError, match="target migration 9 does not exist"):
        await migrator.run(conn, target=9)


@pytest.mark.asyncio
async def test_run_on_a_pool_releases_the_connection(tmp_path: Path, migrations: Path) -> None:
    write(migrations, "1_first.sql", "CREATE TABLE first (id INTEGER);")
    migrator = Migrator(migrations)
    options = ConnectOptions(path=str(tmp_path / "pool.db"), mode="rwc")

    async with Pool(options, max_size=1, acquire_timeout=10) as pool:
        applied = await migrator.run(pool)
        assert [r.migration.version for r in applied] == [1]
        # with a single connection, this only succeeds when the migrator gave it back
        assert await table_exists(pool, "first")
        assert pool.size == 1
        assert [s.state for s in await migrator.info(pool)] == ["applied"]


@pytest.mark.asyncio
async def test_run_on_a_pool_with_hooks_runs_them(tmp_path: Path, migrations: Path) -> None:
    write(migrations, "1_first.sql", "CREATE TABLE first (id INTEGER);")
    migrator = Migrator(migrations)
    options = ConnectOptions(path=str(tmp_path / "pool.db"), mode="rwc")
    seen: list[str] = []

    async def setup(hooked: Connection) -> None:
        seen.append("setup")
        assert await query[int](t"SELECT 1").one(hooked) == 1

    async with Pool(options, max_size=1, acquire_timeout=10, setup=setup) as pool:
        assert [r.migration.version for r in await migrator.run(pool)] == [1]
        assert seen == ["setup"]
        assert await table_exists(pool, "first")


@pytest.mark.asyncio
async def test_a_migrator_is_rejected_on_a_transaction(conn: Connection, migrations: Path) -> None:
    write(migrations, "1_first.sql", "CREATE TABLE first (id INTEGER);")
    migrator = Migrator(migrations)

    async with conn.transaction() as tx:
        with pytest.raises(InterfaceError, match="not a Transaction"):
            await migrator.run(tx)  # ty: ignore[invalid-argument-type]
        with pytest.raises(InterfaceError, match="inside a transaction"):
            await migrator.run(conn)
    with pytest.raises(TypeError, match="Connection or a Pool"):
        await migrator.run(object())  # ty: ignore[invalid-argument-type]
    assert not await table_exists(conn, "first")


def test_create_schemas_are_rejected(migrations: Path) -> None:
    write(migrations, "1_first.sql", "SELECT 1;")
    with pytest.raises(ValueError, match="create_schemas"):
        Migrator(migrations, create_schemas=["extra"])
    with pytest.raises(TypeError, match="not a string"):
        Migrator(migrations, create_schemas="extra")


def test_a_table_in_a_schema_other_than_main_is_rejected(migrations: Path) -> None:
    write(migrations, "1_first.sql", "SELECT 1;")
    with pytest.raises(ValueError, match="no schemas besides `main`"):
        Migrator(migrations, table="other.history")
    with pytest.raises(ValueError, match="not a valid identifier"):
        Migrator(migrations, table="not valid")


@pytest.mark.asyncio
async def test_a_history_table_named_in_main_is_used(conn: Connection, migrations: Path) -> None:
    write(migrations, "1_first.sql", "CREATE TABLE first (id INTEGER);")
    migrator = Migrator(migrations, table="main.applied")

    assert [r.migration.version for r in await migrator.run(conn)] == [1]
    assert await table_exists(conn, "applied")
    assert not await table_exists(conn, "_rowx_migrations")
    assert [s.state for s in await migrator.info(conn)] == ["applied"]


@pytest.mark.asyncio
async def test_revert_reverts_newest_first_to_a_target_and_everything(conn: Connection, migrations: Path) -> None:
    for version in (1, 2, 3):
        reversible(migrations, version, f"t{version}")
    migrator = Migrator(migrations)

    await migrator.run(conn)
    # without a target only the latest is reverted
    (latest,) = await migrator.revert(conn)
    assert latest.migration.version == 3
    assert latest.elapsed is not None
    await migrator.run(conn)
    reverted = await migrator.revert(conn, target=1)
    assert [r.migration.version for r in reverted] == [3, 2]
    assert await table_exists(conn, "t1")
    assert not await table_exists(conn, "t2")
    assert await history(conn) == [(1, True)]
    assert [r.migration.version for r in await migrator.revert(conn, all=True)] == [1]
    assert await migrator.revert(conn, all=True) == ()
    # reverted migrations apply again
    assert [r.migration.version for r in await migrator.run(conn)] == [1, 2, 3]
    with pytest.raises(ValueError, match="either target or all"):
        await migrator.revert(conn, target=1, all=True)


@pytest.mark.asyncio
async def test_revert_without_a_down_file_raises_before_reverting_anything(conn: Connection, migrations: Path) -> None:
    reversible(migrations, 1, "t1")
    write(migrations, "2_t2.sql", "CREATE TABLE t2 (id INTEGER);")
    migrator = Migrator(migrations)

    await migrator.run(conn)
    with pytest.raises(IrreversibleMigrationError) as exc:
        await migrator.revert(conn, all=True)
    assert exc.value.version == 2
    assert await history(conn) == [(1, True), (2, True)]
    assert await table_exists(conn, "t1")


@pytest.mark.asyncio
async def test_skip_records_migrations_without_running_them(conn: Connection, migrations: Path) -> None:
    for version in (1, 2, 3):
        write(migrations, f"{version}_t{version}.sql", f"CREATE TABLE t{version} (id INTEGER);")
    migrator = Migrator(migrations)

    skipped = await migrator.skip(conn, target=2)
    assert [r.migration.version for r in skipped] == [1, 2]
    assert all(r.elapsed is None for r in skipped)
    assert [s.state for s in await migrator.info(conn)] == ["applied", "applied", "pending"]
    assert not await table_exists(conn, "t1")
    assert [r.migration.version for r in await migrator.run(conn)] == [3]
    assert not await table_exists(conn, "t2")


@pytest.mark.asyncio
async def test_a_dry_run_returns_the_planned_statuses_and_changes_nothing(conn: Connection, migrations: Path) -> None:
    for version in (1, 2, 3):
        reversible(migrations, version, f"t{version}")
    migrator = Migrator(migrations)

    planned = await migrator.run(conn, target=2, dry_run=True)
    assert [(r.migration.version, r.migration.description) for r in planned] == [(1, "t1"), (2, "t2")]
    assert all(r.elapsed is None for r in planned)
    assert [r.migration.version for r in await migrator.skip(conn, dry_run=True)] == [1, 2, 3]
    assert await migrator.revert(conn, all=True, dry_run=True) == ()
    # nothing was created, not even the history table
    assert not await table_exists(conn, "_rowx_migrations")
    assert not await table_exists(conn, "t1")

    await migrator.run(conn, target=2)
    assert [r.migration.version for r in await migrator.run(conn, dry_run=True)] == [3]
    assert [r.migration.version for r in await migrator.revert(conn, all=True, dry_run=True)] == [2, 1]
    assert [r.migration.version for r in await migrator.revert(conn, dry_run=True)] == [2]
    assert await history(conn) == [(1, True), (2, True)]
    assert await table_exists(conn, "t2")
    with pytest.raises(MigrationOrderError):
        await migrator.run(conn, target=1, dry_run=True)


@pytest.mark.asyncio
async def test_info_reports_every_state_and_creates_nothing(conn: Connection, migrations: Path) -> None:
    write(migrations, "1_one.sql", "CREATE TABLE t1 (id INTEGER);")
    write(migrations, "2_two.sql", "CREATE TABLE t2 (id INTEGER);")
    migrator = Migrator(migrations)

    before = await migrator.info(conn)
    assert [(s.version, s.state, s.installed_on) for s in before] == [(1, "pending", None), (2, "pending", None)]
    assert not await table_exists(conn, "_rowx_migrations")

    await migrator.run(conn)
    write(migrations, "3_three.sql", "-- no-transaction\nINSERT INTO missing VALUES (1);")
    with pytest.raises(MigrationFailedError):
        await Migrator(migrations).run(conn)
    write(migrations, "2_two.sql", "CREATE TABLE t2 (id BIGINT);")
    write(migrations, "4_four.sql", "SELECT 1;")
    await query(
        t"INSERT INTO _rowx_migrations (version, description, success, checksum, execution_time) VALUES (5, 'gone', 1, {b'x'}, 1)",
    ).execute(conn)

    states = {s.version: s for s in await Migrator(migrations).info(conn)}
    assert {v: s.state for v, s in states.items()} == {
        1: "applied",
        2: "modified",
        3: "dirty",
        4: "pending",
        5: "missing",
    }
    assert states[1].installed_on is not None
    assert states[1].execution_time is not None
    assert states[1].kind == "simple"
    assert states[1].local_checksum == states[1].applied_checksum
    assert states[2].local_checksum != states[2].applied_checksum
    assert states[4].applied_checksum is None
    assert (states[5].kind, states[5].local_checksum, states[5].applied_checksum) == (None, None, b"x")
    assert states[3].description == "three"


@pytest.mark.asyncio
async def test_plan_errors_are_the_shared_exceptions(conn: Connection, migrations: Path) -> None:
    for version in (1, 2, 3):
        write(migrations, f"{version}_t{version}.sql", f"CREATE TABLE t{version} (id INTEGER);")
    migrator = Migrator(migrations)
    await Migrator(migrations).run(conn, target=1)
    await migrator.skip(conn, target=3)

    write(migrations, "2_t2.sql", "CREATE TABLE t2 (id BIGINT);")
    with pytest.raises(MigrationChecksumError) as checksum:
        await Migrator(migrations).run(conn)
    assert checksum.value.version == 2
    (migrations / "3_t3.sql").unlink()
    write(migrations, "2_t2.sql", "CREATE TABLE t2 (id INTEGER);")
    with pytest.raises(MigrationMissingError) as missing:
        await Migrator(migrations).run(conn)
    assert missing.value.versions == (3,)


@pytest.mark.asyncio
async def test_a_failing_file_raises_with_its_version_path_and_cause(conn: Connection, migrations: Path) -> None:
    write(migrations, "1_ok.sql", "CREATE TABLE ok (id INTEGER);")
    write(migrations, "2_bad.sql", "CREATE TABLE half (id INTEGER);\nINSERT INTO missing VALUES (1);")
    migrator = Migrator(migrations)

    with pytest.raises(MigrationFailedError) as exc:
        await migrator.run(conn)
    assert exc.value.version == 2
    assert isinstance(exc.value.__cause__, DatabaseError)
    assert exc.value.error is exc.value.__cause__
    assert exc.value.error.sqlstate == "HY000"
    assert exc.value.error.code == "SQLITE_ERROR"
    assert "no such table: missing" in exc.value.error.message
    assert str(exc.value).startswith("migration 2 failed: ")
    assert exc.value.path == migrations / "2_bad.sql"
    assert exc.value.dirty is False
    # what completed before the failure
    assert [(r.migration.version, r.elapsed is not None) for r in exc.value.applied] == [(1, True)]
    # a transactional file leaves nothing behind, and the earlier migration stays
    assert await history(conn) == [(1, True)]
    assert not await table_exists(conn, "half")
    # the connection is still usable
    assert await query[int](t"SELECT 1").one(conn) == 1


@pytest.mark.asyncio
async def test_a_no_transaction_file_can_vacuum(conn: Connection, migrations: Path) -> None:
    write(migrations, "1_t.sql", "CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1), (2);")
    write(migrations, "2_vacuum.sql", "-- no-transaction\nVACUUM;")

    await Migrator(migrations).run(conn)

    assert await history(conn) == [(1, True), (2, True)]


@pytest.mark.asyncio
async def test_a_failed_no_transaction_file_leaves_a_dirty_row(conn: Connection, migrations: Path) -> None:
    write(
        migrations, "1_bad.sql", "-- no-transaction\nCREATE TABLE half (id INTEGER);\nINSERT INTO missing VALUES (1);"
    )
    migrator = Migrator(migrations)

    with pytest.raises(MigrationFailedError) as failed:
        await migrator.run(conn)
    assert failed.value.version == 1
    assert failed.value.dirty is True
    assert failed.value.applied == ()
    assert await history(conn) == [(1, False)]
    # outside a transaction, what ran before the failure stays
    assert await table_exists(conn, "half")
    with pytest.raises(DirtyMigrationError) as dirty:
        await migrator.run(conn)
    assert dirty.value.version == 1
    assert [s.state for s in await migrator.info(conn)] == ["dirty"]


@pytest.mark.asyncio
async def test_a_script_with_its_own_transaction_statements_is_rejected(conn: Connection, migrations: Path) -> None:
    write(migrations, "1_own.sql", "CREATE TABLE early (id INTEGER);\nBEGIN;\nCREATE TABLE late (id INTEGER);\nCOMMIT;")

    with pytest.raises(MigrationFailedError) as exc:
        await Migrator(migrations).run(conn)
    assert exc.value.error.code == "SQLITE_AUTH"
    assert "must not contain BEGIN, COMMIT or ROLLBACK" in exc.value.error.message
    assert exc.value.dirty is False
    assert not await table_exists(conn, "early")
    assert await history(conn) == []


@pytest.mark.asyncio
async def test_a_no_transaction_file_can_rebuild_a_table_with_foreign_keys_off(
    conn: Connection, migrations: Path
) -> None:
    write(
        migrations,
        "1_tables.sql",
        "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);\n"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent (id) ON DELETE CASCADE);\n"
        "INSERT INTO parent VALUES (1, 'a'), (2, 'b');\n"
        "INSERT INTO child VALUES (10, 1), (20, 2);",
    )
    write(
        migrations,
        "2_rebuild.sql",
        "-- no-transaction\n"
        "PRAGMA foreign_keys = OFF;\n"
        "CREATE TABLE parent_new (id INTEGER PRIMARY KEY, name TEXT, extra TEXT);\n"
        "INSERT INTO parent_new (id, name) SELECT id, name FROM parent;\n"
        "DROP TABLE parent;\n"
        "ALTER TABLE parent_new RENAME TO parent;\n"
        "PRAGMA foreign_keys = ON;",
    )

    assert [r.migration.version for r in await Migrator(migrations).run(conn)] == [1, 2]
    assert await query[int](t"SELECT count(*) FROM child").one(conn) == 2
    assert await query[int](t"PRAGMA foreign_keys").one(conn) == 1


@pytest.mark.asyncio
async def test_a_migrator_that_cannot_get_the_lock_fails_after_the_busy_timeout(
    tmp_path: Path, migrations: Path
) -> None:
    write(migrations, "1_first.sql", "CREATE TABLE first (id INTEGER);")
    path = tmp_path / "busy.db"
    migrator = Migrator(migrations)
    async with await connect(ConnectOptions(path=str(path), mode="rwc", connect_timeout=0.05)) as conn:
        # the first migration puts the file in WAL mode, where a writer holds the lock
        assert [r.migration.version for r in await migrator.run(conn)] == [1]
        write(migrations, "2_second.sql", "CREATE TABLE second (id INTEGER);")
        migrator = Migrator(migrations)
        # another connection holds the write lock (a second SQLite library in this process would not conflict,
        # because POSIX locks belong to the process)
        async with await connect(ConnectOptions(path=str(path))) as holder, holder.transaction():
            with pytest.raises(DatabaseError, match="another migrator holds the database") as exc:
                await migrator.run(conn)
            assert exc.value.sqlstate == "55P03"
            assert exc.value.code == "SQLITE_BUSY"
        # the lock is free again, and the same connection carries on
        assert [r.migration.version for r in await migrator.run(conn)] == [2]


def cli(capsys: pytest.CaptureFixture[str], dsn: str, directory: Path, *argv: str) -> tuple[int, str, str]:
    code = main(["migrate", *argv, "--database-url", dsn, "--source", str(directory)])
    captured = capsys.readouterr()
    return code, ELAPSED.sub("(<time>)", captured.out), captured.err


def has_table(path: Path, name: str) -> bool:
    with closing(sqlite3.connect(path)) as db:
        return (
            db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None
        )


def applied_versions(path: Path) -> list[int]:
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT version FROM _rowx_migrations ORDER BY version")]


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "cli.db"
    # a database file is made by the user (or by `rowx database create`); migrating opens an existing one
    sqlite3.connect(path).close()
    return path, f"sqlite:///{path}"


def test_cli_run_info_revert_info(
    database: tuple[Path, str], migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, dsn = database
    write(migrations, "1_create_cli_a.sql", "CREATE TABLE cli_a (id INTEGER);")
    write(migrations, "2_create_cli_b.up.sql", "CREATE TABLE cli_b (id INTEGER);")
    write(migrations, "2_create_cli_b.down.sql", "DROP TABLE cli_b;")

    assert cli(capsys, dsn, migrations, "info") == (0, "1/pending create cli a\n2/pending create cli b\n", "")
    assert not has_table(path, "_rowx_migrations")

    assert cli(capsys, dsn, migrations, "run", "--dry-run") == (
        0,
        "Would apply 1/migrate create cli a\nWould apply 2/migrate create cli b\n",
        "",
    )
    assert not has_table(path, "cli_a")

    assert cli(capsys, dsn, migrations, "run") == (
        0,
        "Applied 1/migrate create cli a (<time>)\nApplied 2/migrate create cli b (<time>)\n",
        "",
    )
    assert has_table(path, "cli_a")
    assert has_table(path, "cli_b")
    assert cli(capsys, dsn, migrations, "info") == (
        0,
        "1/applied create cli a (<time>)\n2/applied create cli b (<time>)\n",
        "",
    )

    # without a target, only the latest migration is reverted
    assert cli(capsys, dsn, migrations, "revert") == (0, "Reverted 2/revert create cli b (<time>)\n", "")
    assert has_table(path, "cli_a")
    assert not has_table(path, "cli_b")
    assert applied_versions(path) == [1]

    assert cli(capsys, dsn, migrations, "run") == (0, "Applied 2/migrate create cli b (<time>)\n", "")
    assert cli(capsys, dsn, migrations, "run") == (0, "", "")
    assert cli(capsys, dsn, migrations, "skip", "--dry-run") == (0, "", "")


def test_cli_reports_a_failing_file_and_a_plan_error(
    database: tuple[Path, str], migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, dsn = database
    write(migrations, "1_bad.sql", "INSERT INTO missing VALUES (1);")

    code, out, err = cli(capsys, dsn, migrations, "run")
    assert (code, out) == (2, "")
    assert err.startswith("error: migration 1 failed: ")
    assert "no such table: missing" in err
    assert applied_versions(path) == []

    write(migrations, "1_bad.sql", "SELECT 1;")
    assert cli(capsys, dsn, migrations, "run")[0] == 0
    write(migrations, "1_bad.sql", "SELECT 2;")
    code, out, err = cli(capsys, dsn, migrations, "run")
    assert (code, out) == (2, "")
    assert err == "error: migration 1 was modified after it was applied\n"


def test_cli_rejects_create_schemas_in_the_config(
    database: tuple[Path, str], migrations: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, dsn = database
    write(migrations, "1_first.sql", "SELECT 1;")
    (tmp_path / "pyproject.toml").write_text('[tool.rowx.migrate]\ncreate_schemas = ["extra"]\n', encoding="utf-8")

    code, out, err = cli(capsys, dsn, migrations, "info")
    assert (code, out) == (2, "")
    assert "create_schemas" in err


@pytest.mark.parametrize("command", ["run", "info", "revert", "skip"])
def test_cli_rejects_an_in_memory_database(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert cli(capsys, "sqlite::memory:", tmp_path / "migrations", command) == (
        2,
        "",
        "error: an in-memory database is gone when the command exits; use a file database URL\n",
    )
