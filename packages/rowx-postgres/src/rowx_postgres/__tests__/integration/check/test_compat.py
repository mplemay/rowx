import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from rowx_postgres.__tests__.integration.check.harness import require_companion

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from rowx_core.backend import Describe

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def companion() -> None:
    require_companion()


# a short path of its own (a socket path is limited to about 100 bytes), so a leftover can be counted exactly
@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="rxk-"))
    monkeypatch.setenv("TMPDIR", str(root))
    yield root
    leftovers = sorted(path.name for path in root.iterdir() if path.name.startswith("rxe-"))
    shutil.rmtree(root, ignore_errors=True)
    assert not leftovers, f"an instance left {leftovers} behind"


def embedded_only[F: Callable[..., object]](test: F) -> F:
    return pytest.mark.embedded(pytest.mark.usefixtures("companion", "private_tmp")(test))


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


def run_compat(root: Path, base: str, head: str, dsn: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            str(Path(sys.executable).parent / "rowx"),
            "check-compat",
            "--against",
            base,
            "--head",
            head,
            "--database-url",
            dsn or os.environ["ROWX_TEST_DSN"],
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


ROLLOUT_NAMES = ("base_sql", "head_sql", "base_source", "head_source", "status", "direction")
ROLLOUT_CASES = [
    (
        "CREATE TABLE t (old int NOT NULL)",
        "CREATE TABLE t (new int NOT NULL)",
        "query[int](t'SELECT old FROM t')",
        "query[int](t'SELECT new FROM t')",
        1,
        "migrate first",
    ),
    (
        "CREATE TABLE t (x int NOT NULL)",
        "CREATE TABLE t (x int NOT NULL)",
        "query[int](t'SELECT x FROM t')",
        "query[int](t'SELECT x FROM t')",
        0,
        "compatible checked subset",
    ),
    (
        "CREATE TABLE t (x int NOT NULL)",
        "CREATE TABLE t (x int)",
        "query[int](t'SELECT x FROM t')",
        "query[int | None](t'SELECT x FROM t')",
        1,
        "migrate first",
    ),
    (
        "CREATE TABLE t (x int)",
        "CREATE TABLE t (x int NOT NULL)",
        "query[int | None](t'SELECT x FROM t')",
        "query[int | None](t'SELECT x FROM t')",
        0,
        "compatible checked subset",
    ),
]
# the embedded engine has no `xml` type
XML_ROLLOUT = (
    "CREATE TABLE t (x int NOT NULL, y int NOT NULL)",
    "CREATE TABLE t (x xml NOT NULL, y int NOT NULL)",
    "query[int](t'SELECT x FROM t')",
    "query[int](t'SELECT y FROM t')",
    1,
    "migrate first",
)


def assert_rollout(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    tmp_path: Path,
    dsn: str | None,
    base_sql: str,
    head_sql: str,
    base_source: str,
    head_source: str,
    status: int,
    direction: str,
) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    prefix = "from rowx import query\n"
    base = commit(tmp_path, prefix + base_source, base_sql)
    head = commit(tmp_path, prefix + head_source, head_sql)
    result = run_compat(tmp_path, base, head, dsn)
    assert result.returncode == status, result.stdout + result.stderr
    assert direction in result.stdout
    assert base in result.stdout
    assert head in result.stdout
    if status == 1:
        assert "q.py:" in result.stdout
        migrate = result.stdout.split("migrate first:", 1)[1].split("deploy first:", 1)[0]
        assert "error[" in migrate
        if "old int" in base_sql:
            deploy = result.stdout.split("deploy first:", 1)[1]
            assert "error[" in deploy


@pytest.mark.server(needs="create-database")
@pytest.mark.parametrize(ROLLOUT_NAMES, [*ROLLOUT_CASES, XML_ROLLOUT])
def test_rollout_directions(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    tmp_path: Path,
    schema: str,
    base_sql: str,
    head_sql: str,
    base_source: str,
    head_source: str,
    status: int,
    direction: str,
) -> None:
    del schema
    assert_rollout(tmp_path, None, base_sql, head_sql, base_source, head_source, status, direction)


@pytest.mark.server(needs="create-database")
def test_historical_source_is_never_executed(tmp_path: Path, schema: str) -> None:
    del schema
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    marker = tmp_path / "marker"
    annotation_marker = tmp_path / "annotation-marker"
    source = (
        "from typing import Annotated\nfrom rowx import query\n"
        f"open({str(marker)!r}, 'w').write('executed')\n"
        "def annotation():\n"
        f"    open({str(annotation_marker)!r}, 'w').write('executed')\n"
        "    return int\n"
        "query[Annotated[int, annotation()]](t'SELECT 1')\n"
    )
    base = commit(tmp_path, source, "SELECT 1")
    (tmp_path / "q.py").write_text("this is not valid python !", encoding="utf-8")
    result = run_compat(tmp_path, base, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "described" in result.stdout
    assert not marker.exists()
    assert not annotation_marker.exists()


@pytest.mark.server(needs="create-database")
@pytest.mark.parametrize(
    "source", ["query[int | None](t'SELECT {None}::int')", "query[list[int | None] | None](t'SELECT {[None]}::int[]')"]
)
def test_known_null_self_compatibility(tmp_path: Path, schema: str, source: str) -> None:
    del schema
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    revision = commit(tmp_path, "from rowx import query\n" + source, "SELECT 1")
    result = run_compat(tmp_path, revision, revision)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compatible checked subset" in result.stdout


@pytest.mark.server(needs="create-database")
@pytest.mark.parametrize(
    ("source", "migration", "expected"),
    [
        ("from rowx import query\nquery[int](t'SELECT missing FROM t')", "CREATE TABLE t (x int)", "own schema"),
        ("from rowx import query\nquery[int](t'SELECT 1')", "SELECT nonexistent_function()", "building schema"),
        ("from rowx import query_unchecked\nquery_unchecked(t'SELECT 1')", "SELECT 1", "unchecked"),
        ("x = 1", "SELECT 1", "incomplete"),
        ("from rowx import query\nquery[Missing](t'SELECT 1')", "SELECT 1", "incomplete"),
        ("from rowx import query\nquery[int](t'SELECT {unknown}::int')", "SELECT 1", "incomplete"),
    ],
)
def test_incomplete_runs(tmp_path: Path, schema: str, source: str, migration: str, expected: str) -> None:
    del schema
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    revision = commit(tmp_path, source, migration)
    with psycopg.connect(os.environ["ROWX_TEST_DSN"], autocommit=True) as conn:
        before = set(conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'rowx\\_check\\_%'").fetchall())
    result = run_compat(tmp_path, revision, revision)
    with psycopg.connect(os.environ["ROWX_TEST_DSN"], autocommit=True) as conn:
        after = set(conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'rowx\\_check\\_%'").fetchall())
    assert after == before
    assert result.returncode == 2, result.stdout + result.stderr
    assert expected in result.stdout + result.stderr
    assert "compatible checked subset" not in result.stdout


@pytest.mark.server(needs="create-database")
def test_cleanup_after_second_schema_fails(tmp_path: Path, schema: str) -> None:
    del schema
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    source = "from rowx import query\nquery[int](t'SELECT 1')"
    base = commit(tmp_path, source, "SELECT 1")
    head = commit(tmp_path, source, "SELECT nonexistent_function()")
    with psycopg.connect(os.environ["ROWX_TEST_DSN"], autocommit=True) as conn:
        before = set(conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'rowx\\_check\\_%'").fetchall())
    result = run_compat(tmp_path, base, head)
    with psycopg.connect(os.environ["ROWX_TEST_DSN"], autocommit=True) as conn:
        after = set(conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'rowx\\_check\\_%'").fetchall())
    assert result.returncode == 2
    assert head in result.stderr
    assert after == before


@pytest.mark.server(needs="create-database")
def test_checked_subset_reports_skipped_sites(tmp_path: Path, schema: str) -> None:
    del schema
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    source = "from rowx import query, query_unchecked\nquery[int](t'SELECT 1')\nquery_unchecked(t'SELECT 1')\n"
    revision = commit(tmp_path, source, "SELECT 1")
    result = run_compat(tmp_path, revision, revision)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("unchecked") >= 2
    assert "compatible checked subset" in result.stdout


@pytest.mark.server(needs="create-database")
def test_cross_schema_timeout_identifies_phase(
    tmp_path: Path, schema: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rowx_core.errors import DatabaseError  # ruff: ignore[import-outside-top-level]
    from rowx_postgres._services import PostgresCheckerSession  # ruff: ignore[import-outside-top-level]

    from rowx.check.cli import main  # ruff: ignore[import-outside-top-level]

    del schema
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    source = "from rowx import query\nquery[int](t'SELECT 1')"
    base = commit(tmp_path, source, "SELECT 1")
    head = commit(tmp_path, source + "\n# head", "SELECT 1")
    original = PostgresCheckerSession.describe
    calls = 0

    async def describe_with_timeout(self: PostgresCheckerSession, sql: str) -> Describe:
        nonlocal calls
        calls += 1
        if calls == 3:
            error = DatabaseError("57014", "injected timeout")
            raise error
        return await original(self, sql)

    monkeypatch.setattr(PostgresCheckerSession, "describe", describe_with_timeout)
    monkeypatch.chdir(tmp_path)
    status = main(["check-compat", "--against", base, "--head", head, "--database-url", os.environ["ROWX_TEST_DSN"]])
    captured = capsys.readouterr()
    assert status == 2
    assert f"migrate first: source {base}, schema {head}" in captured.out
    assert "timeout" in captured.err
    assert "compatible checked subset" not in captured.out


@pytest.mark.server(needs="create-database")
@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [("53200", 2), ("53100", 2), ("55P03", 2), ("55006", 2), ("42501", 2), ("40P01", 2), ("40001", 2), ("42703", 1)],
)
def test_cross_schema_database_failure_classification(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    tmp_path: Path,
    schema: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sqlstate: str,
    expected: int,
) -> None:
    from rowx_core.errors import DatabaseError  # ruff: ignore[import-outside-top-level]
    from rowx_postgres._services import PostgresCheckerSession  # ruff: ignore[import-outside-top-level]

    from rowx.check.cli import main  # ruff: ignore[import-outside-top-level]

    del schema
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    sha = commit(tmp_path, "from rowx import query\nquery[int](t'SELECT x FROM t')", "CREATE TABLE t (x int NOT NULL)")
    original = PostgresCheckerSession.describe
    calls = 0

    async def describe_with_failure(self: PostgresCheckerSession, sql: str) -> Describe:
        nonlocal calls
        calls += 1
        if calls == 3:
            error = DatabaseError(sqlstate, "injected database error")
            raise error
        return await original(self, sql)

    monkeypatch.setattr(PostgresCheckerSession, "describe", describe_with_failure)
    monkeypatch.chdir(tmp_path)
    status = main(["check-compat", "--against", sha, "--head", sha, "--database-url", os.environ["ROWX_TEST_DSN"]])
    output = capsys.readouterr().out
    assert calls == 4
    assert status == expected
    assert "sql-error" in output
    assert ("incomplete" in output) == (expected == 2)


# `check-compat` builds each revision's schema in memory on `embedded://`, one instance per describe worker
@embedded_only
@pytest.mark.parametrize(ROLLOUT_NAMES, ROLLOUT_CASES)
def test_rollout_directions_on_embedded(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    tmp_path: Path,
    base_sql: str,
    head_sql: str,
    base_source: str,
    head_source: str,
    status: int,
    direction: str,
) -> None:
    assert_rollout(tmp_path, "embedded://", base_sql, head_sql, base_source, head_source, status, direction)


@embedded_only
def test_embedded_compat_describes_several_statements_on_several_instances(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    source = (
        "from rowx import query\n"
        "query[int](t'SELECT x FROM t')\n"
        "query[int](t'SELECT x FROM t WHERE x > 1')\n"
        "query[int](t'SELECT x FROM t WHERE x > 2')\n"
    )
    revision = commit(tmp_path, source, "CREATE TABLE t (x int NOT NULL)")
    result = run_compat(tmp_path, revision, revision, "embedded://")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compatible checked subset" in result.stdout
    assert "disposable PostgreSQL server" not in result.stderr


@embedded_only
def test_embedded_compat_reports_a_failing_migration_like_a_server_and_leaves_nothing(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    source = "from rowx import query\nquery[int](t'SELECT 1')"
    base = commit(tmp_path, source, "SELECT 1")
    head = commit(tmp_path, source, "SELECT nonexistent_function()")
    result = run_compat(tmp_path, base, head, "embedded://")
    assert result.returncode == 2
    assert head in result.stderr
    assert "building schema" in result.stderr
    assert "the migrations failed on the scratch database" in result.stderr
    assert "rxe-" not in result.stdout + result.stderr
    assert "compatible checked subset" not in result.stdout
