import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from rowx._backend import UnsupportedSchemeError, module_for

QUERY: Final[str] = 'from rowx import query\n\nquery[int](t"SELECT 1")\n'
MONEY_QUERY: Final[str] = """\
from rowx import query
from rowx_postgres import Money

query[Money](t"SELECT 1")
"""
MONEY_CONCAT: Final[str] = """\
from rowx_postgres import Money

x = t"a" + Money(1)
"""
MISSING_BACKEND: Final[str] = """\
import sys
from importlib.abc import MetaPathFinder


class _BlockPostgres(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "rowx_postgres" or fullname.startswith("rowx_postgres."):
            message = f"no module named {fullname}"
            raise ModuleNotFoundError(message, name=fullname)
        return None


sys.meta_path.insert(0, _BlockPostgres())
assert "rowx_postgres" not in sys.modules
from rowx.check.cli import main

assert "rowx_postgres" not in sys.modules
assert "rowx.check.migrate" not in sys.modules
assert "rowx.check.database" not in sys.modules
assert "rowx.check.drift" not in sys.modules
assert "rowx.check.scratch" not in sys.modules
code = main(["check", "--database-url", "postgres://localhost/db", sys.argv[1]])
assert "rowx.check.migrate" not in sys.modules
assert "rowx.check.database" not in sys.modules
assert "rowx.check.drift" not in sys.modules
assert "rowx.check.scratch" not in sys.modules
raise SystemExit(code)
"""
BLOCKED_CHECK: Final[str] = """\
import os
import sys
from importlib.abc import MetaPathFinder


class _BlockPostgres(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "rowx_postgres" or fullname.startswith("rowx_postgres."):
            message = f"no module named {fullname}"
            raise ModuleNotFoundError(message, name=fullname)
        return None


sys.meta_path.insert(0, _BlockPostgres())
os.chdir(sys.argv[1])
os.environ.pop("DATABASE_URL", None)
from rowx.check.cli import main

code = main(sys.argv[2:])
"""
MIGRATE_TABLE: Final[str] = "[tool.rowx.migrate]\n"
ASSIGNMENT: Final[str] = "x = 1\n"


def _run_blocked(directory: Path, args: list[str], *absent: str) -> subprocess.CompletedProcess[str]:
    checks = "".join(f"assert {name!r} not in sys.modules\n" for name in absent)
    script = BLOCKED_CHECK + checks + "raise SystemExit(code)\n"
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", script, str(directory), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_missing_backend_exits_two(tmp_path: Path) -> None:
    path = tmp_path / "query.py"
    path.write_text(QUERY, encoding="utf-8")
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", MISSING_BACKEND, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "install `rowx[postgres]`" in result.stderr
    assert "internal error" not in result.stderr


