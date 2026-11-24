import asyncio
import os
import secrets
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Final, override

import pytest
from rowx_core.errors import InterfaceError
from rowx_sqlite import ConnectOptions, Migrator, _testing
from rowx_sqlite._core import sqlite_version
from rowx_sqlite._services import SqliteMigrations
from rowx_sqlite._testing import (
    CACHE_NAME,
    HOLD_SUFFIX,
    TMP_MARKER,
    SqliteProvisioner,
    provisioner,
    release_hold,
    take_hold,
    try_lock,
    unlock,
)

from rowx.testing import TEMPLATE_PREFIX, TEST_PREFIX, project_prefix, template_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from rowx_core.backend import Provisioner
    from rowx_core.migrations import MigrationResult

pytest_plugins = ["pytester"]

# `-p rowx.testing` is how a project opts in without a conftest
PLUGIN: Final[tuple[str, ...]] = ("-p", "rowx.testing")
IDENTITY: Final[tuple[str, ...]] = (f"sqlite:{sqlite_version()}",)
HEX_LENGTH: Final[int] = 16

INNER: Final[str] = """
import asyncio
from pathlib import Path

from rowx import query_unchecked


def record(url):
    with Path("urls.txt").open("a") as handle:
        handle.write(url + "\\n")


def insert(conn):
    asyncio.run(query_unchecked[tuple[()]](t"INSERT INTO items (id) VALUES (1)").execute(conn))


def count(conn):
    return asyncio.run(query_unchecked[tuple[int]](t"SELECT count(*) FROM items").one(conn))[0]


def test_first(rowx_database_url, rowx_connection):
    record(rowx_database_url)
    insert(rowx_connection)
    assert count(rowx_connection) == 1


def test_second(rowx_database_url, rowx_connection):
    record(rowx_database_url)
    insert(rowx_connection)
    assert count(rowx_connection) == 1
"""

# lists the tables the clone holds, so that a run shows the schema it was given
TABLES_INNER: Final[str] = """
import asyncio
from pathlib import Path

from rowx import query_unchecked


def test_tables(rowx_connection):
    sql = t"SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    rows = asyncio.run(query_unchecked[tuple[str]](sql).all(rowx_connection))
    Path("tables.txt").write_text(" ".join(name for (name,) in rows))
"""

XDIST_INNER: Final[str] = """
import pytest

from rowx import query_unchecked


@pytest.mark.parametrize("number", range(8))
def test_worker(rowx_database_url, rowx_connection, number):
    import asyncio

    asyncio.run(query_unchecked[tuple[()]](t"INSERT INTO items (id) VALUES (1)").execute(rowx_connection))
    (found,) = asyncio.run(query_unchecked[tuple[int]](t"SELECT count(*) FROM items").one(rowx_connection))
    assert found == 1
"""

# what the session holds in its temporary directory while it runs
TEMPORARY_INNER: Final[str] = """
import tempfile
from pathlib import Path


def test_temporary(rowx_connection):
    found = sorted(path.relative_to(tempfile.gettempdir()).as_posix() for path in Path(tempfile.gettempdir()).rglob("*"))
    Path("seen.txt").write_text("\\n".join(found))
"""

EMPTY_INNER: Final[str] = """
import asyncio

from rowx import query_unchecked


def test_empty(rowx_connection):
    sql = t"SELECT count(*) FROM sqlite_master"
    assert asyncio.run(query_unchecked[tuple[int]](sql).one(rowx_connection))[0] == 0
"""


POOL_INNER: Final[str] = """
import asyncio

from rowx import query_unchecked


def test_pool(rowx_pool):
    asyncio.run(query_unchecked[tuple[()]](t"INSERT INTO items (id) VALUES (1)").execute(rowx_pool))
    (found,) = asyncio.run(query_unchecked[tuple[int]](t"SELECT count(*) FROM items").one(rowx_pool))
    assert found == 1
"""


class CountingMigrations(SqliteMigrations):
    runs = 0

    @override
    async def run_on(self, dsn: str) -> tuple[MigrationResult, ...]:
        self.runs += 1
        return await super().run_on(dsn)


