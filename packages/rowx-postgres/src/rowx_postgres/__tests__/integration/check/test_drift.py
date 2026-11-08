import os
import secrets
import time
import zlib
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from rowx.check.cli import main

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = [pytest.mark.db]

# sqlx's multiplier, so the lock held here is the one a migrator takes
LOCK_MULTIPLIER: Final[int] = 0x3D32AD9E
WAIT_LIMIT: Final[float] = 20.0


@pytest.fixture
def server_dsn() -> str:
    dsn = os.environ.get("ROWX_TEST_DSN")
    if dsn is not None:
        return dsn
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
    pytest.skip("ROWX_TEST_DSN is not set")


# A database of its own rather than the `schema` fixture: the migrator lock's key comes from the database name, so
# holding it in the shared test database would stall every migration running there, whatever its schema
@pytest.fixture
def database(server_dsn: str) -> Iterator[tuple[str, str]]:
    name = f"rowx_drift_{secrets.token_hex(6)}"
    with psycopg.connect(server_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(server_dsn, dbname=name), name
    finally:
        with psycopg.connect(server_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    # a short statement_timeout, so waiting behind a lock would fail the check instead of hanging
    (tmp_path / "pyproject.toml").write_text(
        '[tool.rowx]\nstatement_timeout = "2s"\n\n[tool.rowx.migrate]\n',
        encoding="utf-8",
    )
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "1_create_gate_a.sql").write_text("CREATE TABLE gate_a (id int4 NOT NULL);", encoding="utf-8")
    (tmp_path / "q.py").write_text(
        'from rowx import query\n\nquery[int](t"SELECT id FROM gate_a")\n',
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.usefixtures("project")
def test_the_drift_gate_does_not_wait_while_another_backend_holds_the_migrator_lock(
    database: tuple[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn, name = database
    assert main(["migrate", "run", "--database-url", dsn]) == 0
    capsys.readouterr()

    key = LOCK_MULTIPLIER * zlib.crc32(name.encode())
    with psycopg.connect(dsn, autocommit=True) as holder:
        row = holder.execute("SELECT pg_backend_pid()").fetchone()
        assert row is not None
        pid = row[0]
        holder.execute("SELECT pg_advisory_lock(%s)", (key,))
        started = time.monotonic()
        code = main(["check", "q.py", "--database-url", dsn])
        elapsed = time.monotonic() - started
        captured = capsys.readouterr()
        assert (code, captured.out, captured.err) == (0, "", "")
        assert elapsed < WAIT_LIMIT
        # the lock was held throughout, and the gate neither took nor waited for an advisory lock. pg_locks is
        # cluster-wide, so only this test's private database counts: other sessions on the server may hold their own
        row = holder.execute(
            "SELECT count(*) FILTER (WHERE pid = %s AND granted), count(*) FILTER (WHERE pid <> %s)"
            " FROM pg_locks WHERE locktype = 'advisory'"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
            (pid, pid),
        ).fetchone()
        assert row == (1, 0)


def test_config_ignore_missing_lets_a_missing_migration_pass(
    project: Path,
    database: tuple[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn, _ = database
    assert main(["migrate", "run", "--database-url", dsn]) == 0
    (project / "migrations" / "1_create_gate_a.sql").unlink()
    (project / "migrations" / "2_create_gate_b.sql").write_text(
        "CREATE TABLE gate_b (id int4 NOT NULL);", encoding="utf-8"
    )
    assert main(["migrate", "run", "--database-url", dsn, "--ignore-missing"]) == 0
    capsys.readouterr()
    (project / "q.py").write_text('from rowx import query\n\nquery[int](t"SELECT id FROM gate_b")\n', encoding="utf-8")

    # the default: a missing migration is drift
    code = main(["check", "q.py", "--database-url", dsn])
    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert captured.err == (
        "error: database has drifted from `migrations/`:\n"
        "  1 missing: 1\n"
        "run `rowx migrate run`, or `rowx check --scratch`\n"
    )

    pyproject = project / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8") + "ignore_missing = true\n", encoding="utf-8")
    assert main(["check", "q.py", "--database-url", dsn]) == 0
    assert capsys.readouterr() == ("", "")

    # a pending migration is still drift
    (project / "migrations" / "3_create_gate_c.sql").write_text("SELECT 1;", encoding="utf-8")
    code = main(["check", "q.py", "--database-url", dsn])
    captured = capsys.readouterr()
    assert code == 2
    assert "  1 pending: 3\n" in captured.err
    assert "missing" not in captured.err


@pytest.fixture
def unprivileged_dsn(database: tuple[str, str]) -> Iterator[tuple[str, str]]:
    dsn, _ = database
    role = f"rowx_drift_role_{secrets.token_hex(4)}"
    password = f"pw-{secrets.token_hex(8)}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    try:
        yield make_conninfo(dsn, user=role, password=password), password
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            # revokes a grant a test made, which would block the drop
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.mark.usefixtures("project")
def test_a_role_without_select_on_the_history_table_exits_two_with_a_grant_hint(
    database: tuple[str, str],
    unprivileged_dsn: tuple[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner, _ = database
    dsn, password = unprivileged_dsn
    assert main(["migrate", "run", "--database-url", owner]) == 0
    capsys.readouterr()

    code = main(["check", "q.py", "--database-url", dsn])
    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert captured.err == (
        "error: could not read the migration history: permission denied; "
        "grant SELECT on _rowx_migrations to the checker role\n"
    )
    assert password not in captured.err

    # granting it is the fix
    with psycopg.connect(owner, autocommit=True) as admin:
        role = make_role_name(dsn)
        admin.execute(sql.SQL("GRANT SELECT ON _rowx_migrations TO {}").format(sql.Identifier(role)))
    assert main(["check", "q.py", "--database-url", dsn]) == 0


def make_role_name(dsn: str) -> str:
    return str(conninfo_to_dict(dsn)["user"])
