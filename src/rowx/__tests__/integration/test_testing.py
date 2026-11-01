import os
import sys
import textwrap
from subprocess import STDOUT  # ruff: ignore[suspicious-subprocess-import]
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql

from rowx.postgres import Migrator
from rowx.postgres._admin import with_database
from rowx.testing import LOCK_DATABASE, LOCK_KEY, TEMPLATE_PREFIX, TEST_PREFIX, template_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from subprocess import Popen

pytest_plugins = ["pytester"]

DSN: Final[str | None] = os.environ.get("ROWX_TEST_DSN")
SESSION_TIMEOUT: Final[float] = 120.0
POLL_LIMIT: Final[int] = 20000

# `-p rowx.testing` is how a project opts in without a conftest
PLUGIN: Final[tuple[str, ...]] = ("-p", "rowx.testing", "-p", "no:cacheprovider")

INNER: Final[str] = textwrap.dedent(
    """
    import asyncio
    from pathlib import Path

    from rowx import query_unchecked


    def record(url):
        with Path("urls.txt").open("a") as handle:
            handle.write(url + "\\n")


    def insert(conn):
        asyncio.run(query_unchecked[tuple[()]](t"INSERT INTO {TABLE:ident} (id) VALUES (1)").execute(conn))


    def count(conn):
        return asyncio.run(query_unchecked[tuple[int]](t"SELECT count(*)::int4 FROM {TABLE:ident}").one(conn))[0]


    def test_first(rowx_database_url, rowx_connection):
        record(rowx_database_url)
        insert(rowx_connection)
        assert count(rowx_connection) == 1


    def test_second(rowx_database_url, rowx_connection):
        record(rowx_database_url)
        insert(rowx_connection)
        assert count(rowx_connection) == 1
    """
)


# waits, holding its clone, until the outer test opens the gate (an advisory lock it holds)
HOLD_INNER: Final[str] = textwrap.dedent(
    """
    import os

    import psycopg


    def test_wait(rowx_database_url):
        with psycopg.connect(os.environ["ROWX_TEST_DSN"], autocommit=True) as conn:
            conn.execute("SELECT pg_notify(%s, 'waiting')", (CHANNEL,))
            conn.execute("SELECT pg_advisory_lock(%s)", (GATE,))


    def test_after(rowx_database_url):
        pass
    """
)


# announces the session before the plugin's session fixture takes the build lock
ANNOUNCE_INNER: Final[str] = textwrap.dedent(
    """
    import os

    import psycopg
    import pytest


    @pytest.fixture(scope="session", autouse=True)
    def announce():
        with psycopg.connect(os.environ["ROWX_TEST_DSN"], autocommit=True) as conn:
            conn.execute("SELECT pg_notify(%s, 'started')", (CHANNEL,))


    def test_clone(rowx_database_url):
        pass
    """
)


POOL_INNER: Final[str] = textwrap.dedent(
    """
    import asyncio

    from rowx import query_unchecked


    def test_pool(rowx_pool, rowx_connection):
        async def work():
            await query_unchecked[tuple[()]](t"INSERT INTO {TABLE:ident} (id) VALUES (1)").execute(rowx_pool)
            (count,) = await query_unchecked[tuple[int]](t"SELECT count(*)::int4 FROM {TABLE:ident}").one(rowx_connection)
            return count

        assert asyncio.run(work()) == 1
    """
)


def finish(session: Popen[str]) -> tuple[int, str]:
    try:
        output, _ = session.communicate(timeout=SESSION_TIMEOUT)
    finally:
        if session.poll() is None:
            session.kill()
            session.communicate()
    return int(session.returncode), output


def notified(listener: psycopg.Connection, count: int) -> int:
    return sum(1 for _ in listener.notifies(timeout=SESSION_TIMEOUT, stop_after=count))


def databases(prefix: str) -> set[str]:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s", (prefix.replace("_", r"\_") + "%",)
        )
        return {row[0] for row in rows}


def oid_of(name: str) -> int | None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as conn:
        row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return None if row is None else int(row[0])


def drop(name: str) -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