class Project:
    def __init__(self, pytester: pytest.Pytester) -> None:
        self.pytester = pytester
        self.migrations = pytester.path / "migrations"
        self.migrations.mkdir()
        self.write_migration(1, "CREATE TABLE items (id INTEGER PRIMARY KEY);")
        pytester.makepyprojecttoml('[tool.pytest.ini_options]\n\n[tool.rowx.migrate]\ndir = "migrations"\n')
        pytester.makepyfile(test_inner=INNER)

    def write_migration(self, version: int, sql: str) -> None:
        (self.migrations / f"{version}_m{version}.sql").write_text(sql, encoding="utf-8")

    @property
    def url(self) -> str:
        return os.environ["DATABASE_URL"]

    @property
    def cache(self) -> Path:
        return self.pytester.path / ".pytest_cache" / "d" / CACHE_NAME

    def template(self) -> str:
        return template_name(Migrator(self.migrations), directory=self.migrations, identity=IDENTITY)

    @property
    def prefix(self) -> str:
        return project_prefix(self.migrations)

    def run(self, *args: str) -> pytest.RunResult:
        return self.pytester.runpytest_subprocess(*PLUGIN, *args)

    def templates(self) -> list[str]:
        return sorted(path.name for path in self.cache.glob(f"{TEMPLATE_PREFIX}*.db") if TMP_MARKER not in path.name)

    def leftovers(self) -> list[str]:
        if not self.cache.exists():
            return []
        return sorted(
            path.name for path in self.cache.iterdir() if TMP_MARKER in path.name or path.name.endswith(HOLD_SUFFIX)
        )

    def urls(self) -> list[str]:
        path = self.pytester.path / "urls.txt"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def provisioner(self) -> Provisioner:
        return provisioner(self.url, cache=directory_in(self.cache.parent))


@pytest.fixture
def project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Project:
    # the plugin reads the URL from the environment, and only to pick the backend: the file is never opened
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{pytester.path / 'selector.db'}")
    return Project(pytester)


# what the runner's cache does: the directory is made on request
def directory_in(root: Path) -> Callable[[str], Path]:
    def make(name: str) -> Path:
        (path := root / name).mkdir(parents=True, exist_ok=True)
        return path

    return make


def database_path(url: str) -> Path:
    path = ConnectOptions.from_dsn(url).path
    assert path is not None
    return Path(path)


def test_each_test_gets_its_own_database(project: Project) -> None:
    result = project.run()
    result.assert_outcomes(passed=2)
    first, second = project.urls()
    assert first != second
    assert Path(database_path(first)).name.startswith(TEST_PREFIX + project.prefix + "_")
    # no clone outlives the session
    assert not database_path(first).exists()
    assert not database_path(second).exists()
    assert project.templates() == [f"{project.template()}.db"]
    assert project.leftovers() == []
    # the URL only selected the backend
    assert not (project.pytester.path / "selector.db").exists()
    # the template is left in the rollback journal: one file, no WAL side files
    template = project.cache / project.templates()[0]
    header = template.read_bytes()[:20]
    assert header[18:20] == bytes([1, 1])
    assert not Path(f"{template}-wal").exists()
    assert not Path(f"{template}-shm").exists()


def test_the_template_is_reused_across_sessions(project: Project) -> None:
    project.run().assert_outcomes(passed=2)
    (template,) = (project.cache / name for name in project.templates())
    before = template.stat()
    project.run().assert_outcomes(passed=2)
    after = template.stat()
    assert project.templates() == [template.name]
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert project.leftovers() == []


@pytest.mark.parametrize("keep", [False, True])
def test_a_changed_migration_builds_a_new_template(project: Project, *, keep: bool) -> None:
    project.pytester.makepyfile(test_tables=TABLES_INNER)
    project.run("test_tables.py").assert_outcomes(passed=1)
    old = project.templates()
    assert old == [f"{project.template()}.db"]
    project.write_migration(2, "CREATE TABLE extra (id INTEGER PRIMARY KEY);")
    project.run("test_tables.py", *(["--rowx-keep-templates"] if keep else [])).assert_outcomes(passed=1)
    new = f"{project.template()}.db"
    assert new not in old
    assert "extra" in (project.pytester.path / "tables.txt").read_text(encoding="utf-8").split()
    expected = sorted([*old, new]) if keep else [new]
    assert project.templates() == expected
    assert project.leftovers() == []


