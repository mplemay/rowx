import json
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Final

import pytest
from rowx_core.errors import MigrateError
from rowx_core.lifecycle import ORPHAN_AGE_SECONDS
from rowx_sqlite import ConnectOptions
from rowx_sqlite._scratch import PREFIX, SqliteScratch, scratch_url
from rowx_sqlite._services import SqliteMigrations, open_checker

from rowx.check.cli import main

if TYPE_CHECKING:
    from rowx_sqlite.__tests__.conftest import CheckProject


IS_ROOT: Final[bool] = hasattr(os, "geteuid") and os.geteuid() == 0


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


@pytest.mark.asyncio
async def test_the_scratch_is_a_migrated_temporary_file_that_every_worker_shares(
    scratch_dir: Path, migrations: Path
) -> None:
    service = SqliteMigrations("sqlite::memory:", migrations)
    scratch = SqliteScratch("sqlite::memory:")
    async with scratch.session(service, 4) as build:
        assert build.urls == (scratch.url,)
        assert scratch.url == scratch_url(scratch.path)
        parsed = ConnectOptions.from_dsn(scratch.url).path
        assert parsed is not None
        assert Path(parsed) == scratch.path
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
async def test_a_name_that_is_already_taken_is_an_error_and_the_file_is_kept(
    scratch_dir: Path, migrations: Path
) -> None:
    scratch = SqliteScratch("sqlite::memory:", sweep_previous=False)
    scratch.path.write_bytes(b"someone else's")
    with pytest.raises(FileExistsError):
        async with scratch.session(SqliteMigrations("sqlite::memory:", migrations), 1):
            pytest.fail("the session must not open")
    assert scratch.path.read_bytes() == b"someone else's"
    scratch.sweep()
    assert scratch.path.exists()
    assert names(scratch_dir) == [scratch.path.name]


def test_the_creation_itself_claims_the_file_for_the_sweep(scratch_dir: Path) -> None:
    scratch = SqliteScratch("sqlite::memory:")
    scratch._create()
    assert names(scratch_dir) == [scratch.path.name]
    scratch.sweep()
    assert names(scratch_dir) == []


HEADER = "from rowx import query\n\n"
UNUSED = "unused.db"


def check_json(project: CheckProject, *args: str, url: str | None = None) -> tuple[int, list[dict[str, Any]], str]:
    code, out, err = project.check("--output-format", "json", *args, url=url)
    found: list[dict[str, Any]] = json.loads(out) if out.strip() else []
    return code, found, err


VALID_SOURCE = """\
from rowx import query

query[tuple[int, str]](t"SELECT id, name FROM users")
"""
BROKEN_SOURCE = """\
from rowx import query

query[tuple[int]](t"SELECT nam FROM users")
"""


def write_queries(project: CheckProject, count: int, *, broken: bool = False) -> None:
    for number in range(count):
        project.write(VALID_SOURCE, f"q{number}.py")
    if broken:
        project.write(BROKEN_SOURCE, "broken.py")


def test_check_scratch_with_parallel_workers_leaves_no_files(
    project: CheckProject, scratch_dir: Path, tmp_path: Path
) -> None:
    write_queries(project, 12, broken=True)
    # the target file is never read: the scratch is built from the migrations
    empty = tmp_path / UNUSED
    empty.write_bytes(b"")
    code, found, err = check_json(project, "--scratch", "-j", "4", url=f"sqlite:///{empty}")
    assert not err
    assert code == 1
    assert [item["file"] for item in found] == ["broken.py"]
    assert found[0]["code"] == "SQLITE_ERROR"
    assert names(scratch_dir) == []
    assert empty.read_bytes() == b""


def test_check_scratch_with_parallel_workers_passes_when_every_query_is_valid(
    project: CheckProject, scratch_dir: Path, tmp_path: Path
) -> None:
    write_queries(project, 12)
    empty = tmp_path / UNUSED
    empty.write_bytes(b"")
    code, found, err = check_json(project, "--scratch", "-j", "6", url=f"sqlite:///{empty}")
    assert (code, found, err) == (0, [], "")
    assert names(scratch_dir) == []


