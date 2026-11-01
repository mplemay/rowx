import os
import secrets
import shutil
from pathlib import Path
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from sys import executable
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from rowx.check.cli import main

if TYPE_CHECKING:
    from collections.abc import Iterator

pytest_plugins = ["pytester"]

pytestmark = [pytest.mark.integration, pytest.mark.db]

EXAMPLE: Final[Path] = Path(__file__).resolve().parents[5] / "examples" / "migrations"
DSN: Final[str | None] = os.environ.get("ROWX_TEST_DSN")


@pytest.fixture
def server_url() -> str:
    if DSN is None:
        if os.environ.get("ROWX_REQUIRE_DB") == "1":
            pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
        pytest.skip("ROWX_TEST_DSN is not set")
    return DSN


# a database of its own, because the example migrates it and `rowx check --scratch` and the plugin create more
@pytest.fixture
def database_url(server_url: str) -> Iterator[str]:
    name = f"rowx_example_{secrets.token_hex(6)}"
    with psycopg.connect(server_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(server_url, dbname=name)
        finally:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _run_app(url: str) -> tuple[int, str, str]:
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [executable, str(EXAMPLE / "bookshelf.py")],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url},
    )
    return result.returncode, result.stdout, result.stderr


def test_app_migrates_at_startup_and_is_repeatable(database_url: str) -> None:
    code, out, err = _run_app(database_url)
    assert code == 0, err
    assert out.count("applied ") == 3
    assert "Octavia Butler: 1 book(s)" in out
    code, out, err = _run_app(database_url)
    assert code == 0, err
    assert "applied " not in out
    assert "Octavia Butler: 1 book(s)" in out


def test_app_requires_a_database_url() -> None:
    env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [executable, str(EXAMPLE / "bookshelf.py")], check=False, capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "DATABASE_URL is not set" in result.stderr


def test_check_fails_on_drift_then_passes_once_migrated(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(EXAMPLE)
    assert main(["migrate", "run", "--database-url", database_url, "--dry-run"]) == 0
    assert "Would apply 20261001090000/migrate create authors" in capsys.readouterr().out

    assert main(["check", "--database-url", database_url]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "database has drifted from `migrations/`" in captured.err
    assert "3 pending: 20261001090000, 20261002090000, 20261003090000" in captured.err

    assert main(["migrate", "run", "--database-url", database_url]) == 0
    capsys.readouterr()
    assert main(["check", "--database-url", database_url]) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_check_scratch_needs_no_prepared_database(
    server_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(EXAMPLE)
    assert main(["check", "--scratch", "--database-url", server_url]) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_plugin_tests_pass(pytester: pytest.Pytester, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # the plugin clones from a server URL and creates its own databases there
    monkeypatch.setenv("DATABASE_URL", server_url)
    monkeypatch.delenv("ROWX_REQUIRE_DB", raising=False)
    shutil.copytree(EXAMPLE, pytester.path, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=3)