def test_no_statement_skips_a_postgres_url_when_the_extra_is_missing(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(ASSIGNMENT, encoding="utf-8")
    result = _run_blocked(tmp_path, ["check", "--database-url", "postgres://localhost/db", "x.py"])
    assert result.returncode == 0, result.stderr


def test_no_statement_skips_an_unknown_scheme(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(ASSIGNMENT, encoding="utf-8")
    result = _run_blocked(tmp_path, ["check", "--database-url", "sqlite://x", "x.py"])
    assert result.returncode == 0, result.stderr


def test_unknown_scheme_on_a_statement_exits_two(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(QUERY, encoding="utf-8")
    result = _run_blocked(tmp_path, ["check", "--database-url", "sqlite://x", "x.py"])
    assert result.returncode == 2, result.stderr
    assert "unknown database scheme" in result.stderr
    assert "rowx[postgres]" not in result.stderr


def _migrate_project(directory: Path, source: str) -> None:
    (directory / "pyproject.toml").write_text(MIGRATE_TABLE, encoding="utf-8")
    (directory / "x.py").write_text(source, encoding="utf-8")


def test_migrate_project_with_no_statement_does_not_import_a_backend(tmp_path: Path) -> None:
    _migrate_project(tmp_path, ASSIGNMENT)
    result = _run_blocked(
        tmp_path,
        ["check", "x.py"],
        "rowx_postgres",
        "rowx.check.drift",
        "rowx.check.scratch",
    )
    assert result.returncode == 0, result.stderr


def test_migrate_project_without_a_url_names_the_missing_url(tmp_path: Path) -> None:
    _migrate_project(tmp_path, QUERY)
    result = _run_blocked(tmp_path, ["check", "x.py"])
    assert result.returncode == 2, result.stderr
    assert "no database URL" in result.stderr
    assert "install `rowx[postgres]`" not in result.stderr


def test_money_query_without_a_url_names_the_missing_url(tmp_path: Path) -> None:
    _migrate_project(tmp_path, MONEY_QUERY)
    result = _run_blocked(tmp_path, ["check", "x.py"], "rowx_postgres")
    assert result.returncode == 2, result.stderr
    assert "no database URL" in result.stderr
    assert "install `rowx[postgres]`" not in result.stderr


def _mentions_template_construction(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}"
    return "template-construction" in output or "a t-string can only be added" in output


def test_template_concat_does_not_import_a_backend_for_a_postgres_url(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(MONEY_CONCAT, encoding="utf-8")
    result = _run_blocked(
        tmp_path,
        ["check", "--database-url", "postgres://localhost/db", "x.py"],
        "rowx_postgres",
    )
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert _mentions_template_construction(result)
    assert "install `rowx[postgres]`" not in result.stderr


def test_template_concat_with_an_unknown_scheme_stays_template_construction(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(MONEY_CONCAT, encoding="utf-8")
    result = _run_blocked(tmp_path, ["check", "--database-url", "sqlite://x", "x.py"], "rowx_postgres")
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert _mentions_template_construction(result)
    assert "unknown database scheme" not in result.stderr
    assert "rowx[postgres]" not in result.stderr


def test_money_query_with_an_unknown_scheme_exits_two(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(MONEY_QUERY, encoding="utf-8")
    result = _run_blocked(tmp_path, ["check", "--database-url", "sqlite://x", "x.py"])
    assert result.returncode == 2, result.stderr
    assert "unknown database scheme" in result.stderr
    assert "rowx[postgres]" not in result.stderr


def test_scratch_with_no_statement_does_not_import_scratch(tmp_path: Path) -> None:
    _migrate_project(tmp_path, ASSIGNMENT)
    result = _run_blocked(tmp_path, ["check", "--scratch", "x.py"], "rowx_postgres", "rowx.check.scratch")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "dsn",
    [
        "host=localhost dbname=app",
        "postgres://localhost/app",
        "postgresql://localhost/app",
        "PostgreSQL://localhost/app",
    ],
)
def test_postgres_dsns_select_the_postgres_backend(dsn: str) -> None:
    assert module_for(dsn) == "rowx_postgres._backend"


@pytest.mark.parametrize(
    "argv",
    [
        ["migrate", "info", "--database-url", "sqlite://x"],
        ["migrate", "run", "--database-url", "mysql://x"],
        ["database", "create", "--database-url", "sqlite://x"],
        ["database", "setup", "--database-url", "mysql://x"],
    ],
)
def test_unknown_scheme_on_migrate_and_database_exits_two(tmp_path: Path, argv: list[str]) -> None:
    result = _run_blocked(tmp_path, [*argv, "--connect-timeout", "0"])
    assert result.returncode == 2, result.stderr
    assert "unknown database scheme" in result.stderr
    assert "rowx[postgres]" not in result.stderr


@pytest.mark.parametrize("dsn", ["sqlite://x", "mysql://x"])
def test_unknown_scheme_does_not_ask_for_the_postgres_extra(dsn: str) -> None:
    with pytest.raises(UnsupportedSchemeError, match="unknown database scheme") as raised:
        module_for(dsn)
    assert "rowx[postgres]" not in str(raised.value)


def test_migrate_add_existing_directory_does_not_import_a_backend(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    result = _run_blocked(
        tmp_path,
        ["migrate", "add", "next change", "--sequential", "--source", "migrations"],
        "rowx_postgres",
    )
    assert result.returncode == 0, result.stderr
    assert (directory / "0002_next_change.sql").is_file()
