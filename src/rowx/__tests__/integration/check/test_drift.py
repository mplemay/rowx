import os
import secrets
import time
import zlib
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from rowx.check.cli import main

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.db]

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


# a database of its own, so the migrator lock the test holds can't stall any other test
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
        # the lock was held throughout, and the gate neither took nor waited for an advisory lock
        row = holder.execute(
            "SELECT count(*) FILTER (WHERE pid = %s AND granted), count(*) FILTER (WHERE pid <> %s)"
            " FROM pg_locks WHERE locktype = 'advisory'",
            (pid, pid),
        ).fetchone()
        assert row == (1, 0)