class Project:
    def __init__(self, pytester: pytest.Pytester, table: str) -> None:
        self.pytester = pytester
        self.table = table
        self.migrations = pytester.path / "migrations"
        self.migrations.mkdir()
        self.write_migration(1, f"CREATE TABLE {table} (id int4 PRIMARY KEY);")
        self.set_config()
        pytester.makepyfile(test_inner=f'TABLE = "{table}"\n' + INNER)

    def write_migration(self, version: int, sql: str) -> None:
        (self.migrations / f"{version}_m{version}.sql").write_text(sql, encoding="utf-8")

    def set_config(self, migrate: str = "") -> None:
        self.pytester.makepyprojecttoml(
            f'[tool.pytest.ini_options]\nrowx_database_url = "{DSN}"\n\n[tool.rowx.migrate]\ndir = "migrations"\n{migrate}'
        )

    def template(self, *, table: str = "_rowx_migrations") -> str:
        return template_name(Migrator(self.migrations), table=table)

    def run(self, *args: str) -> pytest.RunResult:
        return self.pytester.runpytest_subprocess(*PLUGIN, *args)

    # a session left running, so another can be started beside it
    def start(self, *args: str, cwd: Path | None = None) -> Popen[str]:
        command = [sys.executable, "-m", "pytest", *PLUGIN, *args]
        return self.pytester.popen(command, stderr=STDOUT, cwd=cwd or self.pytester.path, text=True)

    # a second project beside this one, with its own migrations and server URL
    def sibling(self, migration: str, url: str) -> Path:
        root = self.pytester.mkdir("sibling")
        (root / "migrations").mkdir()
        (root / "migrations" / "1_m1.sql").write_text(migration, encoding="utf-8")
        (root / "pyproject.toml").write_text(
            f'[tool.pytest.ini_options]\nrowx_database_url = "{url}"\n\n[tool.rowx.migrate]\ndir = "migrations"\n',
            encoding="utf-8",
        )
        return root

    def urls(self) -> list[str]:
        path = self.pytester.path / "urls.txt"
        return path.read_text(encoding="utf-8").split() if path.exists() else []


@pytest.fixture
def project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Iterator[Project]:
    if DSN is None:
        if os.environ.get("ROWX_REQUIRE_DB") == "1":
            pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
        pytest.skip("ROWX_TEST_DSN is not set")
    monkeypatch.delenv("ROWX_REQUIRE_DB", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    leaked_before = databases(TEST_PREFIX)
    created = Project(pytester, f"t_{os.urandom(6).hex()}")
    seen = {created.template()}
    try:
        yield created
    finally:
        # every template this test could have built is removed
        seen.add(created.template())
        for name in seen:
            drop(name)
    # no clone outlives the session, passing or failing
    assert databases(TEST_PREFIX) <= leaked_before


def dbname(url: str) -> str:
    return url.rsplit("/", 1)[1].split("?", 1)[0]


def db_test[F: Callable[..., object]](function: F) -> F:
    return pytest.mark.integration(pytest.mark.db(function))


def test_importing_rowx_does_not_import_pytest(pytester: pytest.Pytester) -> None:
    # a fresh interpreter, so this session's own pytest import doesn't count
    code = "import sys, rowx, rowx.postgres; sys.exit('pytest' in sys.modules)"
    result = pytester.runpython_c(code)
    assert result.ret == 0, result.stderr.str()


def test_template_name_follows_the_migrations_and_their_config(tmp_path: Path) -> None:
    (tmp_path / "1_a.sql").write_text("SELECT 1;", encoding="utf-8")
    first = template_name(Migrator(tmp_path))
    assert first.startswith(TEMPLATE_PREFIX)
    assert template_name(Migrator(tmp_path)) == first
    (tmp_path / "2_b.up.sql").write_text("SELECT 2;", encoding="utf-8")
    (tmp_path / "2_b.down.sql").write_text("SELECT 3;", encoding="utf-8")
    with_up = template_name(Migrator(tmp_path))
    assert with_up != first
    # a down file never changes the template
    (tmp_path / "2_b.down.sql").write_text("SELECT 5;", encoding="utf-8")
    assert template_name(Migrator(tmp_path)) == with_up
    # the history table and the schemas created first are part of the template too
    others = {
        template_name(Migrator(tmp_path), table="app.history"),
        template_name(Migrator(tmp_path), create_schemas=["app"]),
    }
    assert len(others) == 2
    assert with_up not in others
    assert template_name(Migrator(tmp_path), create_schemas=["b", "a"]) == template_name(
        Migrator(tmp_path), create_schemas=["a", "b", "a"]
    )
    (tmp_path / "1_a.sql").write_text("SELECT 4;", encoding="utf-8")
    assert template_name(Migrator(tmp_path)) not in {first, with_up}


def test_plugin_is_inactive_unless_opted_in(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_x(rowx_connection): ...")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fixture 'rowx_connection' not found*"])


