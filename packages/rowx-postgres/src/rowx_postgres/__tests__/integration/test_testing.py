import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from subprocess import STDOUT  # ruff: ignore[suspicious-subprocess-import]
from time import time
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from psycopg import sql
from rowx_core.lifecycle import make_tag
from rowx_postgres import Migrator
from rowx_postgres._admin import with_database
from rowx_postgres._embedded_testing import publish_blocking
from rowx_postgres._testing import LOCK_DATABASE, build_key

from rowx.testing import TEMPLATE_PREFIX, TEST_PREFIX, project_prefix, template_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from subprocess import Popen

pytest_plugins = ["pytester"]

DSN: Final[str | None] = os.environ.get("ROWX_TEST_DSN")
SESSION_TIMEOUT: Final[float] = 120.0
POLL_LIMIT: Final[int] = 200000
OLD: Final[int] = 7200

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


ASYNC_INNER: Final[str] = textwrap.dedent(
    """
    import pytest
    import pytest_asyncio

    from rowx import query_unchecked


    async def work(pool, conn):
        await query_unchecked[tuple[()]](t"INSERT INTO {TABLE:ident} (id) VALUES (1)").execute(pool)
        (count,) = await query_unchecked[tuple[int]](t"SELECT count(*)::int4 FROM {TABLE:ident}").one(conn)
        return count


    @pytest_asyncio.fixture(scope="session", loop_scope="session")
    async def session_marker():
        return "session"


    # first, so the session's template is also built while the loop runs
    @pytest.mark.asyncio(loop_scope="session")
    async def test_requested_inside_the_running_loop(request):
        pool = request.getfixturevalue("rowx_pool")
        conn = request.getfixturevalue("rowx_connection")
        assert await work(pool, conn) == 1


    @pytest.mark.asyncio
    async def test_function_loop(rowx_pool, rowx_connection):
        assert await work(rowx_pool, rowx_connection) == 1


    @pytest.mark.asyncio(loop_scope="session")
    async def test_session_loop(rowx_pool, rowx_connection, session_marker):
        assert session_marker == "session"
        assert await work(rowx_pool, rowx_connection) == 1


    @pytest.mark.asyncio(loop_scope="session")
    async def test_session_loop_again(rowx_pool, rowx_connection):
        assert await work(rowx_pool, rowx_connection) == 1
    """
)


ANYIO_INNER: Final[str] = textwrap.dedent(
    """
    import pytest

    from rowx import query_unchecked


    @pytest.fixture
    def anyio_backend():
        return "asyncio"


    @pytest.mark.anyio
    async def test_anyio(rowx_pool, rowx_connection):
        await query_unchecked[tuple[()]](t"INSERT INTO {TABLE:ident} (id) VALUES (1)").execute(rowx_pool)
        (count,) = await query_unchecked[tuple[int]](t"SELECT count(*)::int4 FROM {TABLE:ident}").one(rowx_connection)
        assert count == 1


    @pytest.mark.anyio
    async def test_anyio_requested_inside_the_running_loop(request):
        conn = request.getfixturevalue("rowx_connection")
        (count,) = await query_unchecked[tuple[int]](t"SELECT count(*)::int4 FROM {TABLE:ident}").one(conn)
        assert count == 0
    """
)


