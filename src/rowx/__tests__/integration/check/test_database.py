import io
import os
import secrets
from typing import TYPE_CHECKING, override

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from rowx.check.cli import main

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.db]


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool) -> None:
        super().__init__(text)
        self.tty = tty

    @override
    def isatty(self) -> bool:
        return self.tty


@pytest.fixture
def server_dsn() -> str:
    dsn = os.environ.get("ROWX_TEST_DSN")
    if dsn is not None:
        return dsn
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
    pytest.skip("ROWX_TEST_DSN is not set")


# a uniquely named database that is not created yet and always dropped afterwards
@pytest.fixture
def target(server_dsn: str) -> Iterator[tuple[str, str]]:
    name = f"rowx_dbcli_{secrets.token_hex(6)}"
    yield make_conninfo(server_dsn, dbname=name), name
    with psycopg.connect(server_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "1_create_a.sql").write_text("CREATE TABLE db_a (id int4);", encoding="utf-8")
    (directory / "2_create_b.sql").write_text("CREATE TABLE db_b (id int4);", encoding="utf-8")
    return directory


def cli(capsys: pytest.CaptureFixture[str], dsn: str, *argv: str) -> tuple[int, str, str]:
    code = main(["database", *argv, "--database-url", dsn])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def exists(server_dsn: str, name: str) -> bool:
    with psycopg.connect(server_dsn, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


def applied(dsn: str) -> list[int]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return [row[0] for row in conn.execute("SELECT version FROM _rowx_migrations ORDER BY version")]


def test_create_twice_exits_zero(server_dsn: str, target: tuple[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    dsn, name = target
    assert cli(capsys, dsn, "create") == (0, f"Created database '{name}'\n", "")
    assert exists(server_dsn, name)
    assert cli(capsys, dsn, "create") == (0, f"Database '{name}' already exists\n", "")


def test_drop_with_yes(server_dsn: str, target: tuple[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    dsn, name = target
    cli(capsys, dsn, "create")
    assert cli(capsys, dsn, "drop", "-y") == (0, f"Dropped database '{name}'\n", "")
    assert not exists(server_dsn, name)
    # a missing database is not an error
    assert cli(capsys, dsn, "drop", "-y")[0] == 0


def test_drop_without_tty_or_yes_exits_two_and_keeps_the_database(
    server_dsn: str,
    target: tuple[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn, name = target
    cli(capsys, dsn, "create")
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=False))
    code, out, err = cli(capsys, dsn, "drop")
    assert (code, out) == (2, "")
    assert "pass -y" in err
    assert exists(server_dsn, name)


def test_drop_confirms_on_a_tty(
    server_dsn: str,
    target: tuple[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn, name = target
    cli(capsys, dsn, "create")
    monkeypatch.setattr("sys.stdin", FakeStdin("n\n", tty=True))
    assert cli(capsys, dsn, "drop")[0] == 0
    assert exists(server_dsn, name)
    monkeypatch.setattr("sys.stdin", FakeStdin("y\n", tty=True))
    assert cli(capsys, dsn, "drop")[0] == 0
    assert not exists(server_dsn, name)


def test_force_drops_a_database_with_a_live_session(
    server_dsn: str, target: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    dsn, name = target
    cli(capsys, dsn, "create")
    with psycopg.connect(dsn, autocommit=True) as session:
        session.execute("SELECT 1")
        code, _, err = cli(capsys, dsn, "drop", "-y")
        assert code == 2
        assert exists(server_dsn, name)
        assert cli(capsys, dsn, "drop", "-y", "--force")[0] == 0
        assert err
    assert not exists(server_dsn, name)


def test_setup_creates_and_migrates_then_is_idempotent(
    target: tuple[str, str], project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn, _ = target
    code, out, _ = cli(capsys, dsn, "setup", "--source", str(project))
    assert code == 0
    assert "Applied 1/migrate create a" in out
    assert applied(dsn) == [1, 2]
    code, out, _ = cli(capsys, dsn, "setup", "--source", str(project))
    assert code == 0
    assert "already exists" in out
    assert applied(dsn) == [1, 2]


def test_reset_recreates_the_database_with_every_migration_applied(
    target: tuple[str, str], project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn, _ = target
    cli(capsys, dsn, "setup", "--source", str(project))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE leftover (id int4)")
    assert cli(capsys, dsn, "reset", "-y", "--source", str(project))[0] == 0
    assert applied(dsn) == [1, 2]
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('leftover')").fetchone() == (None,)


def test_reset_without_tty_or_yes_exits_two(
    target: tuple[str, str], project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn, _ = target
    cli(capsys, dsn, "setup", "--source", str(project))
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=False))
    assert cli(capsys, dsn, "reset", "--source", str(project))[0] == 2
    assert applied(dsn) == [1, 2]


def test_a_password_is_never_printed(
    server_dsn: str, target: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, name = target
    secret = "s3cr3t-pw-" + secrets.token_hex(4)
    unreachable = f"postgresql://someone:{secret}@127.0.0.1:1/{name}"
    for argv in (("create",), ("drop", "-y"), ("setup",), ("reset", "-y")):
        code, out, err = cli(capsys, unreachable, *argv)
        assert code == 2
        assert secret not in out + err
        assert unreachable not in out + err
    # a wrong password reaches the server, whose error must not echo it
    wrong = make_conninfo(server_dsn, dbname=name, password=secret)
    _, out, err = cli(capsys, wrong, "create")
    assert secret not in out + err


def test_invalid_migrations_fail_before_any_database_change(
    server_dsn: str, target: tuple[str, str], project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn, name = target
    (project / "1_duplicate.sql").write_text("SELECT 1;", encoding="utf-8")
    assert cli(capsys, dsn, "setup", "--source", str(project))[0] == 2
    assert not exists(server_dsn, name)
    cli(capsys, dsn, "create")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE kept (id int4)")
    code, _, err = cli(capsys, dsn, "reset", "-y", "--source", str(project))
    assert code == 2
    assert err
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('kept') IS NOT NULL").fetchone() == (True,)
