import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.db


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


def run_compat(root: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            str(Path(sys.executable).parent / "rowx"),
            "check-compat",
            "--against",
            base,
            "--head",
            head,
            "--database-url",
            os.environ["ROWX_TEST_DSN"],
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("base_sql", "head_sql", "base_source", "head_source", "status", "direction"),
    [
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
            "CREATE TABLE t (x int NOT NULL, y int NOT NULL)",
            "CREATE TABLE t (x xml NOT NULL, y int NOT NULL)",
            "query[int](t'SELECT x FROM t')",
            "query[int](t'SELECT y FROM t')",
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
    ],
)
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
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    prefix = "from rowx import query\n"
    base = commit(tmp_path, prefix + base_source, base_sql)
    head = commit(tmp_path, prefix + head_source, head_sql)
    result = run_compat(tmp_path, base, head)
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


@pytest.mark.parametrize(
    ("source", "migration", "expected"),
    [
        ("from rowx import query\nquery[int](t'SELECT missing FROM t')", "CREATE TABLE t (x int)", "own schema"),
        ("from rowx import query\nquery[int](t'SELECT 1')", "SELECT nonexistent_function()", "building schema"),
        ("from rowx import query_unchecked\nquery_unchecked(t'SELECT 1')", "SELECT 1", "unchecked"),
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