def test_an_interrupted_teardown_is_finished_by_the_sweep(
    project: CheckProject, scratch_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_queries(project, 3)
    removals: list[Path] = []
    survived: list[bool] = []
    real_remove = SqliteScratch._remove

    # the first teardown never runs, as if the event loop had been torn down under it
    def interrupted(self: SqliteScratch) -> None:
        removals.append(self.path)
        if len(removals) == 1:
            survived.append(self.path.exists())
        if len(removals) > 1:
            real_remove(self)

    monkeypatch.setattr(SqliteScratch, "_remove", interrupted)
    empty = tmp_path / UNUSED
    empty.write_bytes(b"")
    code, found, err = check_json(project, "--scratch", "-j", "2", url=f"sqlite:///{empty}")
    assert (code, found, err) == (0, [], "")
    # the sweep that the CLI runs from outside the loop is what removed the file
    assert len(removals) == 2
    assert survived == [True]
    assert not removals[0].exists()
    assert names(scratch_dir) == []


@pytest.mark.skipif(IS_ROOT, reason="chmod does not restrict root")
def test_the_drift_gate_reads_a_read_only_file_that_is_up_to_date(project: CheckProject) -> None:
    database = project.root / "app.db"
    database.chmod(0o444)
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    project.write(VALID_SOURCE)
    code, found, err = check_json(project)
    assert (code, found, err) == (0, [], "")
    # the checker never wrote to it
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


@pytest.mark.skipif(IS_ROOT, reason="chmod does not restrict root")
def test_the_drift_gate_reports_a_read_only_file_that_has_drifted(project: CheckProject) -> None:
    database = project.root / "app.db"
    before = database.read_bytes()
    database.chmod(0o444)
    (project.root / "migrations" / "2_more.sql").write_text("CREATE TABLE extra (id INTEGER);", encoding="utf-8")
    project.write(VALID_SOURCE)
    code, out, err = project.check()
    assert code != 0
    assert "database has drifted" in out + err
    assert "1 pending: 2" in out + err
    assert database.read_bytes() == before
    # the scratch run needs no drift gate and passes against the same file
    code, _, err = project.check("--scratch")
    assert (code, err) == (0, "")


def git(root: Path, *args: str) -> str:
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        ["git", *args],  # ruff: ignore[start-process-with-partial-path]
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(root: Path, source: str, migration: str) -> str:
    (root / "q.py").write_text(source, encoding="utf-8")
    (root / "migrations" / "1_schema.sql").write_text(migration, encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


COMPAT_CASES = [
    (
        "CREATE TABLE t (old INTEGER NOT NULL)",
        "CREATE TABLE t (new INTEGER NOT NULL)",
        "query[int](t'SELECT old FROM t')",
        "query[int](t'SELECT new FROM t')",
        1,
        "migrate first",
    ),
    (
        "CREATE TABLE t (x INTEGER NOT NULL)",
        "CREATE TABLE t (x INTEGER NOT NULL)",
        "query[int](t'SELECT x FROM t')",
        "query[int](t'SELECT x FROM t')",
        0,
        "compatible checked subset",
    ),
]


@pytest.mark.parametrize(
    ("base_sql", "head_sql", "base_source", "head_source", "status", "verdict"),
    COMPAT_CASES,
    ids=["a renamed column is incompatible", "an unchanged schema is compatible"],
)
def test_check_compat_builds_one_scratch_per_revision_and_leaves_no_files(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    tmp_path: Path,
    scratch_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    base_sql: str,
    head_sql: str,
    base_source: str,
    head_source: str,
    status: int,
    verdict: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (repo / "migrations").mkdir()
    base = commit(repo, HEADER + base_source + "\n", base_sql)
    head = commit(repo, HEADER + head_source + "\n", head_sql)
    monkeypatch.chdir(repo)
    # the target database is never read or written: each revision gets its own scratch file
    target = tmp_path / UNUSED
    target.write_bytes(b"")
    code = main(["check-compat", "--against", base, "--head", head, "--database-url", f"sqlite:///{target}", "-j", "2"])
    out = capsys.readouterr().out
    assert code == status, out
    assert verdict in out
    assert base in out
    assert head in out
    assert names(scratch_dir) == []
    assert target.read_bytes() == b""