# counts every template build, across xdist workers, in a file
COUNTING_CONFTEST: Final[str] = textwrap.dedent(
    """
    from pathlib import Path

    import rowx_postgres._testing as plugin

    original = plugin.build


    async def counted(*args, **kwargs):
        with Path(__file__).with_name("builds.txt").open("a") as handle:
            handle.write("build\\n")
        return await original(*args, **kwargs)


    plugin.build = counted
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
        # a finished template is flagged IS_TEMPLATE, which a drop refuses
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None:
            conn.execute(sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE false").format(sql.Identifier(name)))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def sessions_on(name: str) -> int:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as conn:
        row = conn.execute("SELECT numbackends FROM pg_stat_database WHERE datname = %s", (name,)).fetchone()
    return 0 if row is None else int(row[0])


# a database a past session left behind, tagged `age` seconds ago
def leave(name: str, age: int) -> None:
    assert DSN is not None
    tag = make_tag().rpartition(":")[0] + f":{int(time()) - age}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), sql.Literal(tag)))


def retag(name: str, age: int) -> None:
    assert DSN is not None
    tag = make_tag().rpartition(":")[0] + f":{int(time()) - age}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), sql.Literal(tag)))


class Project:
    kind: Final[str] = "server"

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

    # `migrate` is appended to [tool.rowx.migrate] and `ini` to the pytest ini options
    def set_config(self, migrate: str = "", ini: str = "") -> None:
        self.pytester.makepyprojecttoml(
            f'[tool.pytest.ini_options]\n{ini}\n[tool.rowx.migrate]\ndir = "migrations"\n{migrate}'
        )

    def template(self, *, table: str = "_rowx_migrations") -> str:
        return template_name(Migrator(self.migrations), directory=self.migrations, table=table)

    @property
    def prefix(self) -> str:
        return project_prefix(self.migrations)

    def run(self, *args: str) -> pytest.RunResult:
        return self.pytester.runpytest_subprocess(*PLUGIN, *args)

    # a session left running, so another can be started beside it
    def start(self, *args: str, cwd: Path | None = None) -> Popen[str]:
        command = [sys.executable, "-m", "pytest", *PLUGIN, *args]
        return self.pytester.popen(command, stderr=STDOUT, cwd=cwd or self.pytester.path, text=True)

    # a second project beside this one, at another path and so with another prefix
    def sibling(self, migration: str) -> Path:
        root = self.pytester.mkdir("sibling")
        (root / "migrations").mkdir()
        (root / "migrations" / "1_m1.sql").write_text(migration, encoding="utf-8")
        (root / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\n\n[tool.rowx.migrate]\ndir = "migrations"\n', encoding="utf-8"
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
    # the plugin reads the server URL from the environment, never from a literal in the ini
    monkeypatch.setenv("DATABASE_URL", DSN)
    created = Project(pytester, f"t_{os.urandom(6).hex()}")
    # only the databases of this test's projects, as other sessions may share the server
    prefixes = tuple(
        project_prefix(pytester.path / path) for path in ("migrations", "alternative", "sibling/migrations")
    )
    seen = {created.template()}
    try:
        yield created
    finally:
        # every template this test could have built is removed
        seen.add(created.template())
        for name in seen:
            drop(name)
    # no clone or half-built template outlives the session, passing or failing
    for prefix in prefixes:
        assert not databases(f"{TEST_PREFIX}{prefix}_")
        assert not {name for name in databases(f"{TEMPLATE_PREFIX}{prefix}_") if "_tmp_" in name}


# a database for a session's URL to name instead of the test DSN's own
@pytest.fixture
def other_url() -> Iterator[str]:
    assert DSN is not None
    name = f"rowx_url_{os.urandom(6).hex()}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        yield with_database(DSN, name)
    finally:
        drop(name)


def dbname(url: str) -> str:
    return url.rsplit("/", 1)[1].split("?", 1)[0]


def db_test[F: Callable[..., object]](function: F) -> F:
    return pytest.mark.db(function)


def test_importing_rowx_does_not_import_pytest(pytester: pytest.Pytester) -> None:
    # a fresh interpreter, so this session's own pytest import doesn't count
    code = "import sys, rowx, rowx_postgres; sys.exit('pytest' in sys.modules)"
    result = pytester.runpython_c(code)
    assert result.ret == 0, result.stderr.str()


def test_plugin_is_inactive_unless_opted_in(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_x(rowx_connection): ...")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fixture 'rowx_connection' not found*"])


@pytest.mark.parametrize("require", [False, True])
def test_no_url_skips_or_fails(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, require: bool) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ROWX_REQUIRE_DB", raising=False)
    ini = "rowx_require_database = true\n" if require else ""
    pytester.makepyprojecttoml(f"[tool.pytest.ini_options]\n{ini}")
    (pytester.path / "migrations").mkdir()
    pytester.makepyfile("def test_x(rowx_database_url): ...")
    result = pytester.runpytest_subprocess(*PLUGIN)
    if require:
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*rowx_require_database is set but no database URL*"])
    else:
        result.assert_outcomes(skipped=1)


@db_test
def test_url_comes_from_the_environment_variable_the_ini_names(
    plugin_project: AnyProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.environ["DATABASE_URL"]
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("MY_DB", url)
    plugin_project.set_config(ini='rowx_database_url_env = "MY_DB"\n')
    plugin_project.run("--rowx-keep-templates", "-k", "test_first").assert_outcomes(passed=1)
    # the variable the ini names, not DATABASE_URL, is read
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("MY_DB")
    result = plugin_project.run("-rs", "-k", "test_first")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*$MY_DB*"])


@db_test
def test_url_env_defaults_to_the_rowx_config(plugin_project: AnyProject, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIGURED_DB", os.environ["DATABASE_URL"])
    monkeypatch.delenv("DATABASE_URL")
    plugin_project.pytester.makepyprojecttoml(
        '[tool.rowx]\ndatabase_url_env = "CONFIGURED_DB"\n\n[tool.rowx.migrate]\ndir = "migrations"\n'
    )
    plugin_project.run("--rowx-keep-templates", "-k", "test_first").assert_outcomes(passed=1)


@db_test
def test_require_database_fails_instead_of_skipping_with_the_variable_unset(
    plugin_project: AnyProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL")
    plugin_project.set_config(ini="rowx_require_database = true\n")
    result = plugin_project.run("-k", "test_first")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*rowx_require_database is set but no database URL*"])


@db_test
def test_the_migrations_ini_overrides_the_configured_directory(plugin_project: AnyProject) -> None:
    # the configured directory makes another table, so the tests pass only if the ini's directory is the one used
    plugin_project.write_migration(1, f"CREATE TABLE {plugin_project.table}_unused (id int4 PRIMARY KEY);")
    alternative = plugin_project.pytester.mkdir("alternative")
    (alternative / "1_m1.sql").write_text(f"CREATE TABLE {plugin_project.table} (id int4 PRIMARY KEY, extra int4);")
    plugin_project.set_config(ini='rowx_migrations = "alternative"\n')
    plugin_project.run("--rowx-keep-templates").assert_outcomes(passed=2)


@db_test
def test_the_migrations_ini_alone_turns_the_template_on(plugin_project: AnyProject) -> None:
    # no [tool.rowx.migrate] table at all
    alternative = plugin_project.pytester.mkdir("alternative")
    (alternative / "1_m1.sql").write_text(f"CREATE TABLE {plugin_project.table} (id int4 PRIMARY KEY);")
    plugin_project.pytester.makepyprojecttoml('[tool.pytest.ini_options]\nrowx_migrations = "alternative"\n')
    plugin_project.run("--rowx-keep-templates").assert_outcomes(passed=2)


@db_test
@pytest.mark.server(needs="create-database")
def test_each_test_gets_an_isolated_migrated_database(project: Project) -> None:
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    first, second = project.urls()
    # both tests inserted the same primary key, so neither saw the other's row
    assert dbname(first) != dbname(second)
    assert dbname(first).startswith(f"{TEST_PREFIX}{project.prefix}_")
    assert not {dbname(first), dbname(second)} & databases(TEST_PREFIX)


@db_test
def test_a_failing_test_still_drops_its_clone(plugin_project: AnyProject) -> None:
    plugin_project.pytester.makepyfile(
        test_fail="def test_boom(rowx_database_url, rowx_pool):\n    from pathlib import Path\n"
        "    Path('urls.txt').write_text(rowx_database_url)\n    raise AssertionError\n"
    )
    result = plugin_project.run("--rowx-keep-templates", "test_fail.py")
    result.assert_outcomes(failed=1)
    (url,) = plugin_project.urls()
    if plugin_project.kind == "server":
        assert dbname(url) not in databases(TEST_PREFIX)
    else:
        # the instance's private directory is gone
        assert not Path(parse_qs(urlsplit(url).query)["host"][0]).exists()


@db_test
@pytest.mark.server(needs="create-database")
def test_pool_and_connection_work_in_a_coroutine(project: Project) -> None:
    project.pytester.makepyfile(test_pool=f'TABLE = "{project.table}"\n' + POOL_INNER)
    project.run("--rowx-keep-templates", "test_pool.py").assert_outcomes(passed=1)


@db_test
@pytest.mark.server(needs="create-database")
def test_fixtures_work_from_pytest_asyncio_tests(project: Project) -> None:
    pytest.importorskip("pytest_asyncio")
    project.set_config(ini='asyncio_mode = "strict"\nasyncio_default_fixture_loop_scope = "function"\n')
    project.pytester.makepyfile(test_async=f'TABLE = "{project.table}"\n' + ASYNC_INNER)
    result = project.run("--rowx-keep-templates", "test_async.py")
    result.assert_outcomes(passed=4)


@db_test
@pytest.mark.server(needs="create-database")
def test_fixtures_work_from_an_anyio_test_on_asyncio(project: Project) -> None:
    pytest.importorskip("anyio")
    project.pytester.makepyfile(test_anyio=f'TABLE = "{project.table}"\n' + ANYIO_INNER)
    project.run("--rowx-keep-templates", "test_anyio.py").assert_outcomes(passed=2)


@db_test
@pytest.mark.server(needs="create-database")
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
@pytest.mark.server(needs="create-database")
def test_a_finished_template_is_flagged_as_a_template(project: Project) -> None:
    assert DSN is not None
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    with psycopg.connect(DSN, autocommit=True) as conn:
        row = conn.execute(
            "SELECT datistemplate, datallowconn, shobj_description(oid, 'pg_database') FROM pg_database"
            " WHERE datname = %s",
            (project.template(),),
        ).fetchone()
    assert row == (True, False, None)


@db_test
@pytest.mark.server(needs="create-database")
def test_a_config_change_rebuilds_the_template(project: Project) -> None:
    first = project.template()
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    history = f"{project.table}_history"
    project.set_config(f'table = "{history}"\n')
    second = project.template(table=history)
    assert second != first
    project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    # the history table is read through the template, which refuses connections, so it is cloned and read back
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(sql.Identifier(second)))
    with psycopg.connect(with_database(DSN, second), autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (history,)).fetchone()
    assert row == (True,)
    drop(first)
    drop(second)


@db_test
@pytest.mark.server(needs="create-database")
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
def test_a_failing_migration_reports_and_leaves_nothing(plugin_project: AnyProject) -> None:
    plugin_project.write_migration(2, "THIS IS NOT SQL;")
    result = plugin_project.run("--rowx-keep-templates")
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*the migrations failed on the template database*"])
    if isinstance(plugin_project, Project):
        assert oid_of(plugin_project.template()) is None
    else:
        assert plugin_project.snapshots() == set()
    # the half-built database under its temporary name is dropped too (the project fixture checks for leftovers)


@db_test
@pytest.mark.server(needs="create-database")
def test_cleanup_spares_another_projects_template(project: Project) -> None:
    other = project.sibling(f"CREATE TABLE {project.table}_other (id int4);")
    (other / "test_other.py").write_text("def test_other(rowx_database_url):\n    pass\n", encoding="utf-8")
    other_template = template_name(Migrator(other / "migrations"), directory=other / "migrations")
    assert project_prefix(other / "migrations") != project.prefix
    try:
        ret, output = finish(project.start(cwd=other))
        assert ret == 0, output
        assert oid_of(other_template) is not None
        first = project.template()
        project.run("--rowx-keep-templates", "test_inner.py").assert_outcomes(passed=2)
        project.write_migration(2, f"CREATE TABLE {project.table}_b (id int4);")
        project.run("test_inner.py").assert_outcomes(passed=2)
        # this project's stale template went, the other project's stayed
        assert oid_of(first) is None
        assert oid_of(other_template) is not None
    finally:
        drop(other_template)


@db_test
@pytest.mark.timing
def test_cleanup_spares_a_template_another_session_holds(
    project: Project, monkeypatch: pytest.MonkeyPatch, other_url: str
) -> None:
    assert DSN is not None
    channel = f"rowx_{os.urandom(6).hex()}"
    gate = int.from_bytes(os.urandom(4))
    project.pytester.makepyfile(test_hold=f"CHANNEL = {channel!r}\nGATE = {gate}\n" + HOLD_INNER)
    project.pytester.makepyfile(test_other="def test_other(rowx_database_url):\n    pass\n")
    held_template = project.template()
    with psycopg.connect(DSN, autocommit=True) as listener:
        listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        listener.execute("SELECT pg_advisory_lock(%s)", (gate,))
        # the sessions' URLs name different databases, and still share the claim
        monkeypatch.setenv("DATABASE_URL", other_url)
        held = project.start("test_hold.py")
        monkeypatch.setenv("DATABASE_URL", DSN)
        try:
            # the held session has built its template and is idle between clones
            assert notified(listener, 1) == 1
            # different migrations, so this session builds a new template and cleans up the stale ones
            project.write_migration(2, f"CREATE TABLE {project.table}_b (id int4);")
            ret, output = finish(project.start("test_other.py"))
            assert ret == 0, output
        finally:
            listener.execute("SELECT pg_advisory_unlock(%s)", (gate,))
        ret, output = finish(held)
    # its second test cloned the template the other session left alone
    assert ret == 0, output
    assert oid_of(held_template) is not None
    assert oid_of(project.template()) is not None


@db_test
@pytest.mark.timing
@pytest.mark.parametrize("own", [True, False], ids=["own-database", "other-database"])
def test_the_build_lock_is_taken_in_the_lock_database_before_the_lookup(
    project: Project, monkeypatch: pytest.MonkeyPatch, other_url: str, *, own: bool
) -> None:
    assert DSN is not None
    if not own:
        monkeypatch.setenv("DATABASE_URL", other_url)
    template = project.template()
    key = build_key(template)
    classid, objid = (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF
    waiting = (
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid = %s AND objid = %s"
        " AND objsubid = 1 AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    with psycopg.connect(with_database(DSN, LOCK_DATABASE), autocommit=True) as gate:
        gate.execute("SELECT pg_advisory_lock(%s)", (key,))
        session = project.start("-k", "test_first")
        try:
            # whatever database the URL names, the session queues on the lock in the lock database, although the
            # template does not exist yet
            for _ in range(POLL_LIMIT):
                row = gate.execute(waiting, (classid, objid)).fetchone()
                if row is not None and row[0] == 1:
                    break
            else:
                pytest.fail("the session never waited on the build lock")
            assert oid_of(template) is None
        finally:
            gate.execute("SELECT pg_advisory_unlock(%s)", (key,))
        ret, output = finish(session)
    assert ret == 0, output
    assert oid_of(template) is not None


@db_test
@pytest.mark.server(needs="create-database")
def test_the_template_is_built_once_under_xdist(project: Project) -> None:
    pytest.importorskip("xdist")
    project.pytester.makeconftest(COUNTING_CONFTEST)
    project.run("--rowx-keep-templates", "-n", "2").assert_outcomes(passed=2)
    assert oid_of(project.template()) is not None
    builds = (project.pytester.path / "builds.txt").read_text(encoding="utf-8").split()
    assert builds == ["build"]
    # a later run reuses it
    project.run("-n", "2").assert_outcomes(passed=2)
    assert (project.pytester.path / "builds.txt").read_text(encoding="utf-8").split() == ["build"]


@db_test
@pytest.mark.timing
def test_an_orphaned_clone_is_swept_on_the_next_run(project: Project) -> None:
    assert DSN is not None
    channel = f"rowx_{os.urandom(6).hex()}"
    gate = int.from_bytes(os.urandom(4))
    project.pytester.makepyfile(test_hold=f"CHANNEL = {channel!r}\nGATE = {gate}\n" + HOLD_INNER)
    prefix = f"{TEST_PREFIX}{project.prefix}_"
    # another project's old clone and a half-built template of this one that is also old
    foreign = f"{TEST_PREFIX}deadbeef_{os.urandom(4).hex()}"
    half_built = f"{TEMPLATE_PREFIX}{project.prefix}_{os.urandom(8).hex()}_tmp_{os.urandom(4).hex()}"
    leave(foreign, OLD)
    leave(half_built, OLD)
    orphans: list[str] = []
    try:
        with psycopg.connect(DSN, autocommit=True) as listener:
            listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
            listener.execute("SELECT pg_advisory_lock(%s)", (gate,))
            held = project.start("test_hold.py")
            try:
                assert notified(listener, 1) == 1
                (orphan,) = databases(prefix)
                orphans.append(orphan)
                # the inner session dies with its clone in place
                held.kill()
                held.communicate()
            finally:
                listener.execute("SELECT pg_advisory_unlock(%s)", (gate,))
        for _ in range(POLL_LIMIT):
            if sessions_on(orphan) == 0:
                break
        else:
            pytest.fail("the killed session's connection never closed")
        # the tag is fresh, so the next run leaves the clone alone
        project.run("-k", "test_first").assert_outcomes(passed=1)
        assert orphan in databases(prefix)
        assert half_built not in databases(TEMPLATE_PREFIX)
        # once the tag is over an hour old it is swept
        retag(orphan, OLD)
        project.run("-k", "test_first").assert_outcomes(passed=1)
        assert orphan not in databases(prefix)
        assert foreign in databases(TEST_PREFIX)
    finally:
        drop(foreign)
        drop(half_built)
        for name in orphans:
            drop(name)


# ---- embedded: a snapshot built once per session, a fresh instance per test ----

EMBEDDED_URL: Final[str] = "embedded://"

# counts every snapshot build, across xdist workers, in a file
EMBEDDED_COUNTING_CONFTEST: Final[str] = textwrap.dedent(
    """
    from pathlib import Path

    import rowx_postgres._embedded_testing as plugin

    original = plugin.build_snapshot


    async def counted(*args, **kwargs):
        with Path(__file__).with_name("builds.txt").open("a") as handle:
            handle.write("build\\n")
        return await original(*args, **kwargs)


    plugin.build_snapshot = counted
    """
)

EMBEDDED_POOL_INNER: Final[str] = textwrap.dedent(
    """
    import asyncio
    import pathlib

    from rowx import query_unchecked


    def test_pool_and_connection_together(rowx_pool, rowx_connection):
        async def work():
            try:
                await query_unchecked[tuple[()]](t"INSERT INTO {TABLE:ident} (id) VALUES (1)").execute(rowx_pool)
            except Exception as error:
                chain = []
                while error is not None:
                    chain.append(f"{type(error).__name__}: {error} hint={getattr(error, 'hint', None)}")
                    error = error.__cause__
                pathlib.Path("chain.txt").write_text("\\n".join(chain))
                return
            raise AssertionError("the pool served a second session")

        asyncio.run(work())
    """
)

EMBEDDED_ASYNC_INNER: Final[str] = textwrap.dedent(
    """
    import pytest
    import pytest_asyncio

    from rowx import query_unchecked


    async def insert_one(executor):
        await query_unchecked[tuple[()]](t"INSERT INTO {TABLE:ident} (id) VALUES (1)").execute(executor)
        (count,) = await query_unchecked[tuple[int]](t"SELECT count(*)::int4 FROM {TABLE:ident}").one(executor)
        return count


    @pytest_asyncio.fixture(scope="session", loop_scope="session")
    async def session_marker():
        return "session"


    @pytest.mark.asyncio(loop_scope="session")
    async def test_requested_inside_the_running_loop(request):
        assert await insert_one(request.getfixturevalue("rowx_pool")) == 1


    @pytest.mark.asyncio
    async def test_function_loop_pool(rowx_pool):
        assert await insert_one(rowx_pool) == 1


    @pytest.mark.asyncio
    async def test_function_loop_connection(rowx_connection):
        assert await insert_one(rowx_connection) == 1


    @pytest.mark.asyncio(loop_scope="session")
    async def test_session_loop_pool(rowx_pool, session_marker):
        assert session_marker == "session"
        assert await insert_one(rowx_pool) == 1


    @pytest.mark.asyncio(loop_scope="session")
    async def test_session_loop_connection(rowx_connection):
        assert await insert_one(rowx_connection) == 1
    """
)

EMBEDDED_ANYIO_INNER: Final[str] = textwrap.dedent(
    """
    import pytest

    from rowx import query_unchecked


    @pytest.fixture
    def anyio_backend():
        return "asyncio"


    @pytest.mark.anyio
    async def test_anyio_pool(rowx_pool):
        await query_unchecked[tuple[()]](t"INSERT INTO {TABLE:ident} (id) VALUES (1)").execute(rowx_pool)


    @pytest.mark.anyio
    async def test_anyio_requested_inside_the_running_loop(request):
        conn = request.getfixturevalue("rowx_connection")
        (count,) = await query_unchecked[tuple[int]](t"SELECT count(*)::int4 FROM {TABLE:ident}").one(conn)
        assert count == 0
    """
)

EMBEDDED_FAILING_INNER: Final[str] = textwrap.dedent(
    """
    def test_boom(rowx_database_url, rowx_connection):
        raise AssertionError
    """
)

# the session is interrupted from inside a test, as Ctrl-C would
EMBEDDED_INTERRUPT_INNER: Final[str] = textwrap.dedent(
    """
    import os
    import signal


    def test_interrupted(rowx_database_url, rowx_connection):
        os.kill(os.getpid(), signal.SIGINT)
        import time

        time.sleep(30)
    """
)


class EmbeddedProject:
    kind: Final[str] = "embedded"

    def __init__(self, pytester: pytest.Pytester, table: str) -> None:
        self.pytester = pytester
        self.table = table
        self.migrations = pytester.path / "migrations"
        self.migrations.mkdir()
        self.write_migration(1, f"CREATE TABLE {table} (id int4 PRIMARY KEY);")
        self.set_config()
        pytester.makepyfile(test_inner=f'TABLE = "{table}"\n' + INNER)
        pytester.makeconftest(EMBEDDED_COUNTING_CONFTEST)

    def write_migration(self, version: int, text: str) -> None:
        (self.migrations / f"{version}_m{version}.sql").write_text(text, encoding="utf-8")

    def set_config(self, migrate: str = "", ini: str = "") -> None:
        self.pytester.makepyprojecttoml(
            f'[tool.pytest.ini_options]\n{ini}\n[tool.rowx.migrate]\ndir = "migrations"\n{migrate}'
        )

    @property
    def prefix(self) -> str:
        return project_prefix(self.migrations)

    @property
    def cache(self) -> Path:
        return self.pytester.path / ".pytest_cache" / "d" / "rowx-embedded"

    def snapshots(self, prefix: str | None = None) -> set[str]:
        found = self.cache.glob(f"{prefix or self.prefix}_*.snapshot")
        return {path.name for path in found}

    def builds(self) -> int:
        path = self.pytester.path / "builds.txt"
        return len(path.read_text(encoding="utf-8").split()) if path.exists() else 0

    # the cache provider stays on, so the snapshot persists between runs
    def run(self, *args: str) -> pytest.RunResult:
        return self.pytester.runpytest_subprocess("-p", "rowx.testing", *args)

    def urls(self) -> list[str]:
        path = self.pytester.path / "urls.txt"
        return path.read_text(encoding="utf-8").split() if path.exists() else []


@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # short, because a Unix socket path is limited; every session's instance and snapshot directory is under it
    root = Path(tempfile.mkdtemp(prefix="rx", dir="/tmp"))
    monkeypatch.setenv("TMPDIR", str(root))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def embedded_project(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, private_tmp: Path
) -> Iterator[EmbeddedProject]:
    monkeypatch.setenv("DATABASE_URL", EMBEDDED_URL)
    monkeypatch.delenv("ROWX_REQUIRE_DB", raising=False)
    created = EmbeddedProject(pytester, f"t_{os.urandom(6).hex()}")
    yield created
    # a snapshot is tens of megabytes, and pytest keeps the last few runs' directories
    shutil.rmtree(created.pytester.path / ".pytest_cache", ignore_errors=True)
    # no instance, socket directory or snapshot temp directory outlives the session
    assert list(private_tmp.iterdir()) == []


embedded_test = pytest.mark.embedded

type AnyProject = Project | EmbeddedProject


# the engine-neutral plugin behaviours run on both: a server (which needs `CREATE DATABASE` for its templates) and an
# embedded database. A snapshot is keyed by the project path, so the pytester projects cannot share one warm cache
@pytest.fixture(
    params=[
        pytest.param("server", marks=pytest.mark.server(needs="create-database")),
        pytest.param("embedded", marks=pytest.mark.embedded),
    ]
)
def plugin_project(request: pytest.FixtureRequest) -> AnyProject:
    name = "project" if request.param == "server" else "embedded_project"
    created = request.getfixturevalue(name)
    assert isinstance(created, Project | EmbeddedProject)
    return created


# a statement a test leaks while it runs, and the time the session's teardown of that test took
EMBEDDED_LEAK_CONFTEST: Final[str] = textwrap.dedent(
    """
    import time
    from pathlib import Path

    import pytest


    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(item, nextitem):
        started = time.monotonic()
        try:
            return (yield)
        finally:
            Path(__file__).with_name("teardown.txt").write_text(str(time.monotonic() - started))
    """
)

EMBEDDED_LEAK_INNER: Final[str] = textwrap.dedent(
    """
    import threading
    import time

    import psycopg


    def test_leak(rowx_database_url):
        def run():
            try:
                with psycopg.connect(rowx_database_url, autocommit=True) as conn:
                    conn.execute("SELECT pg_sleep(120)")
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()
        # the statement is running by the time the test ends
        time.sleep(1.5)
    """
)


@embedded_test
def test_embedded_each_test_gets_an_isolated_migrated_database(embedded_project: EmbeddedProject) -> None:
    embedded_project.run().assert_outcomes(passed=2)
    first, second = embedded_project.urls()
    # both tests inserted the same primary key, so neither saw the other's row
    assert first != second
    assert first.startswith("postgresql://")
    assert embedded_project.builds() == 1
    assert len(embedded_project.snapshots()) == 1


@embedded_test
def test_embedded_snapshot_is_rebuilt_only_when_a_migration_changes(embedded_project: EmbeddedProject) -> None:
    embedded_project.run().assert_outcomes(passed=2)
    (first,) = embedded_project.snapshots()
    embedded_project.run().assert_outcomes(passed=2)
    assert embedded_project.builds() == 1
    embedded_project.write_migration(2, f"CREATE TABLE {embedded_project.table}_b (id int4);")
    embedded_project.run().assert_outcomes(passed=2)
    assert embedded_project.builds() == 2
    # the stale snapshot went when the new one was built
    (second,) = embedded_project.snapshots()
    assert second != first


# records which process asked for the template, so that the test knows the build was contended
EMBEDDED_WORKERS_CONFTEST: Final[str] = textwrap.dedent(
    """
    import os
    from pathlib import Path

    import rowx_postgres._embedded_testing as plugin

    original_ensure = plugin.EmbeddedProvisioner.ensure_template


    async def recorded(self, *args, **kwargs):
        with Path(__file__).with_name("workers.txt").open("a") as handle:
            handle.write(f"{os.getpid()}\\n")
        return await original_ensure(self, *args, **kwargs)


    plugin.EmbeddedProvisioner.ensure_template = recorded
    """
)


@embedded_test
def test_embedded_snapshot_is_built_once_under_xdist(embedded_project: EmbeddedProject) -> None:
    pytest.importorskip("xdist")
    path = embedded_project.pytester.path
    (path / "conftest.py").write_text(
        (path / "conftest.py").read_text(encoding="utf-8") + EMBEDDED_WORKERS_CONFTEST, encoding="utf-8"
    )
    # `--dist each` runs every test on every worker, so both workers set the session up even if one is slow
    embedded_project.run("-n", "2", "--dist", "each").assert_outcomes(passed=4)
    workers = (path / "workers.txt").read_text(encoding="utf-8").split()
    assert len(set(workers)) == 2
    assert embedded_project.builds() == 1
    embedded_project.run("-n", "2", "--dist", "each").assert_outcomes(passed=4)
    assert embedded_project.builds() == 1


@embedded_test
def test_embedded_stale_snapshots_are_removed_unless_kept(embedded_project: EmbeddedProject) -> None:
    embedded_project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    (first,) = embedded_project.snapshots()
    embedded_project.write_migration(2, f"CREATE TABLE {embedded_project.table}_b (id int4);")
    embedded_project.run("--rowx-keep-templates").assert_outcomes(passed=2)
    assert len(embedded_project.snapshots()) == 2
    assert first in embedded_project.snapshots()
    embedded_project.write_migration(3, f"CREATE TABLE {embedded_project.table}_c (id int4);")
    embedded_project.run().assert_outcomes(passed=2)
    (last,) = embedded_project.snapshots()
    assert last != first


@embedded_test
def test_embedded_cleanup_spares_another_projects_snapshot(embedded_project: EmbeddedProject) -> None:
    embedded_project.run().assert_outcomes(passed=2)
    other = embedded_project.cache / "deadbeef_0123456789abcdef.snapshot"
    other.write_bytes(b"another project's")
    embedded_project.write_migration(2, f"CREATE TABLE {embedded_project.table}_b (id int4);")
    embedded_project.run().assert_outcomes(passed=2)
    assert other.read_bytes() == b"another project's"
    assert len(embedded_project.snapshots()) == 1


@embedded_test
@pytest.mark.parametrize("damage", ["altered", "refused-by-the-engine"])
def test_embedded_a_damaged_or_refused_snapshot_is_rebuilt(embedded_project: EmbeddedProject, damage: str) -> None:
    embedded_project.run().assert_outcomes(passed=2)
    (name,) = embedded_project.snapshots()
    path = embedded_project.cache / name
    good = path.stat().st_size
    if damage == "altered":
        # the digest does not match, so it is rebuilt without booting anything
        path.write_bytes(b"not a snapshot")
    else:
        # a whole file with a good digest that only the engine can refuse, at the first boot
        publish_blocking(path, embedded_project.prefix, b"not a snapshot either")
    embedded_project.run().assert_outcomes(passed=2)
    assert embedded_project.builds() == 2
    assert path.stat().st_size == good


@embedded_test
def test_embedded_without_the_cache_provider_the_snapshot_is_built_once_per_session(
    embedded_project: EmbeddedProject,
) -> None:
    embedded_project.run("-p", "no:cacheprovider").assert_outcomes(passed=2)
    assert embedded_project.builds() == 1
    assert not (embedded_project.pytester.path / ".pytest_cache").exists()
    embedded_project.run("-p", "no:cacheprovider").assert_outcomes(passed=2)
    assert embedded_project.builds() == 2


@embedded_test
def test_embedded_a_failing_test_leaves_nothing(embedded_project: EmbeddedProject) -> None:
    embedded_project.pytester.makepyfile(test_fail=EMBEDDED_FAILING_INNER)
    embedded_project.run("test_fail.py", "-p", "no:cacheprovider").assert_outcomes(failed=1)


@embedded_test
def test_embedded_an_interrupted_session_leaves_nothing(embedded_project: EmbeddedProject) -> None:
    embedded_project.pytester.makepyfile(test_interrupt=EMBEDDED_INTERRUPT_INNER)
    result = embedded_project.run("test_interrupt.py", "-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.INTERRUPTED


@embedded_test
def test_embedded_the_pool_and_a_connection_together_give_the_one_session_hint(
    embedded_project: EmbeddedProject,
) -> None:
    embedded_project.pytester.makepyfile(test_pool=f'TABLE = "{embedded_project.table}"\n' + EMBEDDED_POOL_INNER)
    embedded_project.run("test_pool.py").assert_outcomes(passed=1)
    chain = (embedded_project.pytester.path / "chain.txt").read_text(encoding="utf-8")
    assert "53300" in chain or "too_many" in chain.lower()
    assert "one session at a time" in chain
    assert "max_size=1" in chain


@embedded_test
def test_embedded_fixtures_work_from_pytest_asyncio_tests(embedded_project: EmbeddedProject) -> None:
    pytest.importorskip("pytest_asyncio")
    embedded_project.set_config(ini='asyncio_mode = "strict"\nasyncio_default_fixture_loop_scope = "function"\n')
    embedded_project.pytester.makepyfile(test_async=f'TABLE = "{embedded_project.table}"\n' + EMBEDDED_ASYNC_INNER)
    embedded_project.run("test_async.py").assert_outcomes(passed=5)


@embedded_test
def test_embedded_fixtures_work_from_an_anyio_test_on_asyncio(embedded_project: EmbeddedProject) -> None:
    pytest.importorskip("anyio")
    embedded_project.pytester.makepyfile(test_anyio=f'TABLE = "{embedded_project.table}"\n' + EMBEDDED_ANYIO_INNER)
    embedded_project.run("test_anyio.py").assert_outcomes(passed=2)


@embedded_test
def test_embedded_a_directory_url_is_a_usage_error(
    embedded_project: EmbeddedProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "embedded:///var/tmp/rowx-db")
    result = embedded_project.run("test_inner.py")
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*fresh database from a snapshot for every test*"])


# An explicit `embedded://` URL without the companion is a configuration error, so it FAILS (it never skips), with or
# without `rowx_require_database`: that setting is about a missing URL, not a missing engine. The test needs no
# companion (the import is blocked in the inner session), so it is marked `db`, which the embedded lane's expression
# `(db and not server) or embedded` and a plain `just test-py` both run; `embedded` would skip it where it matters most
@db_test
@pytest.mark.parametrize("require", [False, True], ids=["require-unset", "require-set"])
def test_embedded_url_without_the_extra_fails_clearly(embedded_project: EmbeddedProject, *, require: bool) -> None:
    embedded_project.set_config(ini="rowx_require_database = true\n" if require else "")
    # a stand-in for an environment without the extra: the import fails as it would for a missing distribution
    conftest = embedded_project.pytester.path / "conftest.py"
    conftest.write_text("import sys\n\nsys.modules['rowx_embedded'] = None\n", encoding="utf-8")
    result = embedded_project.run("test_inner.py")
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*rowx_embedded package, which is not installed*", "*uv add*rowx*"])
    # the premise: the blocked import is what the plugin reported, not a leftover of the counting conftest
    assert embedded_project.builds() == 0


@embedded_test
def test_embedded_teardown_does_not_wait_for_a_statement_a_test_leaked(embedded_project: EmbeddedProject) -> None:
    path = embedded_project.pytester.path
    (path / "conftest.py").write_text(
        (path / "conftest.py").read_text(encoding="utf-8") + EMBEDDED_LEAK_CONFTEST, encoding="utf-8"
    )
    embedded_project.pytester.makepyfile(test_leak=EMBEDDED_LEAK_INNER)
    embedded_project.run("test_leak.py", "-p", "no:cacheprovider").assert_outcomes(passed=1)
    # the instance is abandoned, so the statement's 120 s are not waited for
    assert float((path / "teardown.txt").read_text(encoding="utf-8")) < 5
    # (the fixture checks that no instance or directory outlives the session)