@pytest.mark.parametrize("require", [False, True])
def test_no_url_skips_or_fails(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, require: bool) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ROWX_REQUIRE_DB", raising=False)
    if require:
        monkeypatch.setenv("ROWX_REQUIRE_DB", "1")
    (pytester.path / "migrations").mkdir()
    pytester.makepyfile("def test_x(rowx_database_url): ...")
    result = pytester.runpytest_subprocess(*PLUGIN)
    if require:
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*ROWX_REQUIRE_DB=1 but no database URL*"])
    else:
        result.assert_outcomes(skipped=1)


@db_test
def test_url_comes_from_the_database_url_env(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    assert DSN is not None
    monkeypatch.setenv("MY_DB", DSN)
    project.pytester.makepyprojecttoml(
        '[tool.rowx]\ndatabase_url_env = "MY_DB"\n\n[tool.rowx.migrate]\ndir = "migrations"\n'
    )
    project.run("--rowx-keep-templates", "-k", "test_first").assert_outcomes(passed=1)


@db_test
def test_each_test_gets_an_isolated_migrated_database(project: Project) -> None:
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    first, second = project.urls()
    # both tests inserted the same primary key, so neither saw the other's row
    assert dbname(first) != dbname(second)
    assert dbname(first).startswith(TEST_PREFIX)
    assert not {dbname(first), dbname(second)} & databases(TEST_PREFIX)


@db_test
def test_a_failing_test_still_drops_its_clone(project: Project) -> None:
    project.pytester.makepyfile(
        test_fail="def test_boom(rowx_database_url, rowx_pool):\n    from pathlib import Path\n"
        "    Path('urls.txt').write_text(rowx_database_url)\n    raise AssertionError\n"
    )
    result = project.run("--rowx-keep-templates", "test_fail.py")
    result.assert_outcomes(failed=1)
    assert dbname(project.urls()[0]) not in databases(TEST_PREFIX)


@db_test
def test_pool_and_connection_work_in_a_coroutine(project: Project) -> None:
    project.pytester.makepyfile(test_pool=f'TABLE = "{project.table}"\n' + POOL_INNER)
    project.run("--rowx-keep-templates", "test_pool.py").assert_outcomes(passed=1)


@db_test
def test_template_is_reused_then_rebuilt_when_a_migration_changes(project: Project) -> None:
    first = project.template()
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    built = oid_of(first)
    assert built is not None

    # a second run finds the complete template
    project.run().assert_outcomes(passed=2)
    assert oid_of(first) == built

    project.write_migration(2, f"CREATE TABLE {project.table}_b (id int4);")
    second = project.template()
    assert second != first
    project.run().assert_outcomes(passed=2)
    assert oid_of(second) is not None
    # the stale template was dropped when the new one was built
    assert oid_of(first) is None


@db_test
def test_a_config_change_rebuilds_the_template(project: Project) -> None:
    first = project.template()
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    history = f"{project.table}_history"
    project.set_config(f'table = "{history}"\n')
    second = project.template(table=history)
    assert second != first
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    assert DSN is not None
    # the new template keeps its history in the configured table
    with psycopg.connect(with_database(DSN, second), autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (history,)).fetchone()
    assert row == (True,)
    drop(first)
    drop(second)


@db_test
def test_keep_templates_keeps_the_stale_template(project: Project) -> None:
    first = project.template()
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    project.write_migration(2, f"CREATE TABLE {project.table}_b (id int4);")
    second = project.template()
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    assert oid_of(first) is not None
    assert oid_of(second) is not None
    drop(first)
    drop(second)


@db_test
def test_an_incomplete_template_is_rebuilt(project: Project) -> None:
    assert DSN is not None
    name = project.template()
    # what a build that died part-way leaves behind: the database, but no completion mark
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)