def test_xdist_workers_share_one_template(project: Project) -> None:
    project.pytester.makepyfile(test_inner=XDIST_INNER)
    result = project.run("-n", "3")
    result.assert_outcomes(passed=8)
    assert project.templates() == [f"{project.template()}.db"]
    assert project.leftovers() == []


def test_a_held_template_is_not_dropped_by_another_session(project: Project) -> None:
    project.run().assert_outcomes(passed=2)
    (old,) = project.templates()
    service = SqliteMigrations(project.url, project.migrations)
    held = project.provisioner()
    # this process holds the template, as a session that is still running would
    hold = asyncio.run(held.ensure_template(service, old.removesuffix(".db"), keep_stale=False, project=project.prefix))
    project.write_migration(2, "CREATE TABLE extra (id INTEGER PRIMARY KEY);")
    new = f"{project.template()}.db"
    try:
        project.run().assert_outcomes(passed=2)
        assert project.templates() == sorted([old, new])
    finally:
        asyncio.run(hold.close())
    assert project.leftovers() == []
    # nothing holds it now, so the next session drops it
    project.run().assert_outcomes(passed=2)
    assert project.templates() == [new]


def test_a_dead_sessions_leftovers_are_removed(project: Project) -> None:
    project.run().assert_outcomes(passed=2)
    base = f"{TEMPLATE_PREFIX}{project.prefix}"
    stale = project.cache / f"{base}_{'0' * HEX_LENGTH}.db"
    stale.write_bytes(b"")
    hold = project.cache / f"{stale.stem}.1.dead{HOLD_SUFFIX}"
    hold.write_bytes(b"")
    temporary = project.cache / f"{base}_{'1' * HEX_LENGTH}{TMP_MARKER}1_dead.db"
    temporary.write_bytes(b"")
    project.run().assert_outcomes(passed=2)
    assert not stale.exists()
    assert not hold.exists()
    assert not temporary.exists()
    assert project.templates() == [f"{project.template()}.db"]


def test_without_migrations_the_template_is_empty(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{pytester.path / 'selector.db'}")
    pytester.makepyprojecttoml("[tool.pytest.ini_options]\n")
    pytester.makepyfile(test_inner=EMPTY_INNER)
    pytester.runpytest_subprocess(*PLUGIN).assert_outcomes(passed=1)
    cache = pytester.path / ".pytest_cache" / "d" / CACHE_NAME
    expected = template_name(None, directory=pytester.path, identity=IDENTITY)
    assert [path.name for path in cache.glob(f"{TEMPLATE_PREFIX}*.db")] == [f"{expected}.db"]


def test_without_the_cache_provider_the_template_is_per_session(
    project: Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    for variable in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(variable, str(temporary))
    project.pytester.makepyfile(test_inner=TEMPORARY_INNER)
    project.run("-p", "no:cacheprovider").assert_outcomes(passed=1)
    seen = (project.pytester.path / "seen.txt").read_text(encoding="utf-8").split()
    assert any(name.startswith("rowx-templates-") and name.endswith(".db") for name in seen)
    assert any(name.startswith("rowx-clones-") and name.endswith(".db") for name in seen)
    # the session's directories are removed with the session, and the cache was never made
    assert list(temporary.iterdir()) == []
    assert not (project.pytester.path / ".pytest_cache").exists()


def test_an_invalid_url_fails_the_session_with_a_message(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://host/x.db")
    result = project.run()
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*rowx: *SQLite URL*"])


def test_the_memory_selector_is_accepted(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    provisioner("sqlite::memory:")
    monkeypatch.setenv("DATABASE_URL", "sqlite::memory:")
    project.run().assert_outcomes(passed=2)
    assert project.leftovers() == []


def test_the_pool_fixture_runs_queries(project: Project) -> None:
    project.pytester.makepyfile(test_inner=POOL_INNER)
    project.run().assert_outcomes(passed=1)


def test_the_provisioner_rejects_an_invalid_url() -> None:
    with pytest.raises((ValueError, InterfaceError)):
        SqliteProvisioner("sqlite://host/x.db")


def test_with_database_rejects_an_unknown_name(tmp_path: Path) -> None:
    unknown = provisioner(f"sqlite:///{tmp_path / 'x.db'}")
    with pytest.raises(InterfaceError, match="does not exist"):
        unknown.with_database(f"{TEST_PREFIX}{secrets.token_hex(4)}")


def test_clone_needs_a_built_template(tmp_path: Path) -> None:
    unbuilt = provisioner(f"sqlite:///{tmp_path / 'x.db'}")
    with pytest.raises(InterfaceError, match="not built"):
        asyncio.run(unbuilt.clone("rowx_tpl_p_h", "p"))


def write_side_files(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").write_bytes(b"")


def test_drop_removes_the_clone_and_its_side_files(tmp_path: Path) -> None:
    owner = provisioner(f"sqlite:///{tmp_path / 'x.db'}", cache=directory_in(tmp_path))
    name = f"{TEMPLATE_PREFIX}{'a' * 8}_{'b' * HEX_LENGTH}"

    async def scenario() -> None:
        hold = await owner.ensure_template(None, name, keep_stale=False, project="a" * 8)
        try:
            clone = await owner.clone(name, "a" * 8)
            path = database_path(owner.with_database(clone))
            assert path.exists()
            await asyncio.to_thread(write_side_files, path)
            await owner.drop(clone)
            assert not list(path.parent.glob(f"{path.name}*"))
            # an unknown name is not an error: the drop is idempotent
            await owner.drop(clone)
        finally:
            await hold.close()

    asyncio.run(scenario())


def test_a_hold_is_exclusive_while_it_is_taken(tmp_path: Path) -> None:
    final = tmp_path / f"{TEMPLATE_PREFIX}{'a' * 8}_{'b' * HEX_LENGTH}.db"
    path, descriptor = take_hold(final)
    try:
        # a second open file description cannot lock the file while the hold is taken
        assert try_lock(path) is None
    finally:
        release_hold(path, descriptor)
    assert not path.exists()
    again = try_lock(path)
    assert again is not None
    unlock(again)


def test_a_mismatched_project_is_rejected_before_touching_the_filesystem(tmp_path: Path) -> None:
    owner = provisioner(f"sqlite:///{tmp_path / 'x.db'}", cache=directory_in(tmp_path / "cache"))
    name = f"{TEMPLATE_PREFIX}{'a' * 8}_{'b' * HEX_LENGTH}"
    with pytest.raises(ValueError, match="does not belong"):
        asyncio.run(owner.ensure_template(None, name, keep_stale=False, project="c" * 8))
    assert not (tmp_path / "cache").exists()


def test_concurrent_provisioners_build_the_template_once(project: Project) -> None:
    service = CountingMigrations(project.url, project.migrations)
    name = project.template()

    async def scenario() -> None:
        first, second = project.provisioner(), project.provisioner()
        holds = await asyncio.gather(
            first.ensure_template(service, name, keep_stale=False, project=project.prefix),
            second.ensure_template(service, name, keep_stale=False, project=project.prefix),
        )
        assert project.templates() == [f"{name}.db"]
        assert not [path for path in project.cache.iterdir() if TMP_MARKER in path.name]
        for hold in holds:
            await hold.close()

    asyncio.run(scenario())
    assert service.runs == 1
    assert project.leftovers() == []


def test_a_cancel_while_the_hold_is_taken_leaves_no_hold_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started, proceed = threading.Event(), threading.Event()
    real = _testing.take_hold

    def blocked(final: Path) -> tuple[Path, int]:
        started.set()
        proceed.wait()
        return real(final)

    monkeypatch.setattr(_testing, "take_hold", blocked)
    owner = provisioner(f"sqlite:///{tmp_path / 'x.db'}", cache=directory_in(tmp_path))
    name = f"{TEMPLATE_PREFIX}{'a' * 8}_{'b' * HEX_LENGTH}"

    async def scenario() -> None:
        task = asyncio.ensure_future(owner.ensure_template(None, name, keep_stale=False, project="a" * 8))
        await asyncio.to_thread(started.wait)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not list((tmp_path / CACHE_NAME).glob(f"*{HOLD_SUFFIX}"))