@db_test
def test_a_failing_migration_reports_and_leaves_nothing(project: Project) -> None:
    project.write_migration(2, "THIS IS NOT SQL;")
    result = project.run("--rowx-keep-templates")
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*the migrations failed on the template database*"])
    assert oid_of(project.template()) is None


@db_test
def test_cleanup_spares_a_template_another_session_holds(project: Project) -> None:
    assert DSN is not None
    channel = f"rowx_{os.urandom(6).hex()}"
    gate = int.from_bytes(os.urandom(4))
    project.pytester.makepyfile(test_hold=f"CHANNEL = {channel!r}\nGATE = {gate}\n" + HOLD_INNER)
    # different migrations, so its session builds a new template and cleans up the stale ones; its URL selects
    # another database, so the claim on the held template must live where every session looks
    other = project.sibling(f"CREATE TABLE {project.table}_other (id int4);", with_database(DSN, LOCK_DATABASE))
    (other / "test_other.py").write_text("def test_other(rowx_database_url):\n    pass\n", encoding="utf-8")
    other_template = template_name(Migrator(other / "migrations"))
    try:
        with psycopg.connect(DSN, autocommit=True) as listener:
            listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
            listener.execute("SELECT pg_advisory_lock(%s)", (gate,))
            held = project.start("test_hold.py")
            try:
                # the held session has built its template and is idle between clones
                assert notified(listener, 1) == 1
                ret, output = finish(project.start(cwd=other))
                assert ret == 0, output
            finally:
                listener.execute("SELECT pg_advisory_unlock(%s)", (gate,))
            ret, output = finish(held)
        # its second test cloned the template the other session left alone
        assert ret == 0, output
        assert oid_of(project.template()) is not None
        assert oid_of(other_template) is not None
    finally:
        drop(other_template)


@db_test
def test_sessions_on_different_databases_share_the_build_lock(project: Project) -> None:
    assert DSN is not None
    channel = f"rowx_{os.urandom(6).hex()}"
    inner = f"CHANNEL = {channel!r}\n" + ANNOUNCE_INNER
    project.pytester.makepyfile(test_announce=inner)
    # the same migrations, so both sessions want the one template
    migration = (project.migrations / "1_m1.sql").read_text(encoding="utf-8")
    other = project.sibling(migration, with_database(DSN, LOCK_DATABASE))
    (other / "test_announce.py").write_text(inner, encoding="utf-8")
    assert template_name(Migrator(other / "migrations")) == project.template()
    waiting = sql.SQL(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid = 0 AND objid = %s"
        " AND objsubid = 1 AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    with (
        psycopg.connect(DSN, autocommit=True) as listener,
        psycopg.connect(with_database(DSN, LOCK_DATABASE), autocommit=True) as gate,
    ):
        listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        # holds the build lock, so neither session can start building
        gate.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        sessions = [project.start("test_announce.py"), project.start(cwd=other)]
        try:
            assert notified(listener, 2) == 2
            # both sessions queue on the one lock, although their URLs select different databases
            for _ in range(POLL_LIMIT):
                row = gate.execute(waiting, (LOCK_KEY,)).fetchone()
                if row is not None and row[0] == 2:
                    break
            else:
                pytest.fail("the two sessions never waited on the same build lock")
        finally:
            gate.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        results = [finish(session) for session in sessions]
    # one built the template and the other reused it
    for ret, output in results:
        assert ret == 0, output
    assert oid_of(project.template()) is not None


@db_test
def test_the_template_is_built_once_under_xdist(project: Project) -> None:
    pytest.importorskip("xdist")
    project.run("--rowx-keep-templates", "-n", "2").assert_outcomes(passed=2)
    assert oid_of(project.template()) is not None
